"""
Automatic Daily Monitoring & Scheduled Re-Evaluation Service for FreshSave AI
- Recalculates batch expiry status and hours remaining daily based on actual calendar dates.
- Classifies each active batch into its winning strategy among the 5 distinct strategies.
- Stops recommending discounts for expired or depleted batches and blocks sales immediately.
- Withdraws expired deals from customer store.
- Avoids unnecessary database writes when recommendations have not materially changed.
- Protects already-approved customer-facing deals from automatic price changes while flagging material changes for retailer review.
- Operates autonomously in background without requiring the frontend to be open.
- Safe against concurrent / duplicate execution.
"""

import asyncio
import logging
from datetime import datetime, timezone
from app.database import get_database
from app.pricing_engine import classify_batch_and_recommend, calculate_shelf_life_metrics

logger = logging.getLogger("freshsave.scheduler")

_is_monitoring_running = False
_last_run_timestamp: datetime | None = None
_last_run_stats: dict = {}


async def run_daily_monitoring_job(db=None) -> dict:
    """
    Executes the daily inventory and pricing monitoring pass.
    Guarded against concurrent execution.
    """
    global _is_monitoring_running, _last_run_timestamp, _last_run_stats

    if _is_monitoring_running:
        logger.info("Daily monitoring job is already running. Skipping duplicate trigger.")
        return {"status": "skipped", "reason": "already_running"}

    _is_monitoring_running = True
    now = datetime.now(timezone.utc)
    today = now.date()
    today_str = today.strftime("%Y-%m-%d")

    if db is None:
        db = get_database()

    stats = {
        "batches_scanned": 0,
        "expired_batches_flagged": 0,
        "depleted_batches_flagged": 0,
        "deals_expired_or_withdrawn": 0,
        "eligible_batches": 0,
        "recommendations_created": 0,
        "recommendations_updated": 0,
        "recommendations_unchanged": 0,
        "approved_deals_flagged_for_review": 0,
        "run_timestamp": now.isoformat(),
    }

    try:
        logger.info("Starting automatic daily inventory monitoring and pricing pass [%s]...", today_str)

        # 1. Fetch store settings for guardrails
        settings_doc = await db.store_settings.find_one({}) or {}

        # 2. Scan all inventory batches
        batches = await db.inventory_batches.find({}).to_list(length=1000)
        stats["batches_scanned"] = len(batches)

        for batch in batches:
            batch_id = batch["_id"]
            qty = int(batch.get("quantity_remaining", 0))
            metrics = calculate_shelf_life_metrics(batch, today=today, now=now)

            # A. Check if batch has expired
            if metrics["is_expired"]:
                if batch.get("status") != "expired":
                    await db.inventory_batches.update_one(
                        {"_id": batch_id},
                        {"$set": {"status": "expired", "updated_at": now}},
                    )
                    stats["expired_batches_flagged"] += 1

                # Deactivate any active customer deals linked to this expired batch
                deal_res = await db.deals.update_many(
                    {"batch_id": batch_id, "status": "active"},
                    {"$set": {"status": "expired", "published_to_customer": False, "updated_at": now}},
                )
                if deal_res.modified_count > 0:
                    stats["deals_expired_or_withdrawn"] += deal_res.modified_count

                # Dismiss any pending recommendations for expired batch
                await db.pricing_recommendations.update_many(
                    {"batch_id": batch_id, "status": "pending"},
                    {
                        "$set": {
                            "status": "expired",
                            "reason": f"Batch expired on {metrics['expiry_date_str']}. Sale and discount strictly blocked.",
                            "updated_at": now,
                        }
                    },
                )
                continue

            # B. Check if batch is depleted
            if qty <= 0:
                if batch.get("status") != "depleted":
                    await db.inventory_batches.update_one(
                        {"_id": batch_id},
                        {"$set": {"status": "depleted", "updated_at": now}},
                    )
                    stats["depleted_batches_flagged"] += 1

                # Mark active deals as sold out
                deal_res = await db.deals.update_many(
                    {"batch_id": batch_id, "status": "active"},
                    {"$set": {"status": "sold_out", "published_to_customer": False, "updated_at": now}},
                )
                if deal_res.modified_count > 0:
                    stats["deals_expired_or_withdrawn"] += deal_res.modified_count
                continue

            # C. Active batch with remaining stock: evaluate eligibility
            if metrics["is_eligible_for_markdown"]:
                stats["eligible_batches"] += 1

            prod = await db.products.find_one({"_id": batch["product_id"]})
            if not prod or not prod.get("active", True):
                continue

            # Compute the single winning recommendation across all 5 strategies
            rec_data = classify_batch_and_recommend(
                batch=batch,
                product=prod,
                settings=settings_doc,
                now=now,
            )
            rec_data.pop("_display", None)

            # Check existing recommendation in DB for this batch
            existing_recs = await db.pricing_recommendations.find({"batch_id": batch_id}).to_list(length=10)
            existing = existing_recs[0] if existing_recs else None

            # Clean up any legacy duplicates
            if len(existing_recs) > 1:
                for dup in existing_recs[1:]:
                    await db.pricing_recommendations.delete_one({"_id": dup["_id"]})

            existing_deal = await db.deals.find_one({
                "batch_id": batch_id,
                "status": "active",
            })

            if existing:
                if existing.get("status") == "approved" or existing_deal is not None:
                    # Do NOT change the approved customer deal price automatically!
                    # Check if material conditions have changed (e.g. batch is now <24h urgent or has new surplus)
                    materially_worsened = (
                        metrics["is_under_24h"]
                        or rec_data["pricing_mode"] != existing.get("pricing_mode")
                        or abs(existing.get("recommended_price", 0) - rec_data["recommended_price"]) >= 2.0
                    )
                    if materially_worsened:
                        rec_data["status"] = "approved"
                        rec_data["material_change_flag"] = True
                        rec_data["updated_at"] = now
                        rec_data["review_note"] = (
                            f"Material condition change detected ({metrics['hours_remaining']}h remaining, "
                            f"{rec_data['pricing_mode'].replace('_', ' ')}). Deal remains live at retailer-approved price. "
                            f"Retailer review recommended."
                        )
                        await db.pricing_recommendations.update_one(
                            {"_id": existing["_id"]},
                            {"$set": rec_data},
                        )
                        stats["approved_deals_flagged_for_review"] += 1
                    else:
                        stats["recommendations_unchanged"] += 1
                elif existing.get("status") == "pending":
                    # Check if recommendation has materially changed to avoid unnecessary writes
                    is_material_change = (
                        existing.get("pricing_mode") != rec_data["pricing_mode"]
                        or abs(existing.get("recommended_price", 0) - rec_data["recommended_price"]) >= 0.50
                        or abs(existing.get("discount_percentage", 0) - rec_data["discount_percentage"]) >= 1.0
                        or existing.get("projected_units_sold") != rec_data["projected_units_sold"]
                        or existing.get("hours_remaining") != rec_data["hours_remaining"]
                    )

                    if is_material_change:
                        rec_data["updated_at"] = now
                        rec_data["status"] = "pending"
                        await db.pricing_recommendations.update_one(
                            {"_id": existing["_id"]},
                            {"$set": rec_data},
                        )
                        stats["recommendations_updated"] += 1
                    else:
                        stats["recommendations_unchanged"] += 1
            else:
                # Insert the new classified recommendation
                rec_data["created_at"] = now
                rec_data["updated_at"] = now
                rec_data["status"] = "approved" if existing_deal else "pending"
                await db.pricing_recommendations.insert_one(rec_data)
                stats["recommendations_created"] += 1

        _last_run_timestamp = now
        _last_run_stats = stats
        logger.info("Daily monitoring pass completed: %s", stats)
        return stats
    except Exception as exc:
        logger.error("Error during daily monitoring pass: %s", exc, exc_info=True)
        return {"status": "error", "error": str(exc), "partial_stats": stats}
    finally:
        _is_monitoring_running = False


async def start_periodic_monitoring_worker(interval_seconds: int = 14400):
    """
    Background worker that runs the monitoring pass periodically (every 4 hours).
    Operates independently of frontend state.
    """
    logger.info("Initializing background periodic pricing monitor (interval: %d seconds)...", interval_seconds)
    # Wait a few seconds on startup to let DB connect
    await asyncio.sleep(5)

    while True:
        try:
            await run_daily_monitoring_job()
        except asyncio.CancelledError:
            logger.info("Periodic pricing monitor cancelled.")
            break
        except Exception as e:
            logger.error("Periodic monitor loop error: %s", e)

        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            logger.info("Periodic pricing monitor cancelled during sleep.")
            break
