from datetime import datetime, timezone, date
from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from pymongo.errors import PyMongoError

from app.database import get_database
from app.schemas import (
    InventoryBatchCreate,
    InventoryBatchResponse,
    InventorySummaryResponse,
    BatchUpdateInput,
    StockAdjustmentInput,
    BatchWriteOffInput,
)

router = APIRouter(prefix="/api/inventory", tags=["Inventory & Batches"])


class LegacyInventoryAdjustRequest(BaseModel):
    batch_id: str
    adjustment_units: int = Field(description="Units to add (positive) or write off (negative)")
    reason: str = Field(default="Manual inventory count adjustment")


def compute_expiry_status(expiry_date_str: str) -> tuple[int, str]:
    try:
        exp_date = datetime.strptime(expiry_date_str, "%Y-%m-%d").date()
        today = datetime.now(timezone.utc).date()
        days_left = (exp_date - today).days
    except ValueError:
        days_left = 3

    if days_left < 0:
        return days_left, "expired"
    elif days_left <= 1:
        return max(0, days_left), "critical"
    elif days_left <= 3:
        return days_left, "high_risk"
    elif days_left <= 5:
        return days_left, "medium_risk"
    else:
        return days_left, "active"


async def generate_unique_batch_code(db, product: dict) -> str:
    """
    Generate readable and distinguishable batch codes like MILK-001-B01, MILK-001-B02.
    Uses existing product code / SKU and guarantees uniqueness.
    """
    raw_sku = product.get("sku") or "PRD"
    clean_code = raw_sku.replace("SKU-", "").replace(" ", "").upper()
    if len(clean_code) < 3:
        clean_code = (product.get("name", "PRD")[:3]).upper().replace(" ", "")

    count = await db.inventory_batches.count_documents({"product_id": product["_id"]})
    seq = count + 1
    candidate = f"{clean_code}-B{seq:02d}"

    while await db.inventory_batches.find_one({"batch_number": candidate}):
        seq += 1
        candidate = f"{clean_code}-B{seq:02d}"

    return candidate


def serialize_batch(doc: dict, product: dict | None = None) -> dict:
    expiry_str = doc["expiry_date"]
    days_left, urgency = compute_expiry_status(expiry_str)

    p_name = product.get("name", "Perishable Grocery Item") if product else doc.get("product_name", "Grocery Item")
    p_sku = product.get("sku") if product else doc.get("sku")
    category = product.get("category", "General") if product else doc.get("category", "General")
    base_price = (
        product.get("base_price", doc.get("selling_price", doc.get("unit_cost", 25.0) * 1.3))
        if product
        else doc.get("selling_price", 35.0)
    )

    suggested = "Regular Stock Rotation"
    current_status = doc.get("status", urgency)

    if current_status == "written_off":
        urgency = "written_off"
        suggested = "Written-off Inventory Batch"
    elif doc.get("quantity_remaining", 0) <= 0:
        urgency = "depleted"
        suggested = "Depleted Stock - Archive"
    elif urgency == "expired":
        suggested = "Expired - Write-off Batch"
    elif urgency == "critical":
        suggested = "Urgent 20-30% Markdown Required"
    elif urgency == "high_risk":
        suggested = "Evaluate Dynamic Pricing"

    created = doc.get("created_at")
    created_str = created.isoformat() if isinstance(created, datetime) else str(created or "")
    updated = doc.get("updated_at")
    updated_str = updated.isoformat() if isinstance(updated, datetime) else (str(updated) if updated else None)

    return {
        "id": str(doc["_id"]),
        "product_id": str(doc["product_id"]),
        "product_name": p_name,
        "sku": p_sku,
        "category": category,
        "batch_number": doc["batch_number"],
        "received_date": doc["received_date"],
        "expiry_date": doc["expiry_date"],
        "days_remaining": days_left,
        "original_quantity": doc.get("original_quantity", doc.get("quantity_received", 0)),
        "quantity_remaining": doc.get("quantity_remaining", 0),
        "unit_cost": float(doc["unit_cost"]),
        "selling_price": float(base_price),
        "supplier": doc.get("supplier", "Direct Wholesale"),
        "status": urgency,
        "suggested_action": suggested,
        "adjustments": doc.get("adjustments", []),
        "created_at": created_str,
        "updated_at": updated_str,
    }


@router.post("/batches", status_code=status.HTTP_201_CREATED)
async def create_batch(payload: InventoryBatchCreate):
    """
    Create a new stock batch for an existing product with FEFO tracking,
    unique batch code verification, and date validation.
    """
    db = get_database()
    if not ObjectId.is_valid(payload.product_id):
        raise HTTPException(status_code=400, detail="Invalid product ID.")

    prod = await db.products.find_one({"_id": ObjectId(payload.product_id)})
    if not prod:
        raise HTTPException(status_code=404, detail="Referenced product does not exist.")

    if payload.received_date > payload.expiry_date:
        raise HTTPException(status_code=400, detail="Batch expiry date must be on or after received date.")

    # Determine unique batch code
    if payload.batch_number and payload.batch_number.strip():
        batch_num = payload.batch_number.strip()
        existing = await db.inventory_batches.find_one({"batch_number": batch_num})
        if existing:
            raise HTTPException(
                status_code=400,
                detail=f"Batch code '{batch_num}' already exists. Please provide a unique batch code or leave empty for auto-generation.",
            )
    else:
        batch_num = await generate_unique_batch_code(db, prod)

    _, initial_status = compute_expiry_status(payload.expiry_date)
    now = datetime.now(timezone.utc)
    doc = {
        "product_id": prod["_id"],
        "batch_number": batch_num,
        "original_quantity": payload.quantity_received,
        "quantity_remaining": payload.quantity_received,
        "unit_cost": payload.unit_cost,
        "selling_price": prod["base_price"],
        "received_date": payload.received_date,
        "expiry_date": payload.expiry_date,
        "supplier": payload.supplier or "Direct Wholesale",
        "status": initial_status,
        "adjustments": [],
        "created_at": now,
        "updated_at": now,
    }

    try:
        result = await db.inventory_batches.insert_one(doc)
        inserted_id = getattr(result, "inserted_id", None)
        created = await db.inventory_batches.find_one({"_id": inserted_id})

        # Immediately classify the new batch into its appropriate pricing strategy
        try:
            from app.pricing_engine import classify_batch_and_recommend
            settings_doc = await db.store_settings.find_one({}) or {}
            rec_data = classify_batch_and_recommend(batch=created, product=prod, settings=settings_doc, now=now)
            rec_data.pop("_display", None)
            rec_data["created_at"] = now
            rec_data["updated_at"] = now
            await db.pricing_recommendations.insert_one(rec_data)
        except Exception as rec_err:
            pass

        return serialize_batch(created, prod)
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail="Could not record stock batch.") from exc


@router.get("/batches")
async def list_batches(
    product_id: str | None = None,
    status_filter: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    skip: int = Query(default=0, ge=0),
):
    """List batches ordered by First Expired, First Out (FEFO)."""
    db = get_database()
    query: dict = {}
    if product_id and ObjectId.is_valid(product_id):
        query["product_id"] = ObjectId(product_id)
    if status_filter:
        query["status"] = status_filter

    try:
        cursor = db.inventory_batches.find(query).sort([("expiry_date", 1), ("created_at", 1)]).skip(skip).limit(limit)
        docs = await cursor.to_list(length=limit)

        results = []
        for doc in docs:
            prod = await db.products.find_one({"_id": doc["product_id"]})
            results.append(serialize_batch(doc, prod))
        return {"items": results, "total": len(results)}
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail="Could not retrieve batches.") from exc


@router.get("/batches/{batch_id}")
async def get_batch_details(batch_id: str):
    """Retrieve complete metadata, valuation, and adjustment history for a batch."""
    if not ObjectId.is_valid(batch_id):
        raise HTTPException(status_code=400, detail="Invalid batch ID format.")
    db = get_database()
    batch = await db.inventory_batches.find_one({"_id": ObjectId(batch_id)})
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found.")
    prod = await db.products.find_one({"_id": batch["product_id"]})
    return serialize_batch(batch, prod)


@router.put("/batches/{batch_id}")
async def update_batch_metadata(batch_id: str, payload: BatchUpdateInput):
    """Safe editing of permitted batch metadata (dates, unit cost, supplier)."""
    if not ObjectId.is_valid(batch_id):
        raise HTTPException(status_code=400, detail="Invalid batch ID.")
    db = get_database()
    batch = await db.inventory_batches.find_one({"_id": ObjectId(batch_id)})
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found.")

    updates: dict = {}
    new_received = payload.received_date or batch.get("received_date")
    new_expiry = payload.expiry_date or batch.get("expiry_date")

    if new_received and new_expiry and new_received > new_expiry:
        raise HTTPException(status_code=400, detail="Expiry date must be on or after received date.")

    if payload.received_date:
        updates["received_date"] = payload.received_date
    if payload.expiry_date:
        updates["expiry_date"] = payload.expiry_date
        _, new_status = compute_expiry_status(payload.expiry_date)
        if batch.get("quantity_remaining", 0) > 0 and batch.get("status") != "written_off":
            updates["status"] = new_status
    if payload.unit_cost is not None:
        updates["unit_cost"] = payload.unit_cost
    if payload.supplier is not None:
        updates["supplier"] = payload.supplier

    updates["updated_at"] = datetime.now(timezone.utc)

    await db.inventory_batches.update_one({"_id": batch["_id"]}, {"$set": updates})
    updated = await db.inventory_batches.find_one({"_id": batch["_id"]})
    prod = await db.products.find_one({"_id": batch["product_id"]})
    return serialize_batch(updated, prod)


@router.post("/batches/{batch_id}/adjust")
async def adjust_batch_stock(batch_id: str, payload: StockAdjustmentInput):
    """
    Atomic conditional stock adjustment workflow.
    Supports damaged stock, expired stock, stock corrections, and approved additions.
    Guarantees non-negative stock and records an audit log entry.
    """
    if not ObjectId.is_valid(batch_id):
        raise HTTPException(status_code=400, detail="Invalid batch ID.")

    db = get_database()
    batch = await db.inventory_batches.find_one({"_id": ObjectId(batch_id)})
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found.")

    current_qty = batch.get("quantity_remaining", 0)
    now = datetime.now(timezone.utc)

    if payload.direction == "deduct":
        if payload.quantity > current_qty:
            raise HTTPException(
                status_code=400,
                detail=f"Cannot deduct {payload.quantity} units. Only {current_qty} units currently available in this batch.",
            )

        new_qty = current_qty - payload.quantity
        delta = -payload.quantity
        filter_query = {"_id": batch["_id"], "quantity_remaining": {"$gte": payload.quantity}}
    else:
        new_qty = current_qty + payload.quantity
        delta = payload.quantity
        filter_query = {"_id": batch["_id"]}

    if new_qty == 0:
        new_status = "depleted"
    else:
        _, urgency = compute_expiry_status(batch["expiry_date"])
        new_status = urgency

    adjustment_entry = {
        "adjustment_id": str(ObjectId()),
        "adjustment_type": payload.adjustment_type,
        "direction": payload.direction,
        "quantity_changed": abs(delta),
        "previous_quantity": current_qty,
        "new_quantity": new_qty,
        "reason": payload.reason,
        "notes": payload.notes,
        "timestamp": now.isoformat(),
    }

    result = await db.inventory_batches.update_one(
        filter_query,
        {
            "$inc": {"quantity_remaining": delta},
            "$set": {"status": new_status, "updated_at": now},
            "$push": {"adjustments": adjustment_entry},
        },
    )

    if result.matched_count == 0:
        raise HTTPException(
            status_code=409,
            detail="Stock adjustment conflict: the batch was modified by another operation. Please refresh.",
        )

    updated_batch = await db.inventory_batches.find_one({"_id": batch["_id"]})
    prod = await db.products.find_one({"_id": batch["product_id"]})
    return serialize_batch(updated_batch, prod)


@router.post("/adjust")
async def legacy_adjust_batch(payload: LegacyInventoryAdjustRequest):
    """Backward compatibility wrapper for legacy stock adjustment requests."""
    direction = "add" if payload.adjustment_units > 0 else "deduct"
    qty = abs(payload.adjustment_units)
    adj_type = "approved_addition" if payload.adjustment_units > 0 else "stock_correction"
    new_input = StockAdjustmentInput(
        adjustment_type=adj_type,
        quantity=qty,
        direction=direction,
        reason=payload.reason,
    )
    return await adjust_batch_stock(payload.batch_id, new_input)


@router.post("/batches/{batch_id}/write-off")
async def write_off_batch(batch_id: str, payload: BatchWriteOffInput):
    """
    Safely write off all remaining stock for an expired or damaged batch while preserving history.
    """
    if not ObjectId.is_valid(batch_id):
        raise HTTPException(status_code=400, detail="Invalid batch ID.")

    db = get_database()
    batch = await db.inventory_batches.find_one({"_id": ObjectId(batch_id)})
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found.")

    current_qty = batch.get("quantity_remaining", 0)
    now = datetime.now(timezone.utc)

    adjustment_entry = {
        "adjustment_id": str(ObjectId()),
        "adjustment_type": "expired_stock" if batch.get("status") == "expired" else "damaged_stock",
        "direction": "deduct",
        "quantity_changed": current_qty,
        "previous_quantity": current_qty,
        "new_quantity": 0,
        "reason": payload.reason,
        "notes": payload.notes,
        "timestamp": now.isoformat(),
    }

    await db.inventory_batches.update_one(
        {"_id": batch["_id"]},
        {
            "$set": {
                "quantity_remaining": 0,
                "status": "written_off",
                "updated_at": now,
            },
            "$push": {"adjustments": adjustment_entry},
        },
    )

    # Invalidate/withdraw any pending recommendations or deals for this batch
    await db.pricing_recommendations.update_many(
        {"batch_id": batch["_id"], "status": "pending"},
        {"$set": {"status": "rejected", "updated_at": now}},
    )
    await db.deals.update_many(
        {"batch_id": batch["_id"], "status": "active"},
        {"$set": {"status": "withdrawn", "published_to_customer": False, "updated_at": now}},
    )

    updated_batch = await db.inventory_batches.find_one({"_id": batch["_id"]})
    prod = await db.products.find_one({"_id": batch["product_id"]})
    return serialize_batch(updated_batch, prod)


@router.delete("/batches/{batch_id}")
async def delete_batch(batch_id: str):
    """
    Permanent deletion is permitted ONLY for unused draft batches with 0 historical sales,
    0 active deals, and unconsumed stock. All other batches must be written off or deactivated.
    """
    if not ObjectId.is_valid(batch_id):
        raise HTTPException(status_code=400, detail="Invalid batch ID.")

    db = get_database()
    batch = await db.inventory_batches.find_one({"_id": ObjectId(batch_id)})
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found.")

    # Guard 1: Sales reference check
    sales_count = await db.sales.count_documents({"batch_id": batch["_id"]})
    if sales_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot permanently delete batch: it is referenced by {sales_count} sales transactions. Please use the Write-off action to preserve audit history.",
        )

    # Guard 2: Deals reference check
    deals_count = await db.deals.count_documents({"batch_id": batch["_id"]})
    if deals_count > 0:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot permanently delete batch: it is referenced by {deals_count} customer deals. Please use the Write-off action.",
        )

    # Guard 3: Consumed stock check
    orig_qty = batch.get("original_quantity", 0)
    rem_qty = batch.get("quantity_remaining", 0)
    if rem_qty < orig_qty:
        raise HTTPException(
            status_code=400,
            detail="Cannot delete batch where stock has already been partially consumed. Use Write-off instead.",
        )

    # Safe draft deletion
    await db.inventory_batches.delete_one({"_id": batch["_id"]})
    await db.pricing_recommendations.delete_many({"batch_id": batch["_id"], "status": "pending"})

    return {"message": "Draft batch deleted successfully."}


@router.get("/batches/{batch_id}/adjustments")
async def get_batch_adjustments(batch_id: str):
    """Retrieve full chronological audit trail of stock adjustments for this batch."""
    if not ObjectId.is_valid(batch_id):
        raise HTTPException(status_code=400, detail="Invalid batch ID.")
    db = get_database()
    batch = await db.inventory_batches.find_one({"_id": ObjectId(batch_id)})
    if not batch:
        raise HTTPException(status_code=404, detail="Batch not found.")
    return {"adjustments": batch.get("adjustments", [])}


@router.get("/summary", response_model=InventorySummaryResponse)
async def get_inventory_summary():
    """Compute real inventory KPIs across active products and stock batches."""
    db = get_database()
    try:
        total_products = await db.products.count_documents({"active": True})
        batches = await db.inventory_batches.find({"quantity_remaining": {"$gt": 0}}).to_list(length=1000)

        total_units = 0
        total_cost = 0.0
        near_expiry = 0
        critical = 0
        today = datetime.now(timezone.utc).date()

        for b in batches:
            if b.get("status") == "written_off":
                continue
            qty = b.get("quantity_remaining", 0)
            cost = b.get("unit_cost", 0.0)
            total_units += qty
            total_cost += qty * cost

            try:
                exp_date = datetime.strptime(b["expiry_date"], "%Y-%m-%d").date()
                diff = (exp_date - today).days
                if diff <= 3:
                    near_expiry += 1
                if diff <= 1:
                    critical += 1
            except ValueError:
                pass

        return {
            "total_active_products": total_products,
            "total_available_units": total_units,
            "total_inventory_cost": round(total_cost, 2),
            "batches_approaching_expiry": near_expiry,
            "critical_batches_count": critical,
        }
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail="Could not compute inventory summary.") from exc
