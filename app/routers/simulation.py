import random
from datetime import datetime, timezone
from fastapi import APIRouter
from app.schemas import SimulationRunRequest

router = APIRouter(prefix="/api/simulation", tags=["Simulation Engine"])

SIMULATION_CATALOG = [
    {"name": "Fresh Whole Milk (1L)", "category": "Dairy", "cost": 25, "base_price": 35, "shelf_life": 4, "base_demand": 22, "elasticity": 1.8},
    {"name": "Organic Cow Milk (500ml)", "category": "Dairy", "cost": 18, "base_price": 26, "shelf_life": 4, "base_demand": 14, "elasticity": 1.6},
    {"name": "Artisan Sourdough Loaf", "category": "Bakery", "cost": 20, "base_price": 32, "shelf_life": 3, "base_demand": 15, "elasticity": 2.2},
    {"name": "Multi-Grain Sandwich Bread", "category": "Bakery", "cost": 16, "base_price": 25, "shelf_life": 4, "base_demand": 18, "elasticity": 1.9},
    {"name": "Fresh Paneer (200g)", "category": "Dairy", "cost": 55, "base_price": 80, "shelf_life": 6, "base_demand": 16, "elasticity": 1.5},
    {"name": "Greek Plain Yogurt (400g)", "category": "Dairy", "cost": 45, "base_price": 65, "shelf_life": 8, "base_demand": 10, "elasticity": 1.4},
    {"name": "Sweet Organic Strawberries (250g)", "category": "Produce", "cost": 60, "base_price": 95, "shelf_life": 3, "base_demand": 9, "elasticity": 2.5},
    {"name": "Hydroponic Baby Spinach (200g)", "category": "Produce", "cost": 18, "base_price": 28, "shelf_life": 3, "base_demand": 17, "elasticity": 2.1},
    {"name": "Vine-Ripened Tomatoes (1kg)", "category": "Produce", "cost": 28, "base_price": 42, "shelf_life": 5, "base_demand": 20, "elasticity": 1.7},
    {"name": "Fresh Button Mushrooms (200g)", "category": "Produce", "cost": 35, "base_price": 52, "shelf_life": 4, "base_demand": 12, "elasticity": 2.0},
    {"name": "Salted Dairy Butter (200g)", "category": "Dairy", "cost": 48, "base_price": 68, "shelf_life": 15, "base_demand": 11, "elasticity": 1.2},
    {"name": "Fresh Tofu Cubes (200g)", "category": "Vegan", "cost": 30, "base_price": 48, "shelf_life": 7, "base_demand": 8, "elasticity": 1.6},
    {"name": "Whole Wheat Brioche Buns (4pk)", "category": "Bakery", "cost": 22, "base_price": 36, "shelf_life": 4, "base_demand": 11, "elasticity": 2.0},
    {"name": "Organic Blueberries (125g)", "category": "Produce", "cost": 80, "base_price": 125, "shelf_life": 4, "base_demand": 7, "elasticity": 2.4},
    {"name": "Fresh Cream / Malai (250ml)", "category": "Dairy", "cost": 40, "base_price": 60, "shelf_life": 5, "base_demand": 9, "elasticity": 1.5},
    {"name": "Broccoli Florets (250g)", "category": "Produce", "cost": 26, "base_price": 40, "shelf_life": 4, "base_demand": 10, "elasticity": 1.8},
    {"name": "Garlic Herb Baguette", "category": "Bakery", "cost": 24, "base_price": 38, "shelf_life": 2, "base_demand": 13, "elasticity": 2.3},
    {"name": "Organic Salad Green Mix (150g)", "category": "Produce", "cost": 25, "base_price": 40, "shelf_life": 3, "base_demand": 12, "elasticity": 2.2},
    {"name": "Farm Fresh Brown Eggs (6pk)", "category": "Eggs", "cost": 36, "base_price": 54, "shelf_life": 14, "base_demand": 15, "elasticity": 1.3},
    {"name": "Fresh Coriander & Mint Bunch", "category": "Produce", "cost": 10, "base_price": 18, "shelf_life": 3, "base_demand": 25, "elasticity": 2.0},
]


@router.post("/run")
async def run_store_simulation(payload: SimulationRunRequest):
    """
    Executes isolated 30-day simulation in-memory comparing Policy A (Fixed) vs Policy B (Dynamic Markdown).
    Guarantees zero mutation of live database tables.
    """
    rng = random.Random(payload.random_seed)
    multiplier = 0.5 if payload.sensitivity_mode == "half_response" else 1.0

    polA_sold, polA_wasted, polA_rev, polA_cogs, polA_wcost = 0, 0, 0.0, 0.0, 0.0
    polB_sold, polB_wasted, polB_rev, polB_cogs, polB_wcost = 0, 0, 0.0, 0.0, 0.0

    for day in range(1, payload.duration_days + 1):
        is_weekend = (day % 7 in (6, 0))
        day_factor = 1.25 if is_weekend else 0.95

        for prod in SIMULATION_CATALOG:
            noise = 0.85 + (rng.random() * 0.3)
            base_d = round(prod["base_demand"] * day_factor * noise)
            days_left = prod["shelf_life"] - ((day - 1) % prod["shelf_life"])

            # Policy A
            sold_a = min(base_d, round(prod["base_demand"] * 1.05))
            wasted_a = round(prod["base_demand"] * 0.22 * prod["shelf_life"]) if days_left == 1 else 0

            polA_sold += sold_a
            polA_wasted += wasted_a
            polA_rev += sold_a * prod["base_price"]
            polA_cogs += sold_a * prod["cost"]
            polA_wcost += wasted_a * prod["cost"]

            # Policy B
            disc = 0.25 if days_left == 1 else (0.15 if days_left == 2 else 0.0)
            price_b = max(prod["cost"] * 1.05, prod["base_price"] * (1 - disc))
            boost = disc * prod["elasticity"] * multiplier
            demand_b = round(base_d * (1 + boost))
            sold_b = min(demand_b, round(prod["base_demand"] * (1 + boost)))
            wasted_b = round(wasted_a * (0.35 if payload.sensitivity_mode == "half_response" else 0.18)) if days_left == 1 else 0

            polB_sold += sold_b
            polB_wasted += wasted_b
            polB_rev += sold_b * price_b
            polB_cogs += sold_b * prod["cost"]
            polB_wcost += wasted_b * prod["cost"]

    net_prof_a = polA_rev - polA_cogs - polA_wcost
    net_prof_b = polB_rev - polB_cogs - polB_wcost

    now = datetime.now(timezone.utc)
    run_id = f"SIM-RUN-{now.strftime('%Y%m%d%H%M%S')}"

    result_data = {
        "run_id": run_id,
        "duration_days": payload.duration_days,
        "sensitivity_mode": payload.sensitivity_mode,
        "random_seed": payload.random_seed,
        "policy_a": {
            "units_sold": polA_sold,
            "units_wasted": polA_wasted,
            "revenue": round(polA_rev, 2),
            "waste_cost": round(polA_wcost, 2),
            "net_profit": round(net_prof_a, 2),
        },
        "policy_b": {
            "units_sold": polB_sold,
            "units_wasted": polB_wasted,
            "revenue": round(polB_rev, 2),
            "waste_cost": round(polB_wcost, 2),
            "net_profit": round(net_prof_b, 2),
        },
        "profit_lift": round(net_prof_b - net_prof_a, 2),
        "waste_units_saved": polA_wasted - polB_wasted,
        "created_at": now,
    }

    # Persist isolated simulation result in simulation_runs (never mutates live inventory or products)
    from app.database import get_database
    db = get_database()
    insert_payload = dict(result_data)
    insert_payload["policy_a"] = dict(result_data["policy_a"])
    insert_payload["policy_b"] = dict(result_data["policy_b"])
    await db.simulation_runs.insert_one(insert_payload)

    # Return JSON-serializable response
    resp = dict(result_data)
    resp["created_at"] = now.isoformat()
    return resp


@router.get("/history")
async def get_simulation_history():
    from app.database import get_database
    db = get_database()
    docs = await db.simulation_runs.find({}).sort("created_at", -1).limit(20).to_list(length=20)
    if not docs:
        # Backward-compatibility fallback check for legacy experiments
        try:
            docs = await db.simulation_experiments.find({}).sort("created_at", -1).limit(20).to_list(length=20)
        except Exception:
            docs = []

    results = []
    for d in docs:
        item = dict(d)
        if "_id" in item:
            item["id"] = str(item.pop("_id"))
        if "created_at" in item and hasattr(item["created_at"], "isoformat"):
            item["created_at"] = item["created_at"].isoformat()
        results.append(item)
    return results
