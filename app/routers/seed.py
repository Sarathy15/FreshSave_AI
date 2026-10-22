from datetime import datetime, timezone, timedelta
from bson import ObjectId
from fastapi import APIRouter
from app.database import get_database

router = APIRouter(prefix="/api/seed", tags=["Database Seed"])


@router.post("")
async def seed_database():
    """Seed the database with initial realistic perishable grocery inventory and batches."""
    db = get_database()
    now = datetime.now(timezone.utc)
    today = now.date()

    # 1. Store Settings (Stable Identifier)
    settings_doc = await db.store_settings.find_one({"_id": "default_store_settings"})
    if not settings_doc:
        await db.store_settings.update_one(
            {"_id": "default_store_settings"},
            {"$set": {
                "store_name": "Metro Superstore #104",
                "max_discount_percent": 35.0,
                "min_margin_percent": 5.0,
                "expiry_warning_days": 3,
                "auto_approve_near_expiry": False,
                "max_discount": 35,
                "min_margin": 5,
                "auto_approve_clearance": False,
                "currency": "INR (₹)",
                "updated_at": now,
            }},
            upsert=True,
        )

    # 2. Check if products already exist
    existing_count = await db.products.count_documents({})
    if existing_count > 0:
        return {"message": f"Database already has {existing_count} products. No seed needed."}

    # Standard Catalog with SKU and unit
    catalog = [
        {
            "sku": "SKU-DAI-MILK1",
            "name": "Fresh Whole Farm Milk (1L)",
            "category": "Dairy & Eggs",
            "unit": "liter",
            "unit_cost": 25.0,
            "base_price": 35.0,
            "shelf_life_days": 5,
            "average_daily_demand": 18.5,
            "price_sensitivity": 1.8,
            "description": "Pasteurized homogenized pure whole milk, locally sourced.",
            "image_url": "https://images.unsplash.com/photo-1550583724-b2692b85b150?w=400&auto=format&fit=crop&q=80",
            "active": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "sku": "SKU-BAK-SOUR1",
            "name": "Artisan Whole Wheat Sourdough",
            "category": "Bakery",
            "unit": "pack",
            "unit_cost": 20.0,
            "base_price": 32.0,
            "shelf_life_days": 4,
            "average_daily_demand": 12.0,
            "price_sensitivity": 2.1,
            "description": "Freshly baked artisanal whole wheat loaf, zero preservatives.",
            "image_url": "https://images.unsplash.com/photo-1509440159596-0249088772ff?w=400&auto=format&fit=crop&q=80",
            "active": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "sku": "SKU-DAI-YOGU1",
            "name": "Greek Probiotic Plain Yogurt (400g)",
            "category": "Dairy & Eggs",
            "unit": "pack",
            "unit_cost": 45.0,
            "base_price": 65.0,
            "shelf_life_days": 10,
            "average_daily_demand": 9.0,
            "price_sensitivity": 1.4,
            "description": "Thick strained probiotic yogurt high in protein.",
            "image_url": "https://images.unsplash.com/photo-1488477181946-6428a0291777?w=400&auto=format&fit=crop&q=80",
            "active": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "sku": "SKU-PRO-STRA1",
            "name": "Organic Sweet Strawberries (250g)",
            "category": "Produce",
            "unit": "pack",
            "unit_cost": 60.0,
            "base_price": 95.0,
            "shelf_life_days": 4,
            "average_daily_demand": 7.5,
            "price_sensitivity": 2.4,
            "description": "Sweet, juicy farm-picked organic strawberries in eco-punnet.",
            "image_url": "https://images.unsplash.com/photo-1464965911861-746a04b4bca6?w=400&auto=format&fit=crop&q=80",
            "active": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "sku": "SKU-DAI-PANE1",
            "name": "Fresh Malai Paneer (200g)",
            "category": "Dairy & Eggs",
            "unit": "pack",
            "unit_cost": 55.0,
            "base_price": 80.0,
            "shelf_life_days": 6,
            "average_daily_demand": 14.0,
            "price_sensitivity": 1.6,
            "description": "Soft traditional cottage cheese made from fresh cows milk.",
            "image_url": "https://images.unsplash.com/photo-1631452180519-c014fe946bc7?w=400&auto=format&fit=crop&q=80",
            "active": True,
            "created_at": now,
            "updated_at": now,
        },
        {
            "sku": "SKU-PRO-SPIN1",
            "name": "Crisp Baby Spinach Leaves (200g)",
            "category": "Produce",
            "unit": "pack",
            "unit_cost": 18.0,
            "base_price": 28.0,
            "shelf_life_days": 3,
            "average_daily_demand": 15.0,
            "price_sensitivity": 2.2,
            "description": "Hydroponic washed tender baby spinach leaves ready to eat.",
            "image_url": "https://images.unsplash.com/photo-1576045057995-568f588f82fb?w=400&auto=format&fit=crop&q=80",
            "active": True,
            "created_at": now,
            "updated_at": now,
        },
    ]

    inserted_prods = []
    for item in catalog:
        res = await db.products.insert_one(dict(item))
        ins_id = getattr(res, "inserted_id", item.get("_id"))
        p = await db.products.find_one({"_id": ins_id})
        inserted_prods.append(p)

    # 3. Insert Inventory Batches
    batches_data = [
        # Milk Batch 1 (Critical: 1 day left)
        {
            "product_id": inserted_prods[0]["_id"],
            "batch_number": "BATCH-MILK-24A",
            "original_quantity": 40,
            "quantity_remaining": 16,
            "unit_cost": 25.0,
            "selling_price": 35.0,
            "received_date": (today - timedelta(days=2)).strftime("%Y-%m-%d"),
            "expiry_date": (today + timedelta(days=1)).strftime("%Y-%m-%d"),
            "supplier": "Metro Dairy Cooperative",
            "status": "critical",
            "created_at": now,
            "updated_at": now,
        },
        # Milk Batch 2 (Normal: 4 days left)
        {
            "product_id": inserted_prods[0]["_id"],
            "batch_number": "BATCH-MILK-25B",
            "original_quantity": 50,
            "quantity_remaining": 42,
            "unit_cost": 25.0,
            "selling_price": 35.0,
            "received_date": today.strftime("%Y-%m-%d"),
            "expiry_date": (today + timedelta(days=4)).strftime("%Y-%m-%d"),
            "supplier": "Metro Dairy Cooperative",
            "status": "active",
            "created_at": now,
            "updated_at": now,
        },
        # Bread Batch (Critical: 1 day left)
        {
            "product_id": inserted_prods[1]["_id"],
            "batch_number": "BATCH-BREAD-89",
            "original_quantity": 30,
            "quantity_remaining": 18,
            "unit_cost": 20.0,
            "selling_price": 32.0,
            "received_date": (today - timedelta(days=2)).strftime("%Y-%m-%d"),
            "expiry_date": (today + timedelta(days=1)).strftime("%Y-%m-%d"),
            "supplier": "Golden Crust Bakers",
            "status": "critical",
            "created_at": now,
            "updated_at": now,
        },
        # Strawberries (High Risk: 2 days left)
        {
            "product_id": inserted_prods[3]["_id"],
            "batch_number": "BATCH-STR-12",
            "original_quantity": 25,
            "quantity_remaining": 14,
            "unit_cost": 60.0,
            "selling_price": 95.0,
            "received_date": (today - timedelta(days=1)).strftime("%Y-%m-%d"),
            "expiry_date": (today + timedelta(days=2)).strftime("%Y-%m-%d"),
            "supplier": "Green Valley Organic Farms",
            "status": "high_risk",
            "created_at": now,
            "updated_at": now,
        },
        # Paneer (Medium Risk: 4 days left)
        {
            "product_id": inserted_prods[4]["_id"],
            "batch_number": "BATCH-PAN-19",
            "original_quantity": 40,
            "quantity_remaining": 28,
            "unit_cost": 55.0,
            "selling_price": 80.0,
            "received_date": today.strftime("%Y-%m-%d"),
            "expiry_date": (today + timedelta(days=4)).strftime("%Y-%m-%d"),
            "supplier": "Metro Dairy Cooperative",
            "status": "medium_risk",
            "created_at": now,
            "updated_at": now,
        },
    ]

    inserted_batches = []
    for b in batches_data:
        res = await db.inventory_batches.insert_one(dict(b))
        ins_b_id = getattr(res, "inserted_id", b.get("_id"))
        b_doc = await db.inventory_batches.find_one({"_id": ins_b_id})
        inserted_batches.append(b_doc)

    # 4. Insert Initial Deals with explicit batch_id reference
    await db.deals.insert_one({
        "product_id": inserted_prods[4]["_id"],
        "batch_id": inserted_batches[4]["_id"],
        "recommendation_id": None,
        "product_name": inserted_prods[4]["name"],
        "category": inserted_prods[4]["category"],
        "batch_number": "BATCH-PAN-19",
        "original_price": 80.0,
        "deal_price": 68.0,
        "discount_percentage": 15.0,
        "quantity_allocated": 28,
        "quantity_sold": 6,
        "start_date": (today - timedelta(days=1)).strftime("%Y-%m-%d"),
        "end_date": (today + timedelta(days=3)).strftime("%Y-%m-%d"),
        "status": "active",
        "published_to_customer": True,
        "created_at": now,
        "updated_at": now,
    })

    # 5. Insert Initial Recorded Sales with batch_id and sale_timestamp
    await db.sales.insert_one({
        "order_id": "ORD-9842",
        "product_id": inserted_prods[0]["_id"],
        "batch_id": inserted_batches[0]["_id"],
        "product_name": inserted_prods[0]["name"],
        "category": inserted_prods[0]["category"],
        "batch_number": "BATCH-MILK-24A",
        "units_sold": 4,
        "unit_cost": 25.0,
        "selling_price": 28.0,
        "revenue": 112.0,
        "gross_profit": 12.0,
        "is_deal": True,
        "sale_timestamp": now,
        "date": now.strftime("%Y-%m-%d %H:%M"),
        "created_at": now,
    })

    await db.sales.insert_one({
        "order_id": "ORD-9841",
        "product_id": inserted_prods[1]["_id"],
        "batch_id": inserted_batches[2]["_id"],
        "product_name": inserted_prods[1]["name"],
        "category": inserted_prods[1]["category"],
        "batch_number": "BATCH-BREAD-89",
        "units_sold": 2,
        "unit_cost": 20.0,
        "selling_price": 32.0,
        "revenue": 64.0,
        "gross_profit": 24.0,
        "is_deal": False,
        "sale_timestamp": now - timedelta(hours=2),
        "date": (now - timedelta(hours=2)).strftime("%Y-%m-%d %H:%M"),
        "created_at": now,
    })

    return {"message": "Database seeded successfully with initial products, batches, deals, and sales!"}
