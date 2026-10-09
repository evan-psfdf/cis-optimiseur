from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any, Optional
from ortools.sat.python import cp_model
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

@app.get("/")
def home():
    return {"status": "en ligne", "service": "CIS Optimiseur OR-Tools"}

@app.post("/resoudre")
def resoudre(data: RequeteGarde):
    agents = data.agents
    vehicules = data.vehicules

    model = cp_model.CpModel()

    # 1. Détection des groupes jumeaux (ex: "G1", "Groupe 1", "G2"...)
    groupes_jumeaux = {}
    for v_idx, v in enumerate(vehicules):
        prio_str = str(v.get("priorite", "")).strip().lower()
        m = re.match(r'^(groupe\s*\d+|g\d+)$', prio_str)
        if m:
            grp_id = m.group(1).replace(" ", "")
            if grp_id not in groupes_jumeaux:
                groupes_jumeaux[grp_id] = []
            groupes_jumeaux[grp_id].append(v_idx)

    # 2. Variables de décision
    # x[v, p, a] = 1 si l'agent a occupe le poste p sur le véhicule v
    x = {}
    for v_idx, v in enumerate(vehicules):
        for p_idx, b in enumerate(v["besoins"]):
            for a_idx, a in enumerate(agents):
                if b == "Tout agent" or b in a.get("specs", []):
                    x[(v_idx, p_idx, a_idx)] = model.NewBoolVar(f"x_{v_idx}_{p_idx}_{a_idx}")

    # u[v] = 1 si le véhicule est armé
    u = [model.NewBoolVar(f"u_{v_idx}") for v_idx in range(len(vehicules))]

    # 3. Contraintes des postes d'armement
    for v_idx, v in enumerate(vehicules):
        for p_idx in range(len(v["besoins"])):
            candidats = [x[(v_idx, p_idx, a_idx)] for a_idx in range(len(agents)) if (v_idx, p_idx, a_idx) in x]
            model.Add(sum(candidats) == u[v_idx])

        # 1 agent = maximum 1 poste sur un même véhicule
        for a_idx in range(len(agents)):
            postes_agent = [x[(v_idx, p_idx, a_idx)] for p_idx in range(len(v["besoins"])) if (v_idx, p_idx, a_idx) in x]
            model.Add(sum(postes_agent) <= 1)

    # 4. Présence globale d'un agent sur un véhicule
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

    # 5. Règle d'exclusivité des prioritaires (1, 2, 3...)
    # Si un véhicule est prioritaire strict, son équipage ne monte sur aucun autre engin
    for v_idx, v in enumerate(vehicules):
        if v.get("prioritaire"):
            for a_idx in range(len(agents)):
                autres = [is_on_veh[(a_idx, other_v)] for other_v in range(len(vehicules)) if other_v != v_idx]
                model.Add(sum(autres) == 0).OnlyEnforceIf(is_on_veh[(a_idx, v_idx)])

    # 6. Règle des véhicules jumeaux (G1, G2...)
    for grp_id, v_indices in groupes_jumeaux.items():
        if len(v_indices) >= 2:
            v_ref = v_indices[0]
            for other_v in v_indices[1:]:
                # Armés ensemble ou aucun des deux
                model.Add(u[v_ref] == u[other_v])

                # Même équipage imposé sur chaque poste commun
                nb_postes = min(len(vehicules[v_ref]["besoins"]), len(vehicules[other_v]["besoins"]))
                for p_idx in range(nb_postes):
                    for a_idx in range(len(agents)):
                        var_ref = x.get((v_ref, p_idx, a_idx))
                        var_other = x.get((other_v, p_idx, a_idx))
                        if var_ref is not None and var_other is not None:
                            model.Add(var_ref == var_other)

    # 7. Règle Porteur & Cellules (VPCE et Ce...)
    # Un agent ne peut pas conduire le VPCE et armer sa cellule portée
    indices_vpce = [i for i, v in enumerate(vehicules) if v["nom"].upper().startswith("VPCE")]
    indices_cellules = [i for i, v in enumerate(vehicules) if v["nom"].upper().startswith("CE")]

    for vpce_idx in indices_vpce:
        for ce_idx in indices_cellules:
            for a_idx in range(len(agents)):
                model.Add(is_on_veh[(a_idx, vpce_idx)] + is_on_veh[(a_idx, ce_idx)] <= 1)

    # 8. Fonction Objectif (Maximisation & Pénalités progressives de cumul)
    termes_objectif = []

    # Gain principal : armer un maximum d'engins en respectant le rang de priorité
    for v_idx, v in enumerate(vehicules):
        if v.get("prioritaire"):
            poids = 100000 - min(v.get("rangPrio", 1) * 1000, 50000)
        else:
            poids = 10000
        termes_objectif.append(u[v_idx] * poids)

    # Pénalités par paliers de surcharge par agent
    for a_idx in range(len(agents)):
        tot = sum(is_on_veh[(a_idx, v_idx)] for v_idx in range(len(vehicules)))

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

        termes_objectif.append(-100 * ge_2)
        termes_objectif.append(-300 * ge_3)
        termes_objectif.append(-1000 * ge_4)
        termes_objectif.append(-3000 * ge_5)

    model.Maximize(sum(termes_objectif))

    # 9. Résolution
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
