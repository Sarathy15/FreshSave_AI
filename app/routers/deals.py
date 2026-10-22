from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, HTTPException, status
from pymongo.errors import PyMongoError

from app.database import get_database

router = APIRouter(prefix="/api/deals", tags=["Deals Management"])


ALLOWED_DEAL_STATUSES = {"active", "pending", "sold_out", "expired", "withdrawn", "paused"}


def serialize_deal(doc: dict) -> dict:
    return {
        "id": str(doc["_id"]),
        "product_id": str(doc["product_id"]),
        "product_name": doc.get("product_name", "Grocery Item"),
        "category": doc.get("category", "Grocery"),
        "batch_number": doc.get("batch_number", "BATCH-01"),
        "original_price": float(doc["original_price"]),
        "deal_price": float(doc["deal_price"]),
        "discount_percentage": float(doc["discount_percentage"]),
        "quantity_allocated": int(doc["quantity_allocated"]),
        "quantity_sold": int(doc.get("quantity_sold", 0)),
        "start_date": str(doc["start_date"]),
        "end_date": str(doc["end_date"]),
        "status": doc.get("status", "active"),
        "published_to_customer": bool(doc.get("published_to_customer", True)),
    }


@router.get("")
async def list_deals():
    db = get_database()
    try:
        docs = await db.deals.find({}).sort("created_at", -1).to_list(length=50)
        return {"items": [serialize_deal(d) for d in docs]}
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail="Could not retrieve deals.") from exc


@router.patch("/{deal_id}/status")
async def update_deal_status(deal_id: str, payload: dict):
    db = get_database()
    if not ObjectId.is_valid(deal_id):
        raise HTTPException(status_code=400, detail="Invalid deal ID.")

    new_status = payload.get("status")
    published = payload.get("published_to_customer")

    if new_status and new_status not in ALLOWED_DEAL_STATUSES:
        raise HTTPException(status_code=400, detail=f"Invalid deal status '{new_status}'. Allowed: {ALLOWED_DEAL_STATUSES}")

    update_fields = {"updated_at": datetime.now(timezone.utc)}
    if new_status:
        update_fields["status"] = new_status
    if published is not None:
        update_fields["published_to_customer"] = bool(published)

    try:
        res = await db.deals.update_one({"_id": ObjectId(deal_id)}, {"$set": update_fields})
        if res.matched_count == 0:
            raise HTTPException(status_code=404, detail="Deal not found.")
        updated = await db.deals.find_one({"_id": ObjectId(deal_id)})
        return serialize_deal(updated)
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail="Could not update deal.") from exc
