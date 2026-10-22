from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from app.database import get_database

router = APIRouter(prefix="/api/sales", tags=["Sales & Orders"])


class SaleCreateRequest(BaseModel):
    product_id: str
    units: int = Field(gt=0, le=100)
    order_id: str | None = None
    custom_price: float | None = None


def serialize_sale(doc: dict) -> dict:
    ts = doc.get("sale_timestamp") or doc.get("created_at")
    ts_str = ts.strftime("%Y-%m-%d %H:%M") if isinstance(ts, datetime) else str(doc.get("date", ts or ""))

    return {
        "id": str(doc["_id"]),
        "order_id": doc.get("order_id", "ORD-UNKNOWN"),
        "date": ts_str,
        "product_name": doc.get("product_name", "Product"),
        "category": doc.get("category", "Grocery"),
        "batch_number": doc.get("batch_number", "BATCH-01"),
        "units_sold": int(doc.get("units_sold", 1)),
        "unit_cost": float(doc.get("unit_cost", 20.0)),
        "selling_price": float(doc.get("selling_price", 30.0)),
        "revenue": float(doc.get("revenue", 30.0)),
        "gross_profit": float(doc.get("gross_profit", 10.0)),
        "is_deal": bool(doc.get("is_deal", False)),
    }


@router.get("")
async def list_sales(limit: int = Query(default=50, ge=1, le=200)):
    db = get_database()
    docs = await db.sales.find({}).sort("sale_timestamp", -1).limit(limit).to_list(length=limit)
    if not docs:
        docs = await db.sales.find({}).sort("date", -1).limit(limit).to_list(length=limit)
    return {"items": [serialize_sale(d) for d in docs]}


@router.post("/record", status_code=status.HTTP_201_CREATED)
async def record_sale_order(payload: SaleCreateRequest):
    """
    Fulfill order using FEFO stock deduction and record sale transaction.
    Uses safe atomic conditional updates to prevent race conditions and overselling.
    """
    db = get_database()
    now = datetime.now(timezone.utc)

    if not ObjectId.is_valid(payload.product_id):
        raise HTTPException(status_code=400, detail="Invalid product ID.")

    prod = await db.products.find_one({"_id": ObjectId(payload.product_id)})
    if not prod:
        raise HTTPException(status_code=404, detail="Product not found.")

    today_str = now.strftime("%Y-%m-%d")

    # Find earliest expiring eligible unexpired batch (FEFO order)
    batches = await db.inventory_batches.find({
        "product_id": ObjectId(payload.product_id),
        "quantity_remaining": {"$gt": 0},
        "expiry_date": {"$gte": today_str},
    }).sort([("expiry_date", 1), ("created_at", 1)]).to_list(length=10)

    if not batches:
        raise HTTPException(
            status_code=400,
            detail="Cannot fulfill sale: No eligible, unexpired inventory batches available.",
        )

    batch = batches[0]
    available = batch.get("quantity_remaining", 0)
    if available < payload.units:
        raise HTTPException(
            status_code=400,
            detail=f"Insufficient batch quantity. Requested {payload.units}, available in batch: {available}.",
        )

    # Check if there is an active deal for this product or batch
    deal = await db.deals.find_one({
        "product_id": ObjectId(payload.product_id),
        "status": "active",
    })
    is_deal = deal is not None
    selling_price = float(payload.custom_price or (deal["deal_price"] if is_deal else prod["base_price"]))
    unit_cost = float(batch.get("unit_cost", prod.get("unit_cost", 20.0)))

    revenue = round(payload.units * selling_price, 2)
    cogs = round(payload.units * unit_cost, 2)
    gross_profit = round(revenue - cogs, 2)

    # 1. Safe Atomic conditional stock deduction: prevents concurrency races without distributed transaction locks
    update_res = await db.inventory_batches.update_one(
        {
            "_id": batch["_id"],
            "quantity_remaining": {"$gte": payload.units},
        },
        {
            "$inc": {"quantity_remaining": -payload.units},
            "$set": {"updated_at": now},
        },
    )

    if update_res.matched_count == 0:
        raise HTTPException(
            status_code=400,
            detail=f"Concurrent stock deduction failed. Insufficient inventory in batch {batch.get('batch_number')}.",
        )

    # If remaining stock depleted, update batch status
    refreshed_batch = await db.inventory_batches.find_one({"_id": batch["_id"]})
    if refreshed_batch and refreshed_batch.get("quantity_remaining", 0) <= 0:
        await db.inventory_batches.update_one(
            {"_id": batch["_id"]},
            {"$set": {"status": "depleted", "updated_at": now}},
        )

    # 2. If deal active, atomically increment deal quantity sold
    if is_deal:
        await db.deals.update_one(
            {"_id": deal["_id"]},
            {"$inc": {"quantity_sold": payload.units}, "$set": {"updated_at": now}},
        )
        refreshed_deal = await db.deals.find_one({"_id": deal["_id"]})
        if refreshed_deal and refreshed_deal.get("quantity_sold", 0) >= refreshed_deal.get("quantity_allocated", 100):
            await db.deals.update_one(
                {"_id": deal["_id"]},
                {"$set": {"status": "sold_out", "updated_at": now}},
            )

    # 3. Create sale record in db.sales
    order_id = payload.order_id or f"ORD-{now.strftime('%H%M%S%f')[:10]}"
    sale_doc = {
        "order_id": order_id,
        "product_id": prod["_id"],
        "batch_id": batch["_id"],
        "units_sold": payload.units,
        "unit_cost": unit_cost,
        "selling_price": selling_price,
        "revenue": revenue,
        "gross_profit": gross_profit,
        "is_deal": is_deal,
        "sale_timestamp": now,
        "date": now.strftime("%Y-%m-%d %H:%M"),
        "product_name": prod["name"],
        "category": prod["category"],
        "batch_number": batch.get("batch_number", "BATCH-01"),
        "created_at": now,
    }
    result = await db.sales.insert_one(sale_doc)
    created_id = getattr(result, "inserted_id", sale_doc.get("_id"))
    created = await db.sales.find_one({"_id": created_id})

    return serialize_sale(created)
