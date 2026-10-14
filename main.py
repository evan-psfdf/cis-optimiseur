from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any
from ortools.sat.python import cp_model
from collections import Counter
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class RequeteGarde(BaseModel):
    agents: List[Dict[str, Any]]
    vehicules: List[Dict[str, Any]]

def est_engin_faisable(v: Dict[str, Any], agents: List[Dict[str, Any]]) -> bool:
    """Vérifie si l'effectif présent permet mathématiquement d'armer cet engin."""
    besoins = v.get("besoins", [])
    if not besoins:
        return len(agents) >= 1

    # 1. Vérifier qu'il y a assez d'agents distincts éligibles au total
    agents_eligibles = set()
    for a_idx, a in enumerate(agents):
        specs_a = a.get("specs", [])
        if any(b == "Tout agent" or b in specs_a for b in besoins):
            agents_eligibles.add(a_idx)
    if len(agents_eligibles) < len(besoins):
        return False

    # 2. Vérifier chaque spécialité requise par l'engin
    compte_besoins = Counter(b for b in besoins if b != "Tout agent")
    for spec, nb_requis in compte_besoins.items():
        nb_dispo = sum(1 for a in agents if spec in a.get("specs", []))
        if nb_dispo < nb_requis:
            return False

    return True

@app.get("/")
def home():
    return {"status": "en ligne", "service": "CIS Optimiseur OR-Tools v4"}

@app.post("/resoudre")
def resoudre(data: RequeteGarde):
    agents = data.agents
    vehicules = data.vehicules

    model = cp_model.CpModel()

    # =========================================================================
    # 1. DÉTECTION DES GROUPES JUMEAUX (ex: "G1", "Groupe 1", "G2"...)
    # =========================================================================
    groupes_jumeaux = {}
    vehicule_vers_groupe = {}
    for v_idx, v in enumerate(vehicules):
        prio_str = str(v.get("priorite", "")).strip().lower()
        m = re.match(r'^(groupe\s*\d+|g\d+)$', prio_str)
        if m:
            grp_id = m.group(1).replace(" ", "")
            if grp_id not in groupes_jumeaux:
                groupes_jumeaux[grp_id] = []
            groupes_jumeaux[grp_id].append(v_idx)
            vehicule_vers_groupe[v_idx] = grp_id

    # =========================================================================
    # 2. PRÉ-FILTRAGE DES VÉHICULES FAISABLES AUJOURD'HUI
    # =========================================================================
    # Si un véhicule (ex: VIMP) manque d'effectif pour partir, on ne bloque pas
    # ses spécialistes avec un score de rareté artificiel.
    vehicules_faisables = {v_idx: est_engin_faisable(v, agents) for v_idx, v in enumerate(vehicules)}
    for grp_id, v_indices in groupes_jumeaux.items():
        if not all(vehicules_faisables[i] for i in v_indices):
            for i in v_indices:
                vehicules_faisables[i] = False

    # =========================================================================
    # 3. CALCUL DE LA RARETÉ UTILE (SUR LES SEULS ENGINS ARMABLES)
    # =========================================================================
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

        for b in v.get("besoins", []):
            if b and b != "Tout agent":
                demande_specs[b] = demande_specs.get(b, 0) + 1

    offre_specs = {}
    for a in agents:
        for s in set(a.get("specs", [])):
            offre_specs[s] = offre_specs.get(s, 0) + 1

    rarete_spec = {}
    for s, nb_agents in offre_specs.items():
        nb_postes = demande_specs.get(s, 0)
        if nb_postes == 0:
            rarete_spec[s] = 0
        else:
            score = (60.0 / nb_agents) + (40.0 * (nb_postes / nb_agents))
            rarete_spec[s] = max(4, min(150, int(round(score))))

    rarete_spec["Tout agent"] = 1

    # Capital de rareté dominé par la spécialité LA PLUS RARE de l'agent (+ 20% des autres)
    # Évite que 8 compétences courantes ne dépassent 1 compétence rare.
    capital_agent = []
    for a in agents:
        scores_utiles = sorted(
            [rarete_spec.get(s, 0) for s in set(a.get("specs", [])) if rarete_spec.get(s, 0) > 0],
            reverse=True
        )
        if not scores_utiles:
            capital_agent.append(0)
        else:
            max_r = scores_utiles[0]
            reste_r = sum(scores_utiles[1:])
            capital_agent.append(int(round(max_r + 0.2 * reste_r)))

    # =========================================================================
    # 4. VARIABLES DE DÉCISION & CONTRAINTES D'ARMEMENT
    # =========================================================================
    x = {}
    for v_idx, v in enumerate(vehicules):
        for p_idx, b in enumerate(v["besoins"]):
            for a_idx, a in enumerate(agents):
                if b == "Tout agent" or b in a.get("specs", []):
                    x[(v_idx, p_idx, a_idx)] = model.NewBoolVar(f"x_{v_idx}_{p_idx}_{a_idx}")

    u = [model.NewBoolVar(f"u_{v_idx}") for v_idx in range(len(vehicules))]

    for v_idx, v in enumerate(vehicules):
        for p_idx in range(len(v["besoins"])):
            candidats = [x[(v_idx, p_idx, a_idx)] for a_idx in range(len(agents)) if (v_idx, p_idx, a_idx) in x]
            model.Add(sum(candidats) == u[v_idx])

        for a_idx in range(len(agents)):
            postes_agent = [x[(v_idx, p_idx, a_idx)] for p_idx in range(len(v["besoins"])) if (v_idx, p_idx, a_idx) in x]
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

    # =========================================================================
    # 5. EXCLUSIVITÉ DES PRIORITAIRES STRICTS (1, 2, 3...)
    # =========================================================================
    for v_idx, v in enumerate(vehicules):
        if v.get("prioritaire"):
            for a_idx in range(len(agents)):
                autres = [is_on_veh[(a_idx, other_v)] for other_v in range(len(vehicules)) if other_v != v_idx]
                model.Add(sum(autres) == 0).OnlyEnforceIf(is_on_veh[(a_idx, v_idx)])

    # =========================================================================
    # 6. VÉHICULES JUMEAUX (G1, G2...)
    # =========================================================================
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

    # =========================================================================
    # 7. RÈGLE PORTEUR & CELLULES (VPCE et Ce...)
    # =========================================================================
    indices_vpce = [i for i, v in enumerate(vehicules) if v["nom"].upper().startswith("VPCE")]
    indices_cellules = [i for i, v in enumerate(vehicules) if v["nom"].upper().startswith("CE")]

    for vpce_idx in indices_vpce:
        for ce_idx in indices_cellules:
            for a_idx in range(len(agents)):
                model.Add(is_on_veh[(a_idx, vpce_idx)] + is_on_veh[(a_idx, ce_idx)] <= 1)

    # =========================================================================
    # 8. FONCTION OBJECTIF
    # =========================================================================
    termes_objectif = []

    # A. Armement des véhicules
    for v_idx, v in enumerate(vehicules):
        score_rarete_engin = sum(rarete_spec.get(b, 1) for b in v["besoins"])
        if v.get("prioritaire"):
            poids = 100000 - min(v.get("rangPrio", 1) * 1000, 50000) + (score_rarete_engin * 2)
        else:
            poids = 10000 + (score_rarete_engin * 2)
        termes_objectif.append(u[v_idx] * poids)

    # B. Pénalités de cumul par unité d'armement réelle (un groupe jumeau G1 = 1 seule unité)
    vehicules_secondaires_jumeaux = set()
    for grp_id, v_indices in groupes_jumeaux.items():
        if len(v_indices) >= 2:
            for other_v in v_indices[1:]:
                vehicules_secondaires_jumeaux.add(other_v)
    unites_vehicules = [v_idx for v_idx in range(len(vehicules)) if v_idx not in vehicules_secondaires_jumeaux]

    for a_idx in range(len(agents)):
        tot = sum(is_on_veh[(a_idx, v_idx)] for v_idx in unites_vehicules)

        ge_2 = model.NewBoolVar(f"ge_2_{a_idx}")
        ge_3 = model.NewBoolVar(f"ge_3_{a_idx}")
        ge_4 = model.NewBoolVar(f"ge_4_{a_idx}")
        ge_5 = model.NewBoolVar(f"ge_5_{a_idx}")

        model.Add(tot >= 2).OnlyEnforceIf(ge_2)
        model.Add(tot < 2).OnlyEnforceIf(ge_2.Not())

        model.Add(tot >= 3).OnlyEnforceIf(ge_3)
        model.Add(tot < 3).OnlyEnforceIf(ge_3.Not())

        model.Add(tot >= 4).OnlyEnforceIf(ge_4)
        model.Add(tot < 4).OnlyEnforceIf(ge_4.Not())

        model.Add(tot >= 5).OnlyEnforceIf(ge_5)
        model.Add(tot < 5).OnlyEnforceIf(ge_5.Not())

        malus_rarete_doublon = min(30, int(round(capital_agent[a_idx] / 10.0)))

        termes_objectif.append((-250 - malus_rarete_doublon) * ge_2)
        termes_objectif.append(-600 * ge_3)
        termes_objectif.append(-1500 * ge_4)
        termes_objectif.append(-4000 * ge_5)

    # C. Adéquation Poste / Compétence utile
    for (v_idx, p_idx, a_idx), var_x in x.items():
        b = vehicules[v_idx]["besoins"][p_idx]
        val_requise = rarete_spec.get(b, 1)

        gaspillage = max(0, capital_agent[a_idx] - val_requise)
        penalite_surqualification = min(50, int(round(gaspillage / 6.0)))

        score_adequation = int(round(val_requise / 3.0)) - penalite_surqualification
        termes_objectif.append(var_x * score_adequation)

    model.Maximize(sum(termes_objectif))

    # =========================================================================
    # 9. RÉSOLUTION
    # =========================================================================
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = 10.0
    status = solver.Solve(model)

    resultats = []
    for v_idx, v in enumerate(vehicules):
        arme = bool(solver.Value(u[v_idx]))
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

    return {"status": "ok", "vehicules": resultats}
