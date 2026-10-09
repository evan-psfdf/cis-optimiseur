from fastapi import FastAPI
from pydantic import BaseModel
from typing import List, Dict, Any
from ortools.sat.python import cp_model

app = FastAPI()

from fastapi.middleware.cors import CORSMiddleware

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

    # x[v, p, a] = 1 si l'agent a occupe le poste p sur le véhicule v
    x = {}
    for v_idx, v in enumerate(vehicules):
        for p_idx, b in enumerate(v["besoins"]):
            for a_idx, a in enumerate(agents):
                if b == "Tout agent" or b in a["specs"]:
                    x[(v_idx, p_idx, a_idx)] = model.NewBoolVar(f"x_{v_idx}_{p_idx}_{a_idx}")

    # u[v] = 1 si le véhicule est armé
    u = [model.NewBoolVar(f"u_{v_idx}") for v_idx in range(len(vehicules))]

    # 1. Contraintes de postes
    for v_idx, v in enumerate(vehicules):
        for p_idx in range(len(v["besoins"])):
            candidats = [x[(v_idx, p_idx, a_idx)] for a_idx in range(len(agents)) if (v_idx, p_idx, a_idx) in x]
            model.Add(sum(candidats) == u[v_idx])

        # 1 personne = max 1 poste sur un même engin
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

    # 3. Règle d'exclusivité stricte pour les prioritaires
    for v_idx, v in enumerate(vehicules):
        if v.get("prioritaire"):
            for a_idx in range(len(agents)):
                autres = [is_on_veh[(a_idx, other_v)] for other_v in range(len(vehicules)) if other_v != v_idx]
                # Si l'agent est sur un véhicule prioritaire, il ne peut être sur aucun autre
                model.Add(sum(autres) == 0).OnlyEnforceIf(is_on_veh[(a_idx, v_idx)])

    # 4. Fonction Objectif (Maximisation globale)
    termes_objectif = []

    for v_idx, v in enumerate(vehicules):
        if v.get("prioritaire"):
            # Les prioritaires rapportent entre 50 000 et 100 000 points selon leur rang
            poids = 100000 - min(v.get("rangPrio", 1) * 1000, 50000)
        else:
            # Poids positif pour armer tous les autres véhicules
            poids = 10000
        termes_objectif.append(u[v_idx] * poids)

    # Pénalité légère pour le multi-armement : limite le nombre de véhicules par agent
    for a_idx in range(len(agents)):
        total_veh = sum(is_on_veh[(a_idx, v_idx)] for v_idx in range(len(vehicules)))
        termes_objectif.append(-10 * total_veh)

    model.Maximize(sum(termes_objectif))

    # Résolution mathématique
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
