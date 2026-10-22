from datetime import datetime, timezone
from fastapi import APIRouter
from pydantic import BaseModel, Field
from app.database import get_database

router = APIRouter(prefix="/api/settings", tags=["Store Settings"])

DEFAULT_STORE_SETTINGS_ID = "default_store_settings"


class StoreSettingsPayload(BaseModel):
    store_name: str = Field(default="Metro Superstore #104", min_length=2)
    max_discount_percent: float | None = Field(default=None, ge=5, le=80)
    min_margin_percent: float | None = Field(default=None, ge=0, le=50)
    expiry_warning_days: int = Field(default=3, ge=1, le=14)
    auto_approve_near_expiry: bool | None = Field(default=None)
    # Frontend backward-compatibility aliases
    max_discount: int | None = Field(default=None, ge=5, le=80)
    min_margin: int | None = Field(default=None, ge=0, le=50)
    auto_approve_clearance: bool | None = Field(default=None)
    currency: str = Field(default="INR (₹)")


@router.get("")
async def get_store_settings():
    db = get_database()
    doc = await db.store_settings.find_one({"_id": DEFAULT_STORE_SETTINGS_ID})
    if not doc:
        doc = await db.store_settings.find_one({})

    if not doc:
        return {
            "store_name": "Metro Superstore #104",
            "max_discount_percent": 35.0,
            "min_margin_percent": 5.0,
            "expiry_warning_days": 3,
            "auto_approve_near_expiry": False,
            "max_discount": 35,
            "min_margin": 5,
            "auto_approve_clearance": False,
            "currency": "INR (₹)",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    max_disc = doc.get("max_discount_percent", doc.get("max_discount", 35.0))
    min_marg = doc.get("min_margin_percent", doc.get("min_margin", 5.0))
    auto_app = doc.get("auto_approve_near_expiry", doc.get("auto_approve_clearance", False))
    up_at = doc.get("updated_at")

    return {
        "store_name": doc.get("store_name", "Metro Superstore #104"),
        "max_discount_percent": float(max_disc),
        "min_margin_percent": float(min_marg),
        "expiry_warning_days": int(doc.get("expiry_warning_days", 3)),
        "auto_approve_near_expiry": bool(auto_app),
        "max_discount": int(max_disc),
        "min_margin": int(min_marg),
        "auto_approve_clearance": bool(auto_app),
        "currency": doc.get("currency", "INR (₹)"),
        "updated_at": up_at.isoformat() if isinstance(up_at, datetime) else str(up_at or ""),
    }


@router.put("")
async def update_store_settings(payload: StoreSettingsPayload):
    """Upsert store settings under the single stable identifier."""
    db = get_database()
    now = datetime.now(timezone.utc)

    max_disc = payload.max_discount_percent if payload.max_discount_percent is not None else (
        payload.max_discount if payload.max_discount is not None else 35.0
    )
    min_marg = payload.min_margin_percent if payload.min_margin_percent is not None else (
        payload.min_margin if payload.min_margin is not None else 5.0
    )
    auto_app = payload.auto_approve_near_expiry if payload.auto_approve_near_expiry is not None else (
        payload.auto_approve_clearance if payload.auto_approve_clearance is not None else False
    )

    doc_data = {
        "store_name": payload.store_name,
        "max_discount_percent": float(max_disc),
        "min_margin_percent": float(min_marg),
        "expiry_warning_days": int(payload.expiry_warning_days),
        "auto_approve_near_expiry": bool(auto_app),
        "max_discount": int(max_disc),
        "min_margin": int(min_marg),
        "auto_approve_clearance": bool(auto_app),
        "currency": payload.currency,
        "updated_at": now,
    }

    await db.store_settings.update_one(
        {"_id": DEFAULT_STORE_SETTINGS_ID},
        {"$set": doc_data},
        upsert=True,
    )

    resp_data = dict(doc_data)
    resp_data["updated_at"] = now.isoformat()
    return {"message": "Store settings updated successfully", "settings": resp_data}
