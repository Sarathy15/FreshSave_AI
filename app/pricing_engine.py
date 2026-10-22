"""
Batch-Wise Intelligent Pricing Engine for FreshSave AI
Upgraded explainable multi-strategy classification and pricing algorithm.

Every inventory batch is evaluated independently across five distinct strategies:
1. Near-Expiry Markdown (waste reduction for urgent expiry)
2. Stock Clearance (liquidation of substantial surplus inventory)
3. Maximum Profit (margin maximization when natural demand is strong)
4. Bulk & Bundles (multi-unit commercial packages for volume-friendly items)
5. Balanced Smart (optimal multi-objective trade-off between margin and velocity)

Key Rules & Policies:
- 60% shelf-life-elapsed eligibility threshold for expiry-based markdowns
- Configurable policy bands (< 24h critically urgent, 1-2 days, > 2 days, long shelf life)
- Real database records from MongoDB; no fabricated recommendations
- Strict financial guardrails (minimum margin floor, maximum discount cap)
- Consistent demand-response modeling (elasticity, stock/horizon capping)
- Transparent, calculation-backed explanations for selected and rejected strategies
"""

from datetime import datetime, date, timezone
from typing import Literal

StrategyMode = Literal["balanced", "near_expiry", "max_profit", "clearance", "bulk"]

STRATEGY_NAMES: dict[StrategyMode, str] = {
    "balanced": "Balanced Smart",
    "near_expiry": "Near-Expiry Markdown",
    "max_profit": "Maximum Profit",
    "clearance": "Stock Clearance",
    "bulk": "Bulk & Bundles",
}

CANDIDATE_DISCOUNTS = [0.0, 0.05, 0.08, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35]


def parse_date(date_val: str | date | datetime | None) -> date:
    """Parse string or datetime to date object safely."""
    if isinstance(date_val, datetime):
        return date_val.date()
    if isinstance(date_val, date):
        return date_val
    if isinstance(date_val, str) and date_val.strip():
        try:
            return datetime.strptime(date_val.strip().split("T")[0], "%Y-%m-%d").date()
        except ValueError:
            pass
    return datetime.now(timezone.utc).date()


def calculate_shelf_life_metrics(batch: dict, today: date | None = None, now: datetime | None = None) -> dict:
    """
    Calculate batch-specific shelf-life metrics:
    - Shelf-life elapsed % = ((Current date - Received date) / (Expiry date - Received date)) * 100
    - Exact hours remaining until expiry
    - Expiry status flags (< 24h, 1-2 days, > 2 days)
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if today is None:
        today = now.date()

    rec_date = parse_date(batch.get("received_date"))
    exp_date = parse_date(batch.get("expiry_date"))

    total_shelf_life_days = max(1, (exp_date - rec_date).days)
    days_remaining = (exp_date - today).days

    # Calculate precise hours remaining
    if days_remaining < 0:
        hours_remaining = 0
    elif days_remaining == 0:
        # Same day: remaining hours of today
        hours_remaining = max(0, 24 - now.hour)
    else:
        # Future days: days_remaining * 24 minus current hour offset
        hours_remaining = max(1, (days_remaining * 24) - now.hour)

    # Shelf-life elapsed percentage
    elapsed_days = (today - rec_date).days + (now.hour / 24.0)
    if days_remaining < 0 or (days_remaining == 0 and hours_remaining == 0):
        elapsed_pct = 100.0
    elif elapsed_days <= 0:
        elapsed_pct = 0.0
    else:
        elapsed_pct = round(min(100.0, max(0.0, (elapsed_days / total_shelf_life_days) * 100.0)), 1)

    qty_remaining = int(batch.get("quantity_remaining", 0))

    # Categorize status & eligibility
    is_expired = days_remaining < 0 or (days_remaining == 0 and hours_remaining == 0)
    is_depleted = qty_remaining <= 0
    is_under_24h = (not is_expired) and (hours_remaining <= 24)
    is_1_to_2_days = (not is_expired) and (24 < hours_remaining <= 48)

    if is_depleted:
        eligibility = "depleted"
        status_label = "Depleted Stock"
    elif is_expired:
        eligibility = "expired"
        status_label = "Expired — Sale Blocked"
    elif elapsed_pct < 60.0 and not is_under_24h:
        eligibility = "ineligible"
        status_label = "Normal Price — Not Yet Eligible"
    else:
        eligibility = "eligible"
        status_label = "Eligible for Pricing Evaluation"

    return {
        "received_date_str": rec_date.strftime("%Y-%m-%d"),
        "expiry_date_str": exp_date.strftime("%Y-%m-%d"),
        "total_shelf_life_days": total_shelf_life_days,
        "elapsed_days": round(elapsed_days, 1),
        "days_remaining": days_remaining,
        "hours_remaining": hours_remaining,
        "shelf_life_elapsed_pct": elapsed_pct,
        "eligibility_status": eligibility,
        "status_label": status_label,
        "is_eligible_for_markdown": eligibility == "eligible",
        "is_expired": is_expired,
        "is_depleted": is_depleted,
        "is_under_24h": is_under_24h,
        "is_1_to_2_days": is_1_to_2_days,
    }


def calculate_demand_and_surplus(batch: dict, product: dict, metrics: dict) -> dict:
    """
    Computes expected demand before expiry, surplus inventory, and surplus ratio.
    """
    stock = int(batch.get("quantity_remaining", 0))
    daily_demand = float(product.get("average_daily_demand", 10.0))

    hours_left = metrics["hours_remaining"]
    selling_horizon_days = max(0.05, round(hours_left / 24.0, 2))

    # Expected natural units sold before expiry without additional discount
    expected_natural_demand = round(daily_demand * selling_horizon_days, 1)

    # Projected surplus units under normal sales
    projected_surplus = max(0, int(round(stock - expected_natural_demand)))
    surplus_ratio = round(projected_surplus / stock, 3) if stock > 0 else 0.0

    # Configurable threshold: surplus >= 30% of stock AND at least 5 units
    has_substantial_surplus = (projected_surplus >= 5) and (surplus_ratio >= 0.30 or projected_surplus >= 20)

    return {
        "stock": stock,
        "daily_demand": daily_demand,
        "selling_horizon_days": selling_horizon_days,
        "expected_natural_demand": expected_natural_demand,
        "projected_surplus": projected_surplus,
        "surplus_ratio": surplus_ratio,
        "surplus_percentage": round(surplus_ratio * 100.0, 1),
        "has_substantial_surplus": has_substantial_surplus,
    }


def evaluate_candidate_price(
    discount_ratio: float,
    base_price: float,
    unit_cost: float,
    stock: int,
    expected_natural_demand: float,
    elasticity: float,
    min_margin_ratio: float,
    max_disc_ratio: float,
) -> dict:
    """
    Evaluates a candidate discount with consistent financial calculations:
    - Revenue = Expected units sold × Selling price
    - COGS = Expected units sold × Acquisition unit cost
    - Gross profit = Revenue − COGS
    - Caps expected sales by available stock and horizon
    - Enforces margin floor and discount cap
    """
    min_allowed_price = round(unit_cost * (1.0 + min_margin_ratio), 2)
    # If base price itself is below cost * (1+margin), min_allowed_price cannot exceed base_price
    if min_allowed_price > base_price:
        min_allowed_price = base_price

    # Cap candidate discount
    capped_discount = min(discount_ratio, max_disc_ratio)
    candidate_price = round(base_price * (1.0 - capped_discount), 2)

    # Enforce minimum margin floor
    guardrails_passed = True
    if candidate_price < min_allowed_price:
        candidate_price = min_allowed_price
        capped_discount = max(0.0, round((base_price - candidate_price) / base_price, 4))
        if candidate_price < min_allowed_price:
            guardrails_passed = False

    # Demand response modeling: Lift = 1.0 + (discount * elasticity)
    lift = 1.0 + (capped_discount * elasticity)
    boosted_demand = expected_natural_demand * lift

    # Cap projected sales by available physical stock
    proj_sold = min(stock, max(0, int(round(boosted_demand))))
    proj_waste = max(0, stock - proj_sold)

    revenue = round(proj_sold * candidate_price, 2)
    cogs = round(proj_sold * unit_cost, 2)
    gross_profit = round(revenue - cogs, 2)
    waste_cost = round(proj_waste * unit_cost, 2)
    net_outcome = round(gross_profit - waste_cost, 2)

    margin_pct = round(((candidate_price - unit_cost) / candidate_price) * 100.0, 1) if candidate_price > 0 else 0.0

    return {
        "discount_ratio": capped_discount,
        "discount_percentage": round(capped_discount * 100.0, 1),
        "price": candidate_price,
        "proj_sold": proj_sold,
        "proj_waste": proj_waste,
        "revenue": revenue,
        "cogs": cogs,
        "gross_profit": gross_profit,
        "waste_cost": waste_cost,
        "net_outcome": net_outcome,
        "margin_percentage": margin_pct,
        "guardrails_passed": guardrails_passed,
        "min_allowed_price": min_allowed_price,
    }


def evaluate_near_expiry_strategy(
    batch: dict,
    product: dict,
    metrics: dict,
    demand_info: dict,
    settings: dict,
) -> dict:
    """
    Evaluates Strategy A: NEAR-EXPIRY MARKDOWN
    Objective: Reduce food waste by selling eligible stock before expiry.
    """
    stock = demand_info["stock"]
    daily_demand = demand_info["daily_demand"]
    surplus = demand_info["projected_surplus"]
    hours_left = metrics["hours_remaining"]
    elapsed_pct = metrics["shelf_life_elapsed_pct"]

    unit_cost = float(batch.get("unit_cost", product.get("unit_cost", 20.0)))
    base_price = float(product.get("base_price", unit_cost * 1.4))
    elasticity = float(product.get("price_sensitivity", 1.8))
    min_margin = float(settings.get("min_margin_percent", settings.get("min_margin", 5.0))) / 100.0
    max_disc = float(settings.get("max_discount_percent", settings.get("max_discount", 35.0))) / 100.0

    # Eligibility rules for Near-Expiry Markdown:
    # 1. Not expired
    # 2. Urgent window (<= 48h) OR 60% elapsed shelf life
    # 3. Urgency or surplus exists that risks waste
    is_eligible = (
        not metrics["is_expired"]
        and (hours_left <= 48 or elapsed_pct >= 60.0)
        and (surplus > 0 or hours_left <= 24)
    )

    if not is_eligible:
        reason = (
            f"Ineligible for Near-Expiry Markdown: Remaining freshness ({hours_left}h, {elapsed_pct}% elapsed) "
            f"has not reached the urgency threshold or has zero projected waste."
        )
        return {
            "strategy": "near_expiry",
            "name": STRATEGY_NAMES["near_expiry"],
            "eligible": False,
            "score": -9999.0,
            "reason": reason,
            "best_candidate": None,
        }

    # Evaluate candidate prices
    candidates = []
    # Urgency boosts allowable discount up to configured max
    discounts_to_try = [d for d in CANDIDATE_DISCOUNTS if d <= max_disc]
    for d in discounts_to_try:
        cand = evaluate_candidate_price(
            discount_ratio=d,
            base_price=base_price,
            unit_cost=unit_cost,
            stock=stock,
            expected_natural_demand=demand_info["expected_natural_demand"],
            elasticity=elasticity,
            min_margin_ratio=min_margin,
            max_disc_ratio=max_disc,
        )
        # Score formula: Heavily penalizes waste, strongly rewards cleared units before spoilage
        waste_penalty = 1.8 if hours_left <= 24 else 1.3
        cand["score"] = round(cand["gross_profit"] - (waste_penalty * cand["waste_cost"]) + (cand["proj_sold"] * 2.5), 2)
        candidates.append(cand)

    best = max(candidates, key=lambda c: c["score"])

    # Detailed explainable justification
    urgency_text = f"{hours_left} hours" if hours_left <= 48 else f"{metrics['days_remaining']} days"
    why = (
        f"Near-Expiry Markdown selected because this batch expires in {urgency_text} ({elapsed_pct}% shelf life elapsed), "
        f"{stock} units remain, and estimated demand before expiry is {demand_info['expected_natural_demand']} units "
        f"({surplus} surplus units at risk). A {best['discount_percentage']}% markdown to ₹{best['price']:.2f} "
        f"boosts expected sales to {best['proj_sold']} units, avoiding ₹{((best['proj_sold']) * unit_cost):.2f} in spoilage waste "
        f"while respecting the minimum margin floor (₹{best['min_allowed_price']:.2f})."
    )

    return {
        "strategy": "near_expiry",
        "name": STRATEGY_NAMES["near_expiry"],
        "eligible": True,
        "score": best["score"],
        "reason": why,
        "best_candidate": best,
        "evaluated_candidates": candidates,
    }


def evaluate_stock_clearance_strategy(
    batch: dict,
    product: dict,
    metrics: dict,
    demand_info: dict,
    settings: dict,
) -> dict:
    """
    Evaluates Strategy B: STOCK CLEARANCE
    Objective: Reduce excessive inventory when expected demand is insufficient to sell stock.
    Rules:
    - Identify substantial surplus (not based solely on total quantity, but surplus vs expected demand).
    - Distinguish expiry-driven clearance from ordinary overstock.
    """
    stock = demand_info["stock"]
    surplus = demand_info["projected_surplus"]
    surplus_pct = demand_info["surplus_percentage"]
    hours_left = metrics["hours_remaining"]

    unit_cost = float(batch.get("unit_cost", product.get("unit_cost", 20.0)))
    base_price = float(product.get("base_price", unit_cost * 1.4))
    elasticity = float(product.get("price_sensitivity", 1.8))
    min_margin = float(settings.get("min_margin_percent", settings.get("min_margin", 5.0))) / 100.0
    max_disc = float(settings.get("max_discount_percent", settings.get("max_discount", 35.0))) / 100.0

    # Eligibility rules:
    # 1. Not expired
    # 2. Must have substantial surplus inventory relative to expected demand
    is_eligible = not metrics["is_expired"] and demand_info["has_substantial_surplus"]

    if not is_eligible:
        reason = (
            f"Ineligible for Stock Clearance: No substantial surplus identified. "
            f"Expected natural demand ({demand_info['expected_natural_demand']} units) can absorb "
            f"the remaining {stock} units with only {surplus} surplus units ({surplus_pct}% surplus ratio)."
        )
        return {
            "strategy": "clearance",
            "name": STRATEGY_NAMES["clearance"],
            "eligible": False,
            "score": -9999.0,
            "reason": reason,
            "best_candidate": None,
        }

    # Evaluate candidates
    candidates = []
    discounts_to_try = [d for d in CANDIDATE_DISCOUNTS if d <= max_disc]
    for d in discounts_to_try:
        cand = evaluate_candidate_price(
            discount_ratio=d,
            base_price=base_price,
            unit_cost=unit_cost,
            stock=stock,
            expected_natural_demand=demand_info["expected_natural_demand"],
            elasticity=elasticity,
            min_margin_ratio=min_margin,
            max_disc_ratio=max_disc,
        )
        # Score formula: Prioritizes cash recovery, revenue realization, and liquidation of excess units
        cand["score"] = round(cand["revenue"] - (0.5 * cand["waste_cost"]) + (cand["proj_sold"] * 2.2), 2)
        candidates.append(cand)

    best = max(candidates, key=lambda c: c["score"])

    clearance_type = "expiry-driven clearance" if hours_left <= 72 else "ordinary overstock liquidation"
    why = (
        f"Stock Clearance selected for {clearance_type}: Inventory has a substantial surplus of {surplus} units "
        f"({surplus_pct}% of total {stock} stock) over expected demand ({demand_info['expected_natural_demand']} units). "
        f"A {best['discount_percentage']}% clearance discount to ₹{best['price']:.2f} stimulates volume to sell {best['proj_sold']} units, "
        f"generating ₹{best['revenue']:.2f} in realized cash revenue without breaching the margin floor (₹{best['min_allowed_price']:.2f})."
    )

    return {
        "strategy": "clearance",
        "name": STRATEGY_NAMES["clearance"],
        "eligible": True,
        "score": best["score"],
        "reason": why,
        "best_candidate": best,
        "evaluated_candidates": candidates,
    }


def evaluate_max_profit_strategy(
    batch: dict,
    product: dict,
    metrics: dict,
    demand_info: dict,
    settings: dict,
) -> dict:
    """
    Evaluates Strategy C: MAXIMUM PROFIT
    Objective: Maximize expected gross profit while accounting for demand and inventory constraints.
    Rules:
    - Compare current price with feasible alternative prices.
    - Select the price producing the highest estimated gross profit.
    - Retain normal selling price when discounting is not financially justified.
    """
    stock = demand_info["stock"]
    unit_cost = float(batch.get("unit_cost", product.get("unit_cost", 20.0)))
    base_price = float(product.get("base_price", unit_cost * 1.4))
    elasticity = float(product.get("price_sensitivity", 1.8))
    min_margin = float(settings.get("min_margin_percent", settings.get("min_margin", 5.0))) / 100.0
    max_disc = float(settings.get("max_discount_percent", settings.get("max_discount", 35.0))) / 100.0

    is_eligible = not metrics["is_expired"]

    if not is_eligible:
        return {
            "strategy": "max_profit",
            "name": STRATEGY_NAMES["max_profit"],
            "eligible": False,
            "score": -9999.0,
            "reason": "Ineligible: Batch has expired.",
            "best_candidate": None,
        }

    # Evaluate candidate prices with pure gross profit objective
    candidates = []
    discounts_to_try = [d for d in CANDIDATE_DISCOUNTS if d <= max_disc]
    for d in discounts_to_try:
        cand = evaluate_candidate_price(
            discount_ratio=d,
            base_price=base_price,
            unit_cost=unit_cost,
            stock=stock,
            expected_natural_demand=demand_info["expected_natural_demand"],
            elasticity=elasticity,
            min_margin_ratio=min_margin,
            max_disc_ratio=max_disc,
        )
        # Score formula: Pure expected gross profit
        cand["score"] = round(cand["gross_profit"], 2)
        candidates.append(cand)

    best = max(candidates, key=lambda c: c["score"])

    # If normal price (0% discount) gives the best profit (or within ₹1 of best), retain normal price!
    normal_cand = next((c for c in candidates if c["discount_percentage"] == 0.0), None)
    if normal_cand and normal_cand["gross_profit"] >= best["gross_profit"] - 1.0:
        best = normal_cand

    if best["discount_percentage"] == 0.0:
        why = (
            f"Maximum Profit selected: Natural customer demand ({demand_info['expected_natural_demand']} units) "
            f"is strong enough to absorb remaining inventory ({stock} units). Discounting is not financially justified; "
            f"retaining the standard retail price of ₹{best['price']:.2f} maximizes expected gross profit at ₹{best['gross_profit']:.2f}."
        )
    else:
        why = (
            f"Maximum Profit selected: Candidate price comparison shows a measured {best['discount_percentage']}% adjustment "
            f"to ₹{best['price']:.2f} optimizes price elasticity, producing the highest gross profit of ₹{best['gross_profit']:.2f} "
            f"(Revenue: ₹{best['revenue']:.2f}, COGS: ₹{best['cogs']:.2f})."
        )

    return {
        "strategy": "max_profit",
        "name": STRATEGY_NAMES["max_profit"],
        "eligible": True,
        "score": best["score"],
        "reason": why,
        "best_candidate": best,
        "evaluated_candidates": candidates,
    }


def evaluate_bulk_bundles_strategy(
    batch: dict,
    product: dict,
    metrics: dict,
    demand_info: dict,
    settings: dict,
) -> dict:
    """
    Evaluates Strategy D: BULK & BUNDLES
    Objective: Encourage customers to purchase multiple units through commercially viable offers.
    Rules:
    - Sufficient stock (>= 6 units).
    - Freshness window (>= 48h remaining so customer can consume multiple units).
    - Calculate complete bundle price and effective per-unit price.
    - Never violate minimum margin on effective unit price.
    - Suitable categories: Pantry, Produce, Dairy & Eggs, Bakery, Beverages, Snacks.
    """
    stock = demand_info["stock"]
    hours_left = metrics["hours_remaining"]
    category = product.get("category", "General")

    unit_cost = float(batch.get("unit_cost", product.get("unit_cost", 20.0)))
    base_price = float(product.get("base_price", unit_cost * 1.4))
    min_margin = float(settings.get("min_margin_percent", settings.get("min_margin", 5.0))) / 100.0
    max_disc = float(settings.get("max_discount_percent", settings.get("max_discount", 35.0))) / 100.0

    min_allowed_price = round(unit_cost * (1.0 + min_margin), 2)
    if min_allowed_price > base_price:
        min_allowed_price = base_price

    # Volume suitability criteria
    volume_friendly_categories = [
        "Pantry", "Beverages", "Produce", "Dairy & Eggs", "Bakery", "Snacks", "Confectionery", "Groceries", "General"
    ]
    is_category_suitable = any(c.lower() in category.lower() for c in volume_friendly_categories)

    # Bulk bundle eligibility checks:
    # 1. Not expired
    # 2. Stock at least 6 units (allows multi-unit packaging)
    # 3. Freshness window >= 48 hours (customers need time to consume multiple items)
    # 4. Product category is suitable
    is_eligible = (
        not metrics["is_expired"]
        and stock >= 6
        and hours_left >= 48
        and is_category_suitable
    )

    if not is_eligible:
        if hours_left < 48:
            reason = f"Ineligible for Bulk & Bundles: Remaining freshness window ({hours_left}h) is too short for customer multi-unit consumption."
        elif stock < 6:
            reason = f"Ineligible for Bulk & Bundles: Available inventory ({stock} units) is insufficient for multi-unit bundle packaging."
        else:
            reason = f"Ineligible for Bulk & Bundles: Category '{category}' is unsuitable for multi-unit package promotions."
        return {
            "strategy": "bulk",
            "name": STRATEGY_NAMES["bulk"],
            "eligible": False,
            "score": -9999.0,
            "reason": reason,
            "best_candidate": None,
            "bundle_details": None,
        }

    # Determine optimal bundle size (2-pack or 3-pack based on inventory depth)
    bundle_size = 3 if stock >= 20 and demand_info["daily_demand"] >= 5 else 2

    # Candidate bundle discount percentages (e.g. 8%, 12%, 15%, 20%)
    bundle_discounts = [0.08, 0.12, 0.15, 0.20]
    bundle_candidates = []

    for bd in bundle_discounts:
        if bd > max_disc:
            continue

        raw_bundle_price = base_price * bundle_size * (1.0 - bd)
        effective_unit_price = round(raw_bundle_price / bundle_size, 2)

        # Enforce minimum margin floor
        if effective_unit_price < min_allowed_price:
            effective_unit_price = min_allowed_price
            raw_bundle_price = round(effective_unit_price * bundle_size, 2)
            actual_discount = max(0.0, round(((base_price * bundle_size) - raw_bundle_price) / (base_price * bundle_size), 4))
        else:
            actual_discount = bd

        bundle_price = round(raw_bundle_price, 2)
        bundle_cost = round(unit_cost * bundle_size, 2)
        profit_per_bundle = round(bundle_price - bundle_cost, 2)

        # Estimate bundle uptake
        available_bundles = stock // bundle_size
        natural_bundle_demand = (demand_info["expected_natural_demand"] / bundle_size) * (1.0 + actual_discount * 1.5)
        proj_bundles_sold = min(available_bundles, max(1, int(round(natural_bundle_demand))))
        proj_units_sold = proj_bundles_sold * bundle_size
        proj_waste_units = max(0, stock - proj_units_sold)

        revenue = round(proj_bundles_sold * bundle_price, 2)
        cogs = round(proj_units_sold * unit_cost, 2)
        gross_profit = round(revenue - cogs, 2)
        waste_cost = round(proj_waste_units * unit_cost, 2)

        # Score formula: Volume reward bonus with waste penalty
        score = gross_profit - (0.6 * waste_cost) + (proj_units_sold * 1.5)

        bundle_candidates.append({
            "bundle_size": bundle_size,
            "bundle_price": bundle_price,
            "effective_unit_price": effective_unit_price,
            "bundle_discount_pct": round(actual_discount * 100.0, 1),
            "proj_bundles_sold": proj_bundles_sold,
            "proj_sold": proj_units_sold,
            "proj_waste": proj_waste_units,
            "revenue": revenue,
            "cogs": cogs,
            "gross_profit": gross_profit,
            "waste_cost": waste_cost,
            "min_allowed_price": min_allowed_price,
            "score": round(score, 2),
        })

    best_bundle = max(bundle_candidates, key=lambda b: b["score"])

    why = (
        f"Bulk & Bundles selected: Ample stock ({stock} units) and safe {metrics['days_remaining']}d freshness window "
        f"make a multi-unit offer commercially attractive. Recommending a {best_bundle['bundle_size']}-Pack Bundle "
        f"at ₹{best_bundle['bundle_price']:.2f} (effective ₹{best_bundle['effective_unit_price']:.2f}/unit, "
        f"{best_bundle['bundle_discount_pct']}% savings). Expected to sell {best_bundle['proj_bundles_sold']} bundles "
        f"({best_bundle['proj_sold']} units), realizing ₹{best_bundle['revenue']:.2f} revenue and ₹{best_bundle['gross_profit']:.2f} profit."
    )

    best_as_candidate = {
        "discount_ratio": round(best_bundle["bundle_discount_pct"] / 100.0, 4),
        "discount_percentage": best_bundle["bundle_discount_pct"],
        "price": best_bundle["effective_unit_price"],
        "proj_sold": best_bundle["proj_sold"],
        "proj_waste": best_bundle["proj_waste"],
        "revenue": best_bundle["revenue"],
        "cogs": best_bundle["cogs"],
        "gross_profit": best_bundle["gross_profit"],
        "waste_cost": best_bundle["waste_cost"],
        "net_outcome": round(best_bundle["gross_profit"] - best_bundle["waste_cost"], 2),
        "margin_percentage": round(((best_bundle["effective_unit_price"] - unit_cost) / best_bundle["effective_unit_price"]) * 100.0, 1) if best_bundle["effective_unit_price"] > 0 else 0.0,
        "guardrails_passed": True,
        "min_allowed_price": best_bundle["min_allowed_price"],
        "score": best_bundle["score"],
    }

    bundle_meta = {
        "bundle_size": best_bundle["bundle_size"],
        "bundle_price": best_bundle["bundle_price"],
        "effective_unit_price": best_bundle["effective_unit_price"],
        "unit_savings": round(base_price - best_bundle["effective_unit_price"], 2),
        "bundle_discount_pct": best_bundle["bundle_discount_pct"],
    }

    return {
        "strategy": "bulk",
        "name": STRATEGY_NAMES["bulk"],
        "eligible": True,
        "score": best_bundle["score"],
        "reason": why,
        "best_candidate": best_as_candidate,
        "bundle_details": bundle_meta,
        "evaluated_candidates": bundle_candidates,
    }


def evaluate_balanced_smart_strategy(
    batch: dict,
    product: dict,
    metrics: dict,
    demand_info: dict,
    settings: dict,
) -> dict:
    """
    Evaluates Strategy E: BALANCED SMART
    Objective: Balance expected profit, expiry risk, inventory waste, and sales velocity.
    Rules:
    - Multi-objective optimization across all candidate prices.
    - Used when it provides the best overall trade-off.
    """
    stock = demand_info["stock"]
    unit_cost = float(batch.get("unit_cost", product.get("unit_cost", 20.0)))
    base_price = float(product.get("base_price", unit_cost * 1.4))
    elasticity = float(product.get("price_sensitivity", 1.8))
    min_margin = float(settings.get("min_margin_percent", settings.get("min_margin", 5.0))) / 100.0
    max_disc = float(settings.get("max_discount_percent", settings.get("max_discount", 35.0))) / 100.0

    is_eligible = not metrics["is_expired"]

    if not is_eligible:
        return {
            "strategy": "balanced",
            "name": STRATEGY_NAMES["balanced"],
            "eligible": False,
            "score": -9999.0,
            "reason": "Ineligible: Batch has expired.",
            "best_candidate": None,
        }

    candidates = []
    discounts_to_try = [d for d in CANDIDATE_DISCOUNTS if d <= max_disc]
    for d in discounts_to_try:
        cand = evaluate_candidate_price(
            discount_ratio=d,
            base_price=base_price,
            unit_cost=unit_cost,
            stock=stock,
            expected_natural_demand=demand_info["expected_natural_demand"],
            elasticity=elasticity,
            min_margin_ratio=min_margin,
            max_disc_ratio=max_disc,
        )
        # Score formula: Balanced trade-off (Gross Profit - 0.75 * Waste Cost + 1.0 * Velocity)
        cand["score"] = round(cand["gross_profit"] - (0.75 * cand["waste_cost"]) + (cand["proj_sold"] * 1.0), 2)
        candidates.append(cand)

    best = max(candidates, key=lambda c: c["score"])

    why = (
        f"Balanced Smart selected: Provides the optimal multi-objective balance between gross margin (₹{best['gross_profit']:.2f}) "
        f"and waste mitigation ({best['proj_waste']} unsold units). Recommended price of ₹{best['price']:.2f} "
        f"({best['discount_percentage']}% markdown) secures {best['proj_sold']} unit sales while upholding the minimum margin floor."
    )

    return {
        "strategy": "balanced",
        "name": STRATEGY_NAMES["balanced"],
        "eligible": True,
        "score": best["score"],
        "reason": why,
        "best_candidate": best,
        "evaluated_candidates": candidates,
    }


def evaluate_all_strategies_for_batch(
    batch: dict,
    product: dict,
    metrics: dict,
    demand_info: dict,
    settings: dict,
) -> dict[StrategyMode, dict]:
    """
    Evaluates all five pricing strategies independently for a single inventory batch.
    """
    return {
        "near_expiry": evaluate_near_expiry_strategy(batch, product, metrics, demand_info, settings),
        "clearance": evaluate_stock_clearance_strategy(batch, product, metrics, demand_info, settings),
        "max_profit": evaluate_max_profit_strategy(batch, product, metrics, demand_info, settings),
        "bulk": evaluate_bulk_bundles_strategy(batch, product, metrics, demand_info, settings),
        "balanced": evaluate_balanced_smart_strategy(batch, product, metrics, demand_info, settings),
    }


def classify_batch_and_recommend(
    batch: dict,
    product: dict,
    settings: dict | None = None,
    now: datetime | None = None,
    force_mode: str | None = None,
) -> dict:
    """
    Core Classification & Recommendation Engine.
    Classifies every inventory batch into its most appropriate strategy:
    1. Evaluates all 5 strategies independently.
    2. Enforces configurable expiry policy bands:
       A. Expired -> block sales and discounts
       B. < 24 Hours -> critically urgent (Near-Expiry or Clearance)
       C. 1-2 Days -> expiry-risk and surplus evaluation
       D. > 2 Days -> 60% elapsed threshold, surplus, profit
       E. Long shelf life -> Max Profit, Clearance, Bulk, or Balanced
    3. Generates transparent reasons for chosen strategy and why others were excluded.
    """
    if settings is None:
        settings = {}
    if now is None:
        now = datetime.now(timezone.utc)
    today = now.date()

    metrics = calculate_shelf_life_metrics(batch, today=today, now=now)
    demand_info = calculate_demand_and_surplus(batch, product, metrics)

    unit_cost = float(batch.get("unit_cost", product.get("unit_cost", 20.0)))
    base_price = float(product.get("base_price", unit_cost * 1.4))
    stock = demand_info["stock"]
    min_margin = float(settings.get("min_margin_percent", settings.get("min_margin", 5.0))) / 100.0
    min_floor = round(unit_cost * (1.0 + min_margin), 2)
    if min_floor > base_price:
        min_floor = base_price

    # 1. CASE: EXPIRED BATCH
    if metrics["is_expired"]:
        exp_reason = (
            f"Batch {batch.get('batch_number')} expired on {metrics['expiry_date_str']}. "
            f"All retail sales and discount promotions are strictly blocked under food safety rules."
        )
        return {
            "product_id": product["_id"],
            "batch_id": batch["_id"],
            "pricing_mode": "balanced",
            "strategy_name": STRATEGY_NAMES["balanced"],
            "current_price": base_price,
            "recommended_price": base_price,
            "discount_percentage": 0.0,
            "projected_units_sold": 0,
            "projected_revenue": 0.0,
            "projected_profit": 0.0,
            "projected_waste_units": stock,
            "bundle_details": None,
            "reason": exp_reason,
            "decision_reason": exp_reason,
            "status": "expired",
            "guardrails_passed": False,
            "min_allowed_price": min_floor,
            "shelf_life_elapsed_pct": 100.0,
            "hours_remaining": 0,
            "days_to_expiry": 0,
            "expected_demand": 0.0,
            "surplus_units": stock,
            "eligibility_status": "expired",
            "why_selected": exp_reason,
            "why_others_not_selected": {
                s: "Sales blocked: batch has passed its expiry date."
                for s in STRATEGY_NAMES.keys()
            },
            "strategy_evaluations": {},
            "alternatives_considered": [],
            "_display": {
                "product_name": product.get("name", "Grocery Item"),
                "category": product.get("category", "General"),
                "batch_number": batch.get("batch_number", "BATCH-01"),
                "unit_cost": unit_cost,
                "stock_remaining": stock,
                "days_to_expiry": 0,
                "hours_remaining": 0,
                "guardrails_passed": False,
                "min_allowed_price": min_floor,
                "shelf_life_elapsed_pct": 100.0,
                "eligibility_status": "expired",
                "received_date": metrics["received_date_str"],
                "expiry_date": metrics["expiry_date_str"],
            },
        }

    # 2. EVALUATE ALL FIVE STRATEGIES
    all_evals = evaluate_all_strategies_for_batch(batch, product, metrics, demand_info, settings)

    # 3. STRATEGY CLASSIFICATION DECISION ENGINE
    hours_left = metrics["hours_remaining"]
    elapsed_pct = metrics["shelf_life_elapsed_pct"]
    has_surplus = demand_info["has_substantial_surplus"]

    selected_strategy: StrategyMode = "balanced"
    selection_notes = ""

    if force_mode and force_mode in all_evals and all_evals[force_mode]["eligible"]:
        # Manual retailer force override
        selected_strategy = force_mode  # type: ignore
        selection_notes = f"Manually reviewed under {STRATEGY_NAMES[selected_strategy]}."
    else:
        # AUTOMATIC CLASSIFICATION POLICY
        # Band B: Less Than 24 Hours Remaining (Critically Urgent)
        if metrics["is_under_24h"]:
            if has_surplus and all_evals["clearance"]["eligible"] and all_evals["clearance"]["score"] > all_evals["near_expiry"]["score"]:
                selected_strategy = "clearance"
                selection_notes = "Critically urgent (<24h) with heavy inventory surplus: Stock Clearance selected for maximum volume liquidation."
            elif all_evals["near_expiry"]["eligible"]:
                selected_strategy = "near_expiry"
                selection_notes = "Critically urgent (<24h remaining): Near-Expiry Markdown prioritized to prevent total spoilage loss."
            else:
                selected_strategy = "balanced"

        # Band C: 1 to 2 Days Remaining (Urgent Expiry Window)
        elif metrics["is_1_to_2_days"]:
            if has_surplus and all_evals["clearance"]["eligible"]:
                selected_strategy = "clearance"
                selection_notes = "1-2 days remaining with substantial inventory overhang: Stock Clearance prioritized."
            elif all_evals["near_expiry"]["eligible"]:
                selected_strategy = "near_expiry"
                selection_notes = "1-2 days remaining with risk of unsold stock: Near-Expiry Markdown selected."
            elif all_evals["max_profit"]["eligible"] and all_evals["max_profit"]["best_candidate"]["discount_percentage"] == 0.0:
                selected_strategy = "max_profit"
                selection_notes = "1-2 days remaining but customer demand comfortably clears inventory: Maximum Profit retained."
            else:
                selected_strategy = "balanced"

        # Band D: More Than 2 Days Remaining (Medium Shelf Life)
        elif hours_left <= 120:  # <= 5 days
            if has_surplus and all_evals["clearance"]["eligible"]:
                selected_strategy = "clearance"
                selection_notes = "Substantial inventory surplus beyond natural sales: Stock Clearance markdown justified."
            elif all_evals["bulk"]["eligible"] and stock >= 10:
                selected_strategy = "bulk"
                selection_notes = "Adequate stock and healthy freshness window: Bulk & Bundles multi-unit promotion selected."
            elif demand_info["projected_surplus"] == 0 and all_evals["max_profit"]["eligible"]:
                selected_strategy = "max_profit"
                selection_notes = "Natural demand absorbs batch stock: Maximum Profit normal price retained."
            elif elapsed_pct >= 60.0 and all_evals["near_expiry"]["eligible"]:
                selected_strategy = "near_expiry"
                selection_notes = "Batch elapsed >= 60% shelf life: Near-Expiry Markdown selected to maintain sell-through."
            else:
                selected_strategy = "balanced"

        # Band E: Long Shelf Life (> 5 days)
        else:
            if all_evals["bulk"]["eligible"] and stock >= 12:
                selected_strategy = "bulk"
                selection_notes = "Long shelf life and high inventory quantity: Bulk & Bundles offer selected to increase basket size."
            elif has_surplus and all_evals["clearance"]["eligible"]:
                selected_strategy = "clearance"
                selection_notes = "Excess stock relative to daily sales velocity: Stock Clearance selected."
            elif demand_info["projected_surplus"] == 0 and all_evals["max_profit"]["eligible"]:
                selected_strategy = "max_profit"
                selection_notes = "Long freshness window with zero surplus: Maximum Profit retained."
            else:
                selected_strategy = "balanced"

    # Fetch winning strategy outcome
    chosen_eval = all_evals[selected_strategy]
    best_candidate = chosen_eval["best_candidate"]
    bundle_meta = chosen_eval.get("bundle_details")

    # If winning strategy has no candidate (e.g. edge case), fallback to balanced
    if not best_candidate:
        chosen_eval = all_evals["balanced"]
        best_candidate = chosen_eval["best_candidate"]
        selected_strategy = "balanced"

    # Build detailed why-others-not-selected map
    why_others: dict[str, str] = {}
    for sm, ev in all_evals.items():
        if sm == selected_strategy:
            continue
        if not ev["eligible"]:
            why_others[sm] = ev["reason"]
        else:
            cand = ev["best_candidate"]
            score_diff = round(chosen_eval["score"] - ev["score"], 1)
            why_others[sm] = (
                f"Eligible but scored lower (score: {ev['score']} vs chosen {chosen_eval['score']}, difference: {score_diff}). "
                f"Projected profit was ₹{cand['gross_profit']:.2f} with {cand['proj_waste']} unsold units."
            )

    why_selected = chosen_eval["reason"]
    if selection_notes:
        why_selected = f"{selection_notes} {why_selected}"

    # Structure alternative candidate prices
    alternatives = []
    if "evaluated_candidates" in chosen_eval:
        for c in chosen_eval["evaluated_candidates"]:
            if "discount_percentage" in c:
                alternatives.append({
                    "discount_pct": c.get("discount_percentage", 0.0),
                    "price": c.get("price", c.get("effective_unit_price", base_price)),
                    "expected_sales": c.get("proj_sold", 0),
                    "gross_profit": c.get("gross_profit", 0.0),
                    "score": c.get("score", 0.0),
                })

    return {
        "product_id": product["_id"],
        "batch_id": batch["_id"],
        "pricing_mode": selected_strategy,
        "strategy_name": STRATEGY_NAMES[selected_strategy],
        "current_price": base_price,
        "recommended_price": best_candidate["price"],
        "discount_percentage": best_candidate["discount_percentage"],
        "projected_units_sold": best_candidate["proj_sold"],
        "projected_revenue": best_candidate["revenue"],
        "projected_profit": best_candidate["gross_profit"],
        "projected_waste_units": best_candidate["proj_waste"],
        "bundle_details": bundle_meta,
        "reason": why_selected,
        "decision_reason": why_selected,
        "status": "pending",
        "guardrails_passed": best_candidate["guardrails_passed"],
        "min_allowed_price": best_candidate["min_allowed_price"],
        "shelf_life_elapsed_pct": metrics["shelf_life_elapsed_pct"],
        "hours_remaining": metrics["hours_remaining"],
        "days_to_expiry": metrics["days_remaining"],
        "expected_demand": demand_info["expected_natural_demand"],
        "surplus_units": demand_info["projected_surplus"],
        "eligibility_status": metrics["eligibility_status"],
        "why_selected": why_selected,
        "why_others_not_selected": why_others,
        "strategy_evaluations": {
            k: {
                "name": v["name"],
                "eligible": v["eligible"],
                "score": v["score"],
                "price": v["best_candidate"]["price"] if v["best_candidate"] else base_price,
                "discount_pct": v["best_candidate"]["discount_percentage"] if v["best_candidate"] else 0.0,
                "expected_sales": v["best_candidate"]["proj_sold"] if v["best_candidate"] else 0,
                "gross_profit": v["best_candidate"]["gross_profit"] if v["best_candidate"] else 0.0,
                "unsold_waste": v["best_candidate"]["proj_waste"] if v["best_candidate"] else stock,
            }
            for k, v in all_evals.items()
        },
        "alternatives_considered": alternatives,
        "_display": {
            "product_name": product.get("name", "Grocery Item"),
            "category": product.get("category", "General"),
            "batch_number": batch.get("batch_number", "BATCH-01"),
            "unit_cost": unit_cost,
            "stock_remaining": stock,
            "days_to_expiry": metrics["days_remaining"],
            "hours_remaining": metrics["hours_remaining"],
            "guardrails_passed": best_candidate["guardrails_passed"],
            "min_allowed_price": best_candidate["min_allowed_price"],
            "shelf_life_elapsed_pct": metrics["shelf_life_elapsed_pct"],
            "eligibility_status": metrics["eligibility_status"],
            "received_date": metrics["received_date_str"],
            "expiry_date": metrics["expiry_date_str"],
            "expected_demand": demand_info["expected_natural_demand"],
            "surplus_units": demand_info["projected_surplus"],
            "bundle_details": bundle_meta,
        },
    }


def compute_batch_recommendation(
    batch: dict,
    product: dict,
    mode: str = "auto",
    settings: dict | None = None,
    today: date | None = None,
    now: datetime | None = None,
) -> dict:
    """
    Backward-compatible wrapper for compute_batch_recommendation.
    Delegates to classify_batch_and_recommend.
    """
    force_mode = None if mode in ["auto", "all"] else mode
    return classify_batch_and_recommend(
        batch=batch,
        product=product,
        settings=settings,
        now=now,
        force_mode=force_mode,
    )
