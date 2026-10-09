from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Dict, Any
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

    # Détection des groupes et priorités
    # Si la priorité contient "groupe" ou "g" suivi d'un id (ex: "Groupe 1", "G1")
    groupes_jumeaux = {}
    for v_idx, v in enumerate(vehicules):
        prio_str = str(v.get("priorite", "")).strip().lower()
        m = re.match(r'^(groupe\s*\d+|g\d+)$', prio_str)
        if m:
            grp_id = m.group(1).replace(" ", "")
            if grp_id not in groupes_jumeaux:
                groupes_jumeaux[grp_id] = []
            groupes_jumeaux[grp_id].append(v_idx)

    # Variables de décision
    # x[v, p, a] = 1 si l'agent a occupe le poste p sur l'engin v
    x = {}
    for v_idx, v in enumerate(vehicules):
        for p_idx, b in enumerate(v["besoins"]):
            for a_idx, a in enumerate(agents):
                if b == "Tout agent" or b in a["specs"]:
                    x[(v_idx, p_idx, a_idx)] = model.NewBoolVar(f"x_{v_idx}_{p_idx}_{a_idx}")

    # u[v] = 1 si l'engin est armé
    u = [model.NewBoolVar(f"u_{v_idx}") for v_idx in range(len(vehicules))]

    # 1. Contraintes de postes
    for v_idx, v in enumerate(vehicules):
        for p_idx in range(len(v["besoins"])):
            candidats = [x[(v_idx, p_idx, a_idx)] for a_idx in range(len(agents)) if (v_idx, p_idx, a_idx) in x]
            model.Add(sum(candidats) == u[v_idx])

        # 1 agent = max 1 poste sur le même véhicule
        for a_idx in range(len(agents)):
            postes_agent = [x[(v_idx, p_idx, a_idx)] for p_idx in range(len(v["besoins"])) if (v_idx, p_idx, a_idx) in x]
            model.Add(sum(postes_agent) <= 1)

    # 2. Présence d'un agent sur un véhicule
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

    # 3. Règle d'exclusivité des prioritaires réels (1, 2, 3...)
    for v_idx, v in enumerate(vehicules):
        if v.get("prioritaire"):
            for a_idx in range(len(agents)):
                autres = [is_on_veh[(a_idx, other_v)] for other_v in range(len(vehicules)) if other_v != v_idx]
                model.Add(sum(autres) == 0).OnlyEnforceIf(is_on_veh[(a_idx, v_idx)])

    # 4. RÈGLE JUMEAUX (Groupe 1, Groupe 2...)
    # Les véhicules d'un même groupe ont strictement le même équipage
    for grp_id, v_indices in groupes_jumeaux.items():
        if len(v_indices) >= 2:
            v_ref = v_indices[0]
            for other_v in v_indices[1:]:
                # Ils sont armés ensemble
                model.Add(u[v_ref] == u[other_v])
                
                # S'ils ont les mêmes postes, forcer les mêmes agents poste par poste
                nb_postes = min(len(vehicules[v_ref]["besoins"]), len(vehicules[other_v]["besoins"]))
                for p_idx in range(nb_postes):
                    for a_idx in range(len(agents)):
                        var_ref = x.get((v_ref, p_idx, a_idx))
                        var_other = x.get((other_v, p_idx, a_idx))
                        if var_ref is not None and var_other is not None:
                            model.Add(var_ref == var_other)

    # 5. RÈGLE PORTEUR ET CELLULES (VPCE et Ce...)
    # Un agent ne peut PAS être à la fois sur le VPCE et sur une cellule Ce...
    indices_vpce = [i for i, v in enumerate(vehicules) if v["nom"].upper().startswith("VPCE")]
    indices_cellules = [i for i, v in enumerate(vehicules) if v["nom"].upper().startswith("CE")]

    for vpce_idx in indices_vpce:
        for ce_idx in indices_cellules:
            for a_idx in range(len(agents)):
                model.Add(is_on_veh[(a_idx, vpce_idx)] + is_on_veh[(a_idx, ce_idx)] <= 1)

    # 6. Fonction Objectif
    termes_objectif = []
    for v_idx, v in enumerate(vehicules):
        if v.get("prioritaire"):
            poids = 100000 - min(v.get("rangPrio", 1) * 1000, 50000)
        else:
            poids = 10000
        termes_objectif.append(u[v_idx] * poids)

    # Pénalité légère pour limiter le multi-armement inutile
    for a_idx in range(len(agents)):
        total_veh = sum(is_on_veh[(a_idx, v_idx)] for v_idx in range(len(vehicules)))
        termes_objectif.append(-5 * total_veh)

    model.Maximize(sum(termes_objectif))

    # Résolution
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
