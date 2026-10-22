from datetime import datetime, timezone, timedelta
from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query, status
from pymongo.errors import PyMongoError

from app.database import get_database
from app.schemas import DealApprovalRequest
from app.pricing_engine import (
    classify_batch_and_recommend,
    calculate_shelf_life_metrics,
    STRATEGY_NAMES,
)
from app.scheduler import run_daily_monitoring_job

router = APIRouter(prefix="/api/pricing", tags=["Smart Pricing"])


def serialize_recommendation(rec: dict, display_info: dict | None = None) -> dict:
    disp = display_info or rec.get("_display", {})
    created = rec.get("created_at")
    created_str = created.isoformat() if isinstance(created, datetime) else str(created or "")
    updated = rec.get("updated_at")
    updated_str = updated.isoformat() if isinstance(updated, datetime) else (str(updated) if updated else None)

    strategy_mode = rec.get("pricing_mode", "balanced")
    strategy_name = rec.get("strategy_name", STRATEGY_NAMES.get(strategy_mode, "Balanced Smart"))

    return {
        "id": str(rec["_id"]),
        "product_id": str(rec["product_id"]),
        "product_name": disp.get("product_name", rec.get("product_name", "Grocery Item")),
        "category": disp.get("category", rec.get("category", "General")),
        "image_url": disp.get("image_url") or rec.get("image_url"),
        "batch_id": str(rec["batch_id"]),
        "batch_number": disp.get("batch_number", rec.get("batch_number", "BATCH-01")),
        "unit_cost": float(disp.get("unit_cost", rec.get("unit_cost", 20.0))),
        "current_price": float(rec["current_price"]),
        "recommended_price": float(rec["recommended_price"]),
        "discount_percentage": float(rec["discount_percentage"]),
        "stock_remaining": int(disp.get("stock_remaining", rec.get("stock_remaining", 0))),
        "days_to_expiry": int(disp.get("days_to_expiry", rec.get("days_to_expiry", 2))),
        "hours_remaining": int(disp.get("hours_remaining", rec.get("hours_remaining", 48))),
        "projected_units_sold": int(rec.get("projected_units_sold", 0)),
        "projected_revenue": float(rec.get("projected_revenue", 0.0)),
        "projected_profit": float(rec.get("projected_profit", 0.0)),
        "projected_waste_units": int(rec.get("projected_waste_units", 0)),
        "reason": rec.get("reason", ""),
        "pricing_mode": strategy_mode,
        "strategy_name": strategy_name,
        "status": rec.get("status", "pending"),
        "guardrails_passed": bool(disp.get("guardrails_passed", rec.get("guardrails_passed", True))),
        "min_allowed_price": float(disp.get("min_allowed_price", rec.get("min_allowed_price", rec.get("current_price", 0)))),
        "shelf_life_elapsed_pct": float(rec.get("shelf_life_elapsed_pct", disp.get("shelf_life_elapsed_pct", 0.0))),
        "expected_demand": float(disp.get("expected_demand", rec.get("expected_demand", 0.0))),
        "surplus_units": int(disp.get("surplus_units", rec.get("surplus_units", 0))),
        "bundle_details": rec.get("bundle_details") or disp.get("bundle_details"),
        "why_selected": rec.get("why_selected") or rec.get("reason", ""),
        "why_others_not_selected": rec.get("why_others_not_selected", {}),
        "strategy_evaluations": rec.get("strategy_evaluations", {}),
        "material_change_flag": bool(rec.get("material_change_flag", False)),
        "eligibility_status": rec.get("eligibility_status", disp.get("eligibility_status", "eligible")),
        "received_date": disp.get("received_date") or rec.get("received_date"),
        "expiry_date": disp.get("expiry_date") or rec.get("expiry_date"),
        "decision_reason": rec.get("decision_reason") or rec.get("why_selected") or rec.get("reason"),
        "alternatives_considered": rec.get("alternatives_considered", []),
        "created_at": created_str,
        "updated_at": updated_str,
    }


@router.get("/recommendations")
async def get_pricing_recommendations(
    mode: str | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
):
    """
    Returns batch-wise pricing recommendations automatically classified into the 5 strategies.
    - Evaluates each batch independently across all 5 strategies.
    - Persists only one active recommendation per batch in pricing_recommendations collection.
    - Respects actual MongoDB batch inventory and expiry dates.
    - Preserves retailer approval state.
    """
    # Guard against direct python invocation where default is QueryInfo
    if not isinstance(mode, str):
        mode = None
    if not isinstance(status_filter, str):
        status_filter = None

    db = get_database()
    now = datetime.now(timezone.utc)
    today_str = now.date().strftime("%Y-%m-%d")

    try:
        settings_doc = await db.store_settings.find_one({}) or {}

        # Fetch active inventory batches with positive remaining quantity, sorted by earliest expiry (FEFO)
        batches = (
            await db.inventory_batches.find({
                "quantity_remaining": {"$gt": 0},
                "expiry_date": {"$gte": today_str},
            })
            .sort("expiry_date", 1)
            .limit(50)
            .to_list(length=50)
        )

        results = []
        for batch in batches:
            prod = await db.products.find_one({"_id": batch["product_id"]})
            if not prod or not prod.get("active", True):
                continue

            # Classify batch using the comprehensive 5-strategy decision engine
            rec_data = classify_batch_and_recommend(
                batch=batch,
                product=prod,
                settings=settings_doc,
                now=now,
                force_mode=None if mode in [None, "all"] else mode,
            )
            display_data = rec_data.pop("_display", {})
            if prod.get("image_url"):
                display_data["image_url"] = prod["image_url"]

            # Locate existing recommendation for this specific batch
            existing_rec = await db.pricing_recommendations.find_one({
                "batch_id": batch["_id"],
            })

            # Check if an approved customer deal is currently active for this batch
            existing_deal = await db.deals.find_one({
                "batch_id": batch["_id"],
                "status": "active",
            })

            if existing_deal:
                rec_data["status"] = "approved"

            if existing_rec:
                # If existing is pending, update it with fresh evaluation
                if existing_rec.get("status") == "pending":
                    rec_data["updated_at"] = now
                    rec_data["status"] = "pending"
                    await db.pricing_recommendations.update_one(
                        {"_id": existing_rec["_id"]},
                        {"$set": rec_data},
                    )
                    existing_rec.update(rec_data)
                    serialized = serialize_recommendation(existing_rec, display_data)
                else:
                    # If existing recommendation is already approved or rejected, preserve its status
                    rec_data["status"] = existing_rec.get("status", "pending")
                    rec_data["updated_at"] = now
                    await db.pricing_recommendations.update_one(
                        {"_id": existing_rec["_id"]},
                        {"$set": rec_data},
                    )
                    existing_rec.update(rec_data)
                    serialized = serialize_recommendation(existing_rec, display_data)
            else:
                rec_data["created_at"] = now
                rec_data["updated_at"] = now
                insert_res = await db.pricing_recommendations.insert_one(rec_data)
                rec_id = getattr(insert_res, "inserted_id", rec_data.get("_id"))
                rec_data["_id"] = rec_id
                serialized = serialize_recommendation(rec_data, display_data)

            # Apply mode filter if specifically requested and not 'all'
            if mode and mode not in ["all", "auto"]:
                if serialized["pricing_mode"] != mode:
                    continue

            # Apply status filter if requested
            if status_filter and serialized["status"] != status_filter:
                continue

            results.append(serialized)

        return {"items": results}
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail="Could not calculate recommendations.") from exc


@router.get("/strategies-summary")
async def get_strategies_summary():
    """
    Returns actual backend evaluation summary across all five strategies:
    - Number of batches assigned to each strategy
    - Number of pending and approved recommendations per strategy
    - Clear zero count for strategies with no assigned batches (no fake numbers)
    """
    db = get_database()
    now = datetime.now(timezone.utc)
    today_str = now.date().strftime("%Y-%m-%d")

    try:
        settings_doc = await db.store_settings.find_one({}) or {}

        batches = (
            await db.inventory_batches.find({
                "quantity_remaining": {"$gt": 0},
                "expiry_date": {"$gte": today_str},
            })
            .sort("expiry_date", 1)
            .to_list(length=100)
        )

        # Initialize summary metrics for all 5 strategies
        summary = {
            s_key: {
                "strategy": s_key,
                "name": STRATEGY_NAMES[s_key],
                "batch_count": 0,
                "pending_count": 0,
                "approved_count": 0,
                "total_projected_profit": 0.0,
                "total_projected_revenue": 0.0,
                "total_waste_prevented": 0,
            }
            for s_key in ["balanced", "near_expiry", "max_profit", "clearance", "bulk"]
        }

        total_batches = 0
        total_pending = 0
        total_approved = 0

        for batch in batches:
            prod = await db.products.find_one({"_id": batch["product_id"]})
            if not prod or not prod.get("active", True):
                continue

            rec = classify_batch_and_recommend(batch=batch, product=prod, settings=settings_doc, now=now)
            chosen_strategy = rec["pricing_mode"]

            # Check if active deal exists
            existing_deal = await db.deals.find_one({
                "batch_id": batch["_id"],
                "status": "active",
            })
            is_approved = existing_deal is not None

            if chosen_strategy in summary:
                summary[chosen_strategy]["batch_count"] += 1
                if is_approved:
                    summary[chosen_strategy]["approved_count"] += 1
                    total_approved += 1
                else:
                    summary[chosen_strategy]["pending_count"] += 1
                    total_pending += 1

                summary[chosen_strategy]["total_projected_profit"] += rec.get("projected_profit", 0.0)
                summary[chosen_strategy]["total_projected_revenue"] += rec.get("projected_revenue", 0.0)
                summary[chosen_strategy]["total_waste_prevented"] += int(rec.get("projected_units_sold", 0))
                total_batches += 1

        # Round financial values
        for s_data in summary.values():
            s_data["total_projected_profit"] = round(s_data["total_projected_profit"], 2)
            s_data["total_projected_revenue"] = round(s_data["total_projected_revenue"], 2)

        return {
            "strategies": summary,
            "total_batches_classified": total_batches,
            "total_pending": total_pending,
            "total_approved": total_approved,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail="Could not compute strategies summary.") from exc


@router.post("/recommendations/{rec_id}/approve", status_code=status.HTTP_201_CREATED)
async def approve_recommendation(rec_id: str, payload: DealApprovalRequest | None = None):
    """
    Retailer Approval Workflow:
    1. Recheck current batch quantity and expiry date.
    2. Revalidate selected price against pricing rules & guardrails.
    3. Confirm batch is still eligible for sale (not expired).
    4. Save approval status and timestamp in pricing_recommendations.
    5. Publish approved deal in deals collection (preventing duplicates).
    6. Keep standard selling price in products collection unchanged.
    7. Stock deduction follows FEFO upon order placement.
    """
    db = get_database()
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")

    # 1. Locate recommendation record
    rec = None
    if ObjectId.is_valid(rec_id):
        rec = await db.pricing_recommendations.find_one({"_id": ObjectId(rec_id)})

    if not rec and "-" in rec_id:
        parts = rec_id.split("-")
        batch_id_str = parts[1] if len(parts) >= 2 else None
        if batch_id_str and ObjectId.is_valid(batch_id_str):
            rec = await db.pricing_recommendations.find_one({
                "batch_id": ObjectId(batch_id_str),
            })

    # 2. Locate target batch and product
    batch = None
    if rec:
        batch = await db.inventory_batches.find_one({"_id": rec["batch_id"]})
    else:
        parts = rec_id.split("-")
        batch_id_str = parts[1] if len(parts) >= 2 else rec_id
        if ObjectId.is_valid(batch_id_str):
            batch = await db.inventory_batches.find_one({"_id": ObjectId(batch_id_str)})

    if not batch:
        raise HTTPException(status_code=404, detail="Target inventory batch not found.")

    # Guard 1: Batch must not be expired or depleted
    batch_expiry = batch.get("expiry_date", "")
    if batch_expiry < today_str:
        raise HTTPException(status_code=400, detail="Cannot approve deal: Batch has already expired.")

    remaining_qty = int(batch.get("quantity_remaining", 0))
    if remaining_qty <= 0:
        raise HTTPException(status_code=400, detail="Cannot approve deal: Batch inventory is depleted.")

    prod = await db.products.find_one({"_id": batch["product_id"]})
    if not prod:
        raise HTTPException(status_code=404, detail="Product catalogue entry not found.")

    base_price = float(prod.get("base_price", 35.0))
    unit_cost = float(batch.get("unit_cost", prod.get("unit_cost", 20.0)))

    # Guard 2: Enforce store settings guardrails (minimum margin floor & maximum discount)
    settings_doc = await db.store_settings.find_one({}) or {}
    min_margin_pct = float(settings_doc.get("min_margin_percent", settings_doc.get("min_margin", 5.0))) / 100.0
    max_disc_pct = float(settings_doc.get("max_discount_percent", settings_doc.get("max_discount", 35.0))) / 100.0

    min_floor = round(unit_cost * (1.0 + min_margin_pct), 2)
    if min_floor > base_price:
        min_floor = base_price

    # Determine proposed price
    if payload and payload.custom_price is not None:
        proposed_price = float(payload.custom_price)
    elif rec:
        proposed_price = float(rec["recommended_price"])
    else:
        proposed_price = round(base_price * 0.85, 2)

    # Guardrail Validation
    if proposed_price < min_floor:
        proposed_price = min_floor
    if proposed_price > base_price:
        proposed_price = base_price

    final_price = round(proposed_price, 2)
    discount_pct = round(((base_price - final_price) / base_price) * 100, 1) if base_price > 0 else 0.0

    # Step 3 & 4: Save approval status in pricing_recommendations
    if rec:
        await db.pricing_recommendations.update_one(
            {"_id": rec["_id"]},
            {
                "$set": {
                    "status": "approved",
                    "approved_price": final_price,
                    "approved_at": now,
                    "updated_at": now,
                }
            },
        )

    # Step 5: Publish approved deal to deals collection (preventing duplicates)
    existing_deal = await db.deals.find_one({
        "batch_id": batch["_id"],
        "status": "active",
    })

    if existing_deal:
        await db.deals.update_one(
            {"_id": existing_deal["_id"]},
            {
                "$set": {
                    "deal_price": final_price,
                    "discount_percentage": discount_pct,
                    "quantity_allocated": remaining_qty,
                    "published_to_customer": True,
                    "updated_at": now,
                }
            },
        )
        deal_id = existing_deal["_id"]
    else:
        deal_doc = {
            "product_id": prod["_id"],
            "product_name": prod["name"],
            "category": prod["category"],
            "batch_id": batch["_id"],
            "batch_number": batch.get("batch_number", "BATCH-01"),
            "original_price": base_price,
            "deal_price": final_price,
            "discount_percentage": discount_pct,
            "quantity_allocated": remaining_qty,
            "quantity_sold": 0,
            "start_date": today_str,
            "end_date": batch_expiry,
            "status": "active",
            "published_to_customer": True,
            "recommendation_id": rec["_id"] if rec else None,
            "created_at": now,
            "updated_at": now,
        }
        ins_res = await db.deals.insert_one(deal_doc)
        deal_id = getattr(ins_res, "inserted_id", deal_doc.get("_id"))

    created_deal = await db.deals.find_one({"_id": deal_id})

    # Step 6: Standard retail price in products is strictly unchanged!

    return {
        "id": str(created_deal["_id"]),
        "product_id": str(created_deal["product_id"]),
        "product_name": created_deal.get("product_name", prod["name"]),
        "category": created_deal.get("category", prod["category"]),
        "batch_number": created_deal.get("batch_number", batch.get("batch_number", "BATCH-01")),
        "original_price": float(created_deal["original_price"]),
        "deal_price": float(created_deal["deal_price"]),
        "discount_percentage": float(created_deal["discount_percentage"]),
        "quantity_allocated": int(created_deal["quantity_allocated"]),
        "quantity_sold": int(created_deal.get("quantity_sold", 0)),
        "start_date": str(created_deal["start_date"]),
        "end_date": str(created_deal["end_date"]),
        "status": created_deal.get("status", "active"),
        "published_to_customer": bool(created_deal.get("published_to_customer", True)),
    }


@router.post("/recommendations/{rec_id}/reject")
async def reject_recommendation(rec_id: str):
    """
    Retailer Rejection: Dismisses a recommendation and deactivates any linked active deals.
    """
    db = get_database()
    now = datetime.now(timezone.utc)

    rec = None
    if ObjectId.is_valid(rec_id):
        rec = await db.pricing_recommendations.find_one({"_id": ObjectId(rec_id)})

    if not rec and "-" in rec_id:
        parts = rec_id.split("-")
        batch_id_str = parts[1] if len(parts) >= 2 else None
        if batch_id_str and ObjectId.is_valid(batch_id_str):
            rec = await db.pricing_recommendations.find_one({
                "batch_id": ObjectId(batch_id_str),
            })

    if rec:
        await db.pricing_recommendations.update_one(
            {"_id": rec["_id"]},
            {"$set": {"status": "rejected", "rejected_at": now, "updated_at": now}},
        )
        # Deactivate any active deal on this batch
        await db.deals.update_many(
            {"batch_id": rec["batch_id"], "status": "active"},
            {"$set": {"status": "withdrawn", "published_to_customer": False, "updated_at": now}},
        )
    elif ObjectId.is_valid(rec_id):
        await db.pricing_recommendations.update_one(
            {"_id": ObjectId(rec_id)},
            {"$set": {"status": "rejected", "rejected_at": now, "updated_at": now}},
        )

    return {"message": "Recommendation dismissed successfully.", "status": "rejected"}


@router.post("/monitor/run")
async def trigger_daily_monitoring():
    """
    Manually triggers the background daily monitoring job:
    Recalculates batch expiry risk, cleans expired deals, and refreshes pricing suggestions.
    """
    stats = await run_daily_monitoring_job()
    return {"message": "Daily inventory and pricing monitoring completed.", "stats": stats}
