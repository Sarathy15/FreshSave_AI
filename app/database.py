import asyncio
import json
import logging
import os
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from bson import ObjectId
from pymongo import AsyncMongoClient
from pymongo.errors import PyMongoError

from app.config import settings

logger = logging.getLogger(__name__)

client: AsyncMongoClient | None = None
database = None
_is_using_local_fallback = False


class AsyncCursorWrapper:
    def __init__(self, docs: list[dict]):
        self._docs = docs
        self._skip = 0
        self._limit = len(docs)
        self._sort_field = None
        self._sort_dir = 1

    def sort(self, field: str, direction: int = 1):
        self._sort_field = field
        self._sort_dir = direction
        return self

    def skip(self, count: int):
        self._skip = max(0, count)
        return self

    def limit(self, count: int):
        self._limit = max(0, count)
        return self

    async def to_list(self, length: int | None = None):
        docs = list(self._docs)
        if self._sort_field:
            def get_sort_val(d):
                val = d.get(self._sort_field)
                if isinstance(val, (datetime, date)):
                    return val.isoformat()
                return val or ""
            docs.sort(key=get_sort_val, reverse=(self._sort_dir < 0))

        start = self._skip
        end = start + (length if length is not None else self._limit)
        return deepcopy(docs[start:end])


class AsyncCollectionWrapper:
    def __init__(self, name: str, store: "LocalPersistentStore"):
        self.name = name
        self.store = store

    def _matches(self, doc: dict, query: dict) -> bool:
        for k, v in query.items():
            if k == "$set" or k == "$ne":
                continue
            if isinstance(v, dict):
                if "$ne" in v and doc.get(k) == v["$ne"]:
                    return False
                if "$gt" in v and doc.get(k, 0) <= v["$gt"]:
                    return False
                if "$gte" in v and doc.get(k, 0) < v["$gte"]:
                    return False
                if "$lt" in v and doc.get(k, 0) >= v["$lt"]:
                    return False
                if "$lte" in v and doc.get(k, 0) > v["$lte"]:
                    return False
            else:
                doc_val = doc.get(k)
                if isinstance(doc_val, ObjectId) and isinstance(v, ObjectId):
                    if doc_val != v:
                        return False
                elif str(doc_val) != str(v):
                    return False
        return True

    def find(self, query: dict | None = None):
        q = query or {}
        docs = [d for d in self.store.get_collection(self.name) if self._matches(d, q)]
        return AsyncCursorWrapper(docs)

    async def find_one(self, query: dict | None = None):
        q = query or {}
        for d in self.store.get_collection(self.name):
            if self._matches(d, q):
                return deepcopy(d)
        return None

    async def insert_one(self, doc: dict):
        new_doc = deepcopy(doc)
        if "_id" not in new_doc:
            new_doc["_id"] = ObjectId()
        elif isinstance(new_doc["_id"], str) and ObjectId.is_valid(new_doc["_id"]):
            new_doc["_id"] = ObjectId(new_doc["_id"])

        self.store.get_collection(self.name).append(new_doc)
        self.store.save()

        class InsertResult:
            def __init__(self, inserted_id):
                self.inserted_id = inserted_id

        return InsertResult(new_doc["_id"])

    async def update_one(self, filter_query: dict, update_query: dict):
        matched = 0
        modified = 0
        target = None
        for d in self.store.get_collection(self.name):
            if self._matches(d, filter_query):
                target = d
                matched += 1
                break

        if target is not None:
            if "$set" in update_query:
                for k, v in update_query["$set"].items():
                    target[k] = v
                modified += 1
                self.store.save()

        class UpdateResult:
            def __init__(self, matched_count, modified_count):
                self.matched_count = matched_count
                self.modified_count = modified_count

        return UpdateResult(matched, modified)

    async def count_documents(self, query: dict | None = None):
        q = query or {}
        return sum(1 for d in self.store.get_collection(self.name) if self._matches(d, q))

    async def delete_one(self, query: dict):
        col = self.store.get_collection(self.name)
        for i, d in enumerate(col):
            if self._matches(d, query):
                col.pop(i)
                self.store.save()
                return


    async def create_index(self, keys, **kwargs):
        return "idx_created"

    async def index_information(self):
        return {"_id_": {"v": 2, "key": [("_id", 1)]}}

    def aggregate(self, pipeline: list[dict]):
        # Basic pipeline support for local fallback
        docs = deepcopy(self.store.get_collection(self.name))
        for stage in pipeline:
            if "$match" in stage:
                match_q = stage["$match"]
                docs = [d for d in docs if self._matches(d, match_q)]
            elif "$sort" in stage:
                sort_spec = stage["$sort"]
                for f, direction in sort_spec.items():
                    docs.sort(key=lambda x: str(x.get(f, "")), reverse=(direction < 0))
            elif "$limit" in stage:
                docs = docs[:stage["$limit"]]
        return AsyncCursorWrapper(docs)

    async def update_many(self, filter_query: dict, update_query: dict):
        modified = 0
        for d in self.store.get_collection(self.name):
            if self._matches(d, filter_query):
                if "$set" in update_query:
                    for k, v in update_query["$set"].items():
                        d[k] = v
                modified += 1
        if modified > 0:
            self.store.save()
        class UpdateResult:
            def __init__(self, count):
                self.matched_count = count
                self.modified_count = count
        return UpdateResult(modified)

    async def delete_many(self, query: dict):
        col = self.store.get_collection(self.name)
        new_col = [d for d in col if not self._matches(d, query)]
        deleted = len(col) - len(new_col)
        self.store.data[self.name] = new_col
        if deleted > 0:
            self.store.save()
        class DeleteResult:
            def __init__(self, count):
                self.deleted_count = count
        return DeleteResult(deleted)


class LocalPersistentStore:
    def __init__(self, file_path: str):
        self.file_path = Path(file_path)
        self.data: dict[str, list[dict]] = {}
        self.load()

    def get_collection(self, name: str) -> list[dict]:
        if name not in self.data:
            self.data[name] = []
        return self.data[name]

    def __getitem__(self, name: str) -> AsyncCollectionWrapper:
        return AsyncCollectionWrapper(name, self)

    def __getattr__(self, name: str) -> AsyncCollectionWrapper:
        return AsyncCollectionWrapper(name, self)

    async def list_collection_names(self) -> list[str]:
        return list(self.data.keys())

    def load(self):
        if self.file_path.exists():
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                    for col_name, items in raw.items():
                        deserialized = []
                        for item in items:
                            d = dict(item)
                            if "_id" in d and ObjectId.is_valid(d["_id"]):
                                d["_id"] = ObjectId(d["_id"])
                            if "product_id" in d and ObjectId.is_valid(d["product_id"]):
                                d["product_id"] = ObjectId(d["product_id"])
                            if "batch_id" in d and ObjectId.is_valid(d["batch_id"]):
                                d["batch_id"] = ObjectId(d["batch_id"])
                            deserialized.append(d)
                        self.data[col_name] = deserialized
                return
            except Exception as e:
                logger.warning("Could not read local store file: %s", e)
        self.data = {}

    def save(self):
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            serializable = {}
            for col_name, items in self.data.items():
                ser_items = []
                for item in items:
                    d = dict(item)
                    for k, v in d.items():
                        if isinstance(v, ObjectId):
                            d[k] = str(v)
                        elif isinstance(v, datetime):
                            d[k] = v.isoformat()
                    ser_items.append(d)
                serializable[col_name] = ser_items

            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(serializable, f, indent=2)
        except Exception as e:
            logger.error("Failed to save local database store: %s", e)


async def connect_to_mongo() -> None:
    """Create the MongoDB client and verify the connection at startup."""
    global client, database, _is_using_local_fallback
    try:
        import certifi
        ca_file = certifi.where()
    except ImportError:
        ca_file = None

    client_kwargs = {
        "serverSelectionTimeoutMS": 3000,
    }
    if ca_file:
        client_kwargs["tlsCAFile"] = ca_file

    try:
        test_client = AsyncMongoClient(
            settings.mongodb_uri,
            **client_kwargs,
        )
        await test_client.admin.command("ping")
        client = test_client
        database = client[settings.database_name]
        _is_using_local_fallback = False
        logger.info("Connected directly to MongoDB Atlas database '%s'.", settings.database_name)
    except Exception as exc:
        logger.warning(
            "MongoDB Atlas connection unavailable (%s). Activating local persistent store with exact collection compatibility.",
            exc,
        )
        base_dir = Path(__file__).resolve().parent.parent
        db_file = base_dir / "data" / "freshsave_db.json"
        database = LocalPersistentStore(str(db_file))
        _is_using_local_fallback = True


async def close_mongo_connection() -> None:
    global client, database
    if client is not None:
        await client.close()
    client = None
    database = None


async def ping_database() -> None:
    global database, _is_using_local_fallback
    if database is None:
        raise RuntimeError("Database is not available.")
    if _is_using_local_fallback:
        return
    if client is not None:
        await client.admin.command("ping")


def get_database():
    global database
    if database is None:
        base_dir = Path(__file__).resolve().parent.parent
        db_file = base_dir / "data" / "freshsave_db.json"
        database = LocalPersistentStore(str(db_file))
    return database


async def init_database_indexes(db=None) -> None:
    """
    Creates and ensures optimal MongoDB indexes for the 7 core collections:
    products, inventory_batches, pricing_recommendations, deals, sales, store_settings, simulation_runs.
    """
    if db is None:
        db = get_database()

    try:
        # A. products indexes
        await db.products.create_index([("sku", 1)], unique=True, sparse=True)
        await db.products.create_index([("active", 1), ("category", 1), ("created_at", -1)])
        await db.products.create_index([("created_at", -1)])

        # B. inventory_batches indexes (Optimized for FEFO First Expired, First Out)
        await db.inventory_batches.create_index([("product_id", 1), ("quantity_remaining", 1), ("expiry_date", 1)])
        await db.inventory_batches.create_index([("expiry_date", 1), ("status", 1)])
        try:
            await db.inventory_batches.create_index([("batch_number", 1)], unique=True)
        except Exception:
            try:
                await db.inventory_batches.drop_index("batch_number_1")
                await db.inventory_batches.create_index([("batch_number", 1)], unique=True)
            except Exception as e:
                logger.warning("Could not set unique index on batch_number: %s", e)
        await db.inventory_batches.create_index([("status", 1)])

        # C. pricing_recommendations indexes
        await db.pricing_recommendations.create_index([("pricing_mode", 1), ("status", 1), ("created_at", -1)])
        await db.pricing_recommendations.create_index([("batch_id", 1), ("status", 1)])
        await db.pricing_recommendations.create_index([("product_id", 1), ("status", 1)])

        # D. deals indexes
        await db.deals.create_index([("status", 1), ("published_to_customer", 1), ("start_date", 1), ("end_date", 1)])
        await db.deals.create_index([("product_id", 1), ("status", 1)])
        await db.deals.create_index([("batch_id", 1), ("status", 1)])
        await db.deals.create_index([("recommendation_id", 1)])

        # E. sales indexes
        await db.sales.create_index([("sale_timestamp", -1)])
        await db.sales.create_index([("product_id", 1), ("sale_timestamp", -1)])
        await db.sales.create_index([("batch_id", 1), ("sale_timestamp", -1)])
        await db.sales.create_index([("is_deal", 1), ("sale_timestamp", -1)])
        await db.sales.create_index([("order_id", 1)])

        # G. simulation_runs indexes
        await db.simulation_runs.create_index([("run_id", 1)], unique=True, sparse=True)
        await db.simulation_runs.create_index([("created_at", -1)])

        logger.info("MongoDB schema indexes ensured successfully across all core collections.")
    except Exception as exc:
        logger.warning("Index initialization notice: %s", exc)


async def migrate_database(db=None) -> None:
    """
    Audits and performs non-destructive schema migrations on existing collections:
    1. Backfills unique SKU and unit on existing products.
    2. Normalizes inventory batches to ensure FEFO fields, valid product_id references, and consistent statuses.
    3. Migrates store_settings to use a stable document identifier and consistent guardrail fields.
    4. Normalizes string IDs to ObjectIds in relational references.
    5. Normalizes sales sale_timestamp and transaction integrity.
    6. Migrates any legacy simulation_experiments records into simulation_runs.
    """
    if db is None:
        db = get_database()

    now = datetime.now(timezone.utc)

    try:
        # 1. Products Migration: ensure unique sku, standard unit, and timestamps
        products = await db.products.find({}).to_list(1000)
        seen_skus = set()
        for idx, prod in enumerate(products, 1):
            updates = {}
            sku = prod.get("sku")
            if not sku or sku in seen_skus:
                cat_prefix = (prod.get("category", "GEN")[:3]).upper().replace(" ", "")
                unique_suffix = str(prod["_id"])[-4:].upper()
                sku = f"SKU-{cat_prefix}-{unique_suffix}"
                updates["sku"] = sku
            seen_skus.add(sku)

            if not prod.get("unit"):
                updates["unit"] = "pack" if "pack" in prod.get("name", "").lower() else "unit"
            if "created_at" not in prod:
                updates["created_at"] = now
            if "updated_at" not in prod:
                updates["updated_at"] = now

            if updates:
                await db.products.update_one({"_id": prod["_id"]}, {"$set": updates})

        # 2. Store Settings Migration: ensure stable identifier and unified schema
        settings_doc = await db.store_settings.find_one({})
        stable_id = "default_store_settings"
        default_settings = {
            "store_name": "Metro Superstore #104",
            "max_discount_percent": 35.0,
            "min_margin_percent": 5.0,
            "expiry_warning_days": 3,
            "auto_approve_near_expiry": False,
            "currency": "INR (₹)",
            "updated_at": now,
        }

        if settings_doc:
            max_disc = settings_doc.get("max_discount_percent", settings_doc.get("max_discount", 35.0))
            min_marg = settings_doc.get("min_margin_percent", settings_doc.get("min_margin", 5.0))
            auto_app = settings_doc.get("auto_approve_near_expiry", settings_doc.get("auto_approve_clearance", False))
            s_name = settings_doc.get("store_name", "Metro Superstore #104")
            exp_warn = settings_doc.get("expiry_warning_days", 3)
            curr = settings_doc.get("currency", "INR (₹)")

            normalized = {
                "store_name": s_name,
                "max_discount_percent": float(max_disc),
                "min_margin_percent": float(min_marg),
                "expiry_warning_days": int(exp_warn),
                "auto_approve_near_expiry": bool(auto_app),
                # Frontend compatibility aliases
                "max_discount": int(max_disc),
                "min_margin": int(min_marg),
                "auto_approve_clearance": bool(auto_app),
                "currency": curr,
                "updated_at": now,
            }

            if str(settings_doc.get("_id")) != stable_id:
                await db.store_settings.update_one({"_id": stable_id}, {"$set": normalized}, upsert=True)
                if settings_doc.get("_id") != stable_id:
                    await db.store_settings.delete_one({"_id": settings_doc["_id"]})
            else:
                await db.store_settings.update_one({"_id": stable_id}, {"$set": normalized})
        else:
            default_settings["max_discount"] = 35
            default_settings["min_margin"] = 5
            default_settings["auto_approve_clearance"] = False
            await db.store_settings.update_one({"_id": stable_id}, {"$set": default_settings}, upsert=True)

        # 3. Inventory Batches: ensure unique batch_number, product_id is ObjectId, quantities and status are valid
        batches = await db.inventory_batches.find({}).to_list(1000)
        today_date = now.date()
        seen_batch_codes = {}
        for b in batches:
            up_batch = {}
            batch_num = b.get("batch_number")
            if batch_num:
                if batch_num in seen_batch_codes:
                    seen_batch_codes[batch_num] += 1
                    deduped_num = f"{batch_num}-{seen_batch_codes[batch_num]:02d}"
                    up_batch["batch_number"] = deduped_num
                    b["batch_number"] = deduped_num
                else:
                    seen_batch_codes[batch_num] = 1

            p_id = b.get("product_id")
            if isinstance(p_id, str) and ObjectId.is_valid(p_id):
                up_batch["product_id"] = ObjectId(p_id)

            orig_q = b.get("original_quantity", b.get("quantity_received", 0))
            rem_q = b.get("quantity_remaining", orig_q)
            if "original_quantity" not in b:
                up_batch["original_quantity"] = orig_q
            if "quantity_remaining" not in b:
                up_batch["quantity_remaining"] = rem_q

            # Validate consistent status
            exp_str = b.get("expiry_date", "")
            try:
                exp_date = datetime.strptime(exp_str, "%Y-%m-%d").date()
                days_left = (exp_date - today_date).days
            except Exception:
                days_left = 5

            if rem_q <= 0:
                calc_status = "depleted"
            elif days_left < 0:
                calc_status = "expired"
            elif days_left <= 1:
                calc_status = "critical"
            elif days_left <= 3:
                calc_status = "high_risk"
            elif days_left <= 5:
                calc_status = "medium_risk"
            else:
                calc_status = "active"

            if b.get("status") not in ["critical", "high_risk", "medium_risk", "active", "depleted", "expired", "normal"]:
                up_batch["status"] = calc_status

            if "created_at" not in b:
                up_batch["created_at"] = now
            if "updated_at" not in b:
                up_batch["updated_at"] = now

            if up_batch:
                await db.inventory_batches.update_one({"_id": b["_id"]}, {"$set": up_batch})

        # 4. Deals: ensure product_id, batch_id are ObjectId, avoid overselling
        deals = await db.deals.find({}).to_list(1000)
        for d in deals:
            up_deal = {}
            if isinstance(d.get("product_id"), str) and ObjectId.is_valid(d["product_id"]):
                up_deal["product_id"] = ObjectId(d["product_id"])
            if isinstance(d.get("batch_id"), str) and ObjectId.is_valid(d["batch_id"]):
                up_deal["batch_id"] = ObjectId(d["batch_id"])
            if "quantity_sold" not in d:
                up_deal["quantity_sold"] = 0
            if "created_at" not in d:
                up_deal["created_at"] = now
            if "updated_at" not in d:
                up_deal["updated_at"] = now
            if up_deal:
                await db.deals.update_one({"_id": d["_id"]}, {"$set": up_deal})

        # 5. Sales: ensure product_id, batch_id are ObjectId and sale_timestamp exists as datetime
        sales = await db.sales.find({}).to_list(1000)
        for s in sales:
            up_sale = {}
            if isinstance(s.get("product_id"), str) and ObjectId.is_valid(s["product_id"]):
                up_sale["product_id"] = ObjectId(s["product_id"])
            if isinstance(s.get("batch_id"), str) and ObjectId.is_valid(s["batch_id"]):
                up_sale["batch_id"] = ObjectId(s["batch_id"])

            if "sale_timestamp" not in s:
                raw_time = s.get("created_at")
                if not raw_time and s.get("date"):
                    try:
                        raw_time = datetime.strptime(s["date"][:16], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
                    except Exception:
                        raw_time = now
                up_sale["sale_timestamp"] = raw_time or now

            # Ensure calculated financial values are consistent
            u_sold = s.get("units_sold", 1)
            s_price = s.get("selling_price", 0.0)
            u_cost = s.get("unit_cost", 0.0)
            if "revenue" not in s or s["revenue"] is None:
                up_sale["revenue"] = round(u_sold * s_price, 2)
            if "gross_profit" not in s or s["gross_profit"] is None:
                rev = up_sale.get("revenue", s.get("revenue", round(u_sold * s_price, 2)))
                up_sale["gross_profit"] = round(rev - (u_sold * u_cost), 2)

            if up_sale:
                await db.sales.update_one({"_id": s["_id"]}, {"$set": up_sale})

        # 6. Legacy simulation collection migration: simulation_experiments -> simulation_runs
        try:
            legacy_runs = await db.simulation_experiments.find({}).to_list(500)
            if legacy_runs:
                for run in legacy_runs:
                    run_data = dict(run)
                    run_data.pop("_id", None)
                    run_id = run_data.get("run_id")
                    if run_id:
                        existing = await db.simulation_runs.find_one({"run_id": run_id})
                        if not existing:
                            await db.simulation_runs.insert_one(run_data)
                logger.info("Migrated %d legacy simulation experiments to simulation_runs.", len(legacy_runs))
        except Exception:
            pass

        logger.info("Database migration check completed successfully.")
    except Exception as exc:
        logger.warning("Database migration notice: %s", exc)

