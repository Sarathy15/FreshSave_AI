from datetime import datetime, timezone
from bson import ObjectId
from fastapi import APIRouter, HTTPException, Query, status
from pymongo.errors import PyMongoError

from app.database import get_database
from app.schemas import ProductCreate

router = APIRouter(prefix="/api/products", tags=["Products"])


def serialize_product(doc: dict) -> dict:
    sku_val = doc.get("sku")
    if not sku_val:
        cat_prefix = doc.get("category", "GEN")[:3].upper().replace(" ", "")
        sku_val = f"SKU-{cat_prefix}-{str(doc['_id'])[-4:].upper()}"

    return {
        "id": str(doc["_id"]),
        "sku": sku_val,
        "name": doc["name"],
        "category": doc["category"],
        "unit": doc.get("unit", "unit"),
        "unit_cost": float(doc["unit_cost"]),
        "base_price": float(doc["base_price"]),
        "shelf_life_days": int(doc["shelf_life_days"]),
        "average_daily_demand": float(doc["average_daily_demand"]),
        "price_sensitivity": float(doc["price_sensitivity"]),
        "description": doc.get("description"),
        "image_url": doc.get("image_url"),
        "active": bool(doc.get("active", True)),
        "created_at": doc["created_at"],
        "updated_at": doc["updated_at"],
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_product(payload: ProductCreate):
    db = get_database()
    now = datetime.now(timezone.utc)
    document = payload.model_dump(exclude_unset=True)

    sku = payload.sku.strip() if payload.sku and payload.sku.strip() else None
    if sku:
        import re
        escaped_sku = re.escape(sku)
        existing = await db.products.find_one({"sku": {"$regex": f"^{escaped_sku}$", "$options": "i"}})
        if existing:
            raise HTTPException(status_code=400, detail=f"Product with code '{sku}' already exists.")
        document["sku"] = sku
    else:
        new_id = ObjectId()
        document["_id"] = new_id
        cat_prefix = payload.category[:3].upper().replace(" ", "")
        candidate_sku = f"SKU-{cat_prefix}-{str(new_id)[-4:].upper()}"
        suffix = 1
        while await db.products.find_one({"sku": candidate_sku}):
            candidate_sku = f"SKU-{cat_prefix}-{str(new_id)[-4:].upper()}-{suffix}"
            suffix += 1
        document["sku"] = candidate_sku

    document["unit"] = payload.unit or "unit"
    document.update({"created_at": now, "updated_at": now})

    try:
        result = await db.products.insert_one(document)
        inserted_id = getattr(result, "inserted_id", document.get("_id"))
        created = await db.products.find_one({"_id": inserted_id})
        return serialize_product(created)
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail="Could not save product.") from exc


@router.get("")
async def list_products(
    category: str | None = None,
    search: str | None = None,
    active: bool | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    skip: int = Query(default=0, ge=0),
):
    db = get_database()
    filters: dict = {}
    if active is not None:
        filters["active"] = active
    if category and category.lower() != "all":
        filters["category"] = category
    if search and search.strip():
        search_regex = {"$regex": search.strip(), "$options": "i"}
        filters["$or"] = [
            {"name": search_regex},
            {"sku": search_regex},
            {"category": search_regex},
        ]

    try:
        cursor = db.products.find(filters).sort("created_at", -1).skip(skip).limit(limit)
        docs = await cursor.to_list(length=limit)
        return {
            "items": [serialize_product(doc) for doc in docs],
            "limit": limit,
            "skip": skip,
        }
    except PyMongoError as exc:
        raise HTTPException(status_code=500, detail="Could not retrieve products.") from exc


@router.get("/{product_id}")
async def get_product(product_id: str):
    if not ObjectId.is_valid(product_id):
        raise HTTPException(status_code=400, detail="Invalid product ID.")
    db = get_database()
    doc = await db.products.find_one({"_id": ObjectId(product_id)})
    if not doc:
        raise HTTPException(status_code=404, detail="Product not found.")
    return serialize_product(doc)


@router.put("/{product_id}")
async def update_product(product_id: str, payload: ProductCreate):
    if not ObjectId.is_valid(product_id):
        raise HTTPException(status_code=400, detail="Invalid product ID.")
    db = get_database()
    update_data = payload.model_dump(exclude_unset=True)
    update_data["updated_at"] = datetime.now(timezone.utc)

    if payload.sku and payload.sku.strip():
        import re
        sku = payload.sku.strip()
        escaped_sku = re.escape(sku)
        existing = await db.products.find_one({"sku": {"$regex": f"^{escaped_sku}$", "$options": "i"}, "_id": {"$ne": ObjectId(product_id)}})
        if existing:
            raise HTTPException(status_code=400, detail=f"Product with code '{sku}' already exists.")
        update_data["sku"] = sku

    result = await db.products.update_one({"_id": ObjectId(product_id)}, {"$set": update_data})
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Product not found.")
    updated = await db.products.find_one({"_id": ObjectId(product_id)})
    return serialize_product(updated)


@router.delete("/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def deactivate_product(product_id: str):
    if not ObjectId.is_valid(product_id):
        raise HTTPException(status_code=400, detail="Invalid product ID.")
    db = get_database()
    result = await db.products.update_one(
        {"_id": ObjectId(product_id)},
        {"$set": {"active": False, "updated_at": datetime.now(timezone.utc)}},
    )
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Product not found.")
    return None
