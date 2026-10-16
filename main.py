from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from typing import List, Dict, Any
from ortools.sat.python import cp_model
from collections import Counter
import traceback
import json
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}

@app.options("/resoudre")
async def options_resoudre():
    return JSONResponse(content={"status": "ok"}, headers=CORS_HEADERS)

def est_engin_faisable(v: Dict[str, Any], agents: List[Dict[str, Any]]) -> bool:
    """Vérifie si l'effectif présent permet mathématiquement d'armer cet engin."""
    besoins = [str(b).strip() for b in v.get("besoins", []) if b]
    if not besoins:
        return len(agents) >= 1

    agents_eligibles = set()
    for a_idx, a in enumerate(agents):
        specs_a = [str(s).strip() for s in a.get("specs", []) if s]
        if any(b == "Tout agent" or b in specs_a for b in besoins):
            agents_eligibles.add(a_idx)
    if len(agents_eligibles) < len(besoins):
        return False

    compte_besoins = Counter(b for b in besoins if b != "Tout agent")
    for spec, nb_requis in compte_besoins.items():
        nb_dispo = sum(1 for a in agents if spec in [str(s).strip() for s in a.get("specs", [])])
        if nb_dispo < nb_requis:
            return False

    return True

@app.get("/")
def home():
    return JSONResponse(
        content={"status": "en ligne", "service": "CIS Optimiseur OR-Tools v7 (Armement Maximal & Mobilisation Totale)"},
        headers=CORS_HEADERS
    )

@app.post("/resoudre")
async def resoudre(request: Request):
    try:
        body_bytes = await request.body()
        data = json.loads(body_bytes.decode("utf-8"))

        agents_raw = data.get("agents", [])
        vehicules_raw = data.get("vehicules", [])

        agents = []
        for a in agents_raw:
            agents.append({
                "nom": str(a.get("nom", "")).strip(),
                "specs": [str(s).strip() for s in a.get("specs", []) if s]
            })

        vehicules = []
        for v in vehicules_raw:
            besoins_propres = [str(b).strip() for b in v.get("besoins", []) if b]
            if not besoins_propres:
                besoins_propres = ["Tout agent"]
            vehicules.append({
                "nom": str(v.get("nom", "")).strip(),
                "priorite": str(v.get("priorite", "")).strip(),
                "groupe": str(v.get("groupe", "")).strip().upper(),
                "rangPrio": int(v.get("rangPrio", 999)) if str(v.get("rangPrio", "")).isdigit() else 999,
                "prioritaire": bool(v.get("prioritaire", False)),
                "besoins": besoins_propres
            })

        model = cp_model.CpModel()

        # =====================================================================
        # 1. DÉTECTION DES GROUPES JUMEAUX (Colonne M : G1 à G99)
        # =====================================================================
        groupes_jumeaux = {}
        vehicule_vers_groupe = {}
        for v_idx, v in enumerate(vehicules):
            grp_raw = v["groupe"].lower()
            if not grp_raw:
                grp_raw = v["priorite"].lower()

            m = re.match(r'^(?:groupe\s*|g)?([1-9]\d?)$', grp_raw) if v["groupe"] else re.match(r'^(groupe\s*\d+|g\d+)$', grp_raw)
            if m:
                num_grp = re.sub(r'\D', '', m.group(0))
                grp_id = f"g{num_grp}"
                if grp_id not in groupes_jumeaux:
                    groupes_jumeaux[grp_id] = []
                groupes_jumeaux[grp_id].append(v_idx)
                vehicule_vers_groupe[v_idx] = grp_id

        # =====================================================================
        # 2. VÉHICULES MATHÉMATIQUEMENT FAISABLES AUJOURD'HUI
        # =====================================================================
        vehicules_faisables = {v_idx: est_engin_faisable(v, agents) for v_idx, v in enumerate(vehicules)}
        for grp_id, v_indices in groupes_jumeaux.items():
            if not all(vehicules_faisables[i] for i in v_indices):
                for i in v_indices:
                    vehicules_faisables[i] = False

        # =====================================================================
        # 3. RARETÉ UTILE DES COMPÉTENCES
        # =====================================================================
        demande_specs = {}
        groupes_comptes = set()
        for v_idx, v in enumerate(vehicules):
            if not vehicules_faisables[v_idx]:
                continue
            grp_id = vehicule_vers_groupe.get(v_idx)
            if grp_id:
                if grp_id in groupes_comptes:
                    continue
                groupes_comptes.add(grp_id)

            for b in v["besoins"]:
                if b and b != "Tout agent":
                    demande_specs[b] = demande_specs.get(b, 0) + 1

        offre_specs = {}
        for a in agents:
            for s in set(a["specs"]):
                offre_specs[s] = offre_specs.get(s, 0) + 1

        rarete_spec = {}
        for s, nb_agents in offre_specs.items():
            nb_postes = demande_specs.get(s, 0)
            if nb_postes == 0 or nb_agents == 0:
                rarete_spec[s] = 0
            else:
                score = (60.0 / nb_agents) + (40.0 * (nb_postes / nb_agents))
                rarete_spec[s] = max(4, min(150, int(round(score))))

        rarete_spec["Tout agent"] = 1

        capital_agent = []
        for a in agents:
            scores_utiles = sorted(
                [rarete_spec.get(s, 0) for s in set(a["specs"]) if rarete_spec.get(s, 0) > 0],
                reverse=True
            )
            if not scores_utiles:
                capital_agent.append(0)
            else:
                max_r = scores_utiles[0]
                reste_r = sum(scores_utiles[1:])
                capital_agent.append(int(round(max_r + 0.2 * reste_r)))

        # =====================================================================
        # 4. VARIABLES DE DÉCISION & CONTRAINTES DE POSTES
        # =====================================================================
        x = {}
        for v_idx, v in enumerate(vehicules):
            for p_idx, b in enumerate(v["besoins"]):
                for a_idx, a in enumerate(agents):
                    if b == "Tout agent" or b in a["specs"]:
                        x[(v_idx, p_idx, a_idx)] = model.NewBoolVar(f"x_{v_idx}_{p_idx}_{a_idx}")

        u = [model.NewBoolVar(f"u_{v_idx}") for v_idx in range(len(vehicules))]

        for v_idx, v in enumerate(vehicules):
            for p_idx in range(len(v["besoins"])):
                candidats = [x[(v_idx, p_idx, a_idx)] for a_idx in range(len(agents)) if (v_idx, p_idx, a_idx) in x]
                if candidats:
                    model.Add(sum(candidats) == u[v_idx])
                else:
                    model.Add(u[v_idx] == 0)

            for a_idx in range(len(agents)):
                postes_agent = [x[(v_idx, p_idx, a_idx)] for p_idx in range(len(v["besoins"])) if (v_idx, p_idx, a_idx) in x]
                if postes_agent:
                    model.Add(sum(postes_agent) <= 1)

        is_on_veh = {}
        for a_idx in range(len(agents)):
            for v_idx in range(len(vehicules)):
                postes = [x[(v_idx, p_idx, a_idx)] for p_idx in range(len(vehicules[v_idx]["besoins"])) if (v_idx, p_idx, a_idx) in x]
                var = model.NewBoolVar(f"on_{a_idx}_{v_idx}")
                if postes:
                    model.Add(sum(postes) == var)
                else:
                    model.Add(var == 0)
                is_on_veh[(a_idx, v_idx)] = var

        # =====================================================================
        # 5. VÉHICULES JUMEAUX (G1 à G99)
        # =====================================================================
        for grp_id, v_indices in groupes_jumeaux.items():
            if len(v_indices) >= 2:
                v_ref = v_indices[0]
                for other_v in v_indices[1:]:
                    model.Add(u[v_ref] == u[other_v])
                    nb_postes = min(len(vehicules[v_ref]["besoins"]), len(vehicules[other_v]["besoins"]))
                    for p_idx in range(nb_postes):
                        for a_idx in range(len(agents)):
                            var_ref = x.get((v_ref, p_idx, a_idx))
                            var_other = x.get((other_v, p_idx, a_idx))
                            if var_ref is not None and var_other is not None:
                                model.Add(var_ref == var_other)

        # =====================================================================
        # 6. RÈGLE PORTEUR & CELLULES (VPCE et Ce...)
        # =====================================================================
        indices_vpce = [i for i, v in enumerate(vehicules) if v["nom"].upper().startswith("VPCE")]
        indices_cellules = [i for i, v in enumerate(vehicules) if v["nom"].upper().startswith("CE")]

        for vpce_idx in indices_vpce:
            for ce_idx in indices_cellules:
                for a_idx in range(len(agents)):
                    model.Add(is_on_veh[(a_idx, vpce_idx)] + is_on_veh[(a_idx, ce_idx)] <= 1)

        # =====================================================================
        # 7. FONCTION OBJECTIF HIÉRARCHISÉE (ARMEMENT > MOBILISATION > ÉTALEMENT)
        # =====================================================================
        termes_objectif = []

        # A. OBJECTIF N°1 ABSOLU : ARMER TOUS LES VÉHICULES POSSIBLES
        # Poids de 1 000 000 à 10 000 000 pts -> Aucune pénalité ne peut jamais empêcher d'armer un engin !
        for v_idx, v in enumerate(vehicules):
            score_rarete_engin = sum(rarete_spec.get(b, 1) for b in v["besoins"])
            if v["prioritaire"]:
                poids = 10000000 - min(v["rangPrio"] * 50000, 4000000) + (score_rarete_engin * 10)
            else:
                poids = 1000000 + (score_rarete_engin * 10)
            termes_objectif.append(u[v_idx] * poids)

        # Identification des unités d'armement distinctes (un groupe jumeau G1 = 1 seule unité)
        vehicules_secondaires_jumeaux = set()
        for grp_id, v_indices in groupes_jumeaux.items():
            if len(v_indices) >= 2:
                for other_v in v_indices[1:]:
                    vehicules_secondaires_jumeaux.add(other_v)
        unites_vehicules = [v_idx for v_idx in range(len(vehicules)) if v_idx not in vehicules_secondaires_jumeaux]
        unites_prioritaires = [v_idx for v_idx in unites_vehicules if vehicules[v_idx]["prioritaire"]]

        # B. OBJECTIF N°2 : UTILISER UN MAXIMUM D'AGENTS DISPONIBLES & ÉVITER LES DOUBLONS
        for a_idx in range(len(agents)):
            tot = sum(is_on_veh[(a_idx, v_idx)] for v_idx in unites_vehicules)

            ge_1 = model.NewBoolVar(f"ge_1_{a_idx}")
            ge_2 = model.NewBoolVar(f"ge_2_{a_idx}")
            ge_3 = model.NewBoolVar(f"ge_3_{a_idx}")
            ge_4 = model.NewBoolVar(f"ge_4_{a_idx}")
            ge_5 = model.NewBoolVar(f"ge_5_{a_idx}")

            model.Add(tot >= 1).OnlyEnforceIf(ge_1)
            model.Add(tot < 1).OnlyEnforceIf(ge_1.Not())
            model.Add(tot >= 2).OnlyEnforceIf(ge_2)
            model.Add(tot < 2).OnlyEnforceIf(ge_2.Not())
            model.Add(tot >= 3).OnlyEnforceIf(ge_3)
            model.Add(tot < 3).OnlyEnforceIf(ge_3.Not())
            model.Add(tot >= 4).OnlyEnforceIf(ge_4)
            model.Add(tot < 4).OnlyEnforceIf(ge_4.Not())
            model.Add(tot >= 5).OnlyEnforceIf(ge_5)
            model.Add(tot < 5).OnlyEnforceIf(ge_5.Not())

            # Protection forte des véhicules prioritaires :
            # Si l'agent est sur un véhicule prioritaire, on évite à tout prix (-150 000 pts)
            # de le mettre sur un 2e véhicule, SAUF si c'est indispensable pour armer un véhicule (+1 000 000 pts) !
            if unites_prioritaires:
                sur_prio = model.NewBoolVar(f"sur_prio_{a_idx}")
                somme_prio = sum(is_on_veh[(a_idx, v_p)] for v_p in unites_prioritaires)
                model.Add(somme_prio >= 1).OnlyEnforceIf(sur_prio)
                model.Add(somme_prio == 0).OnlyEnforceIf(sur_prio.Not())

                doublon_prio = model.NewBoolVar(f"doublon_prio_{a_idx}")
                model.AddBoolAnd([sur_prio, ge_2]).OnlyEnforceIf(doublon_prio)
                model.AddBoolOr([sur_prio.Not(), ge_2.Not()]).OnlyEnforceIf(doublon_prio.Not())
                termes_objectif.append(-150000 * doublon_prio)

            malus_rarete_doublon = min(200, capital_agent[a_idx] * 2)

            # GROS BONUS (+50 000 pts) dès qu'un agent disponible est utilisé sur au moins 1 véhicule :
            # Laisser un agent au repos alors qu'il peut prendre un poste fait perdre 50 000 pts !
            termes_objectif.append(50000 * ge_1)

            # Pénalités progressives de cumul (2, 3, 4, 5 véhicules)
            termes_objectif.append((-5000 - malus_rarete_doublon) * ge_2)
            termes_objectif.append(-15000 * ge_3)
            termes_objectif.append(-35000 * ge_4)
            termes_objectif.append(-75000 * ge_5)

        # C. OBJECTIF N°3 : ADÉQUATION DES COMPÉTENCES RARES SUR LES POSTES
        for (v_idx, p_idx, a_idx), var_x in x.items():
            b = vehicules[v_idx]["besoins"][p_idx]
            val_requise = rarete_spec.get(b, 1)

            gaspillage = max(0, capital_agent[a_idx] - val_requise)
            penalite_surqualification = min(150, gaspillage)

            score_adequation = (val_requise * 2) - penalite_surqualification
            termes_objectif.append(var_x * score_adequation)

        model.Maximize(sum(termes_objectif))

        # =====================================================================
        # 8. RÉSOLUTION RAPIDE MULTITHREAD
        # =====================================================================
        solver = cp_model.CpSolver()
        solver.parameters.max_time_in_seconds = 10.0
        solver.parameters.num_search_workers = 4
        status = solver.Solve(model)

        resultats = []
        for v_idx, v in enumerate(vehicules):
            arme = bool(solver.Value(u[v_idx])) if (status == cp_model.OPTIMAL or status == cp_model.FEASIBLE) else False
            equipage = []
            for p_idx, b in enumerate(v["besoins"]):
                assigne = "MANQUANT"
                if arme:
                    for a_idx, a in enumerate(agents):
                        if (v_idx, p_idx, a_idx) in x and solver.Value(x[(v_idx, p_idx, a_idx)]) == 1:
                            assigne = a["nom"]
                            break
                equipage.append({"specialite": b, "agent": assigne})

            resultats.append({
                "nom": v["nom"],
                "arme": arme,
                "equipage": equipage
            })

        return JSONResponse(
            content={"status": "ok", "vehicules": resultats},
            headers=CORS_HEADERS
        )

    except Exception as e:
        return JSONResponse(
            status_code=200,
            content={"status": "erreur", "message": str(e), "trace": traceback.format_exc()},
            headers=CORS_HEADERS
        )
