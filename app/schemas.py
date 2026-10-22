from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProductCreate(BaseModel):
    sku: str | None = Field(default=None, max_length=50, description="Unique product SKU identifier")
    name: str = Field(min_length=2, max_length=120)
    category: str = Field(min_length=2, max_length=60)
    unit: str = Field(default="unit", min_length=1, max_length=30, description="Standard unit (e.g. unit, pack, kg, liter)")
    unit_cost: float = Field(gt=0, description="Wholesale cost per unit")
    base_price: float = Field(gt=0, description="Regular retail selling price")
    shelf_life_days: int = Field(gt=0, le=3650, description="Total shelf life in days")
    average_daily_demand: float = Field(ge=0, description="Estimated average units sold per day")
    price_sensitivity: float = Field(ge=0, le=10, description="Demand price elasticity / sensitivity")
    description: str | None = Field(default=None, max_length=1000)
    image_url: str | None = Field(default=None, max_length=2000)
    active: bool = True

    @field_validator("base_price")
    @classmethod
    def price_must_cover_cost(cls, value, info):
        return value


class ProductResponse(ProductCreate):
    id: str
    sku: str
    unit: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


# --- Inventory & Batch Schemas ---

class InventoryBatchCreate(BaseModel):
    product_id: str
    batch_number: str | None = Field(default=None, max_length=50, description="Unique batch code (auto-generated if omitted)")
    quantity_received: int = Field(gt=0, description="Initial batch quantity (original_quantity)")
    unit_cost: float = Field(gt=0, description="Acquisition cost per unit")
    received_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    expiry_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    supplier: str | None = Field(default="Direct Wholesale")


class BatchUpdateInput(BaseModel):
    received_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    expiry_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    unit_cost: float | None = Field(default=None, gt=0)
    supplier: str | None = Field(default=None, max_length=120)


class StockAdjustmentInput(BaseModel):
    adjustment_type: Literal["damaged_stock", "expired_stock", "stock_correction", "approved_addition"]
    quantity: int = Field(gt=0, description="Quantity of units to adjust")
    direction: Literal["deduct", "add"] = "deduct"
    reason: str = Field(min_length=2, max_length=250, description="Audit justification for stock adjustment")
    notes: str | None = Field(default=None, max_length=500)


class BatchWriteOffInput(BaseModel):
    reason: str = Field(min_length=2, max_length=250, description="Audit reason for writing off remaining stock")
    notes: str | None = Field(default=None, max_length=500)


class InventoryBatchResponse(BaseModel):
    id: str
    product_id: str
    product_name: str
    sku: str | None = None
    category: str
    batch_number: str
    received_date: str
    expiry_date: str
    days_remaining: int
    original_quantity: int
    quantity_remaining: int
    unit_cost: float
    selling_price: float
    supplier: str | None = None
    status: Literal["critical", "high_risk", "medium_risk", "normal", "active", "depleted", "expired", "written_off"]
    suggested_action: str | None = None
    adjustments: list[dict] = []
    created_at: str | None = None
    updated_at: str | None = None

    model_config = ConfigDict(from_attributes=True)


class InventorySummaryResponse(BaseModel):
    total_active_products: int
    total_available_units: int
    total_inventory_cost: float
    batches_approaching_expiry: int
    critical_batches_count: int


# --- Smart Pricing & Deals Schemas ---

class PricingRecommendationResponse(BaseModel):
    id: str
    product_id: str
    product_name: str
    category: str
    image_url: str | None = None
    batch_id: str
    batch_number: str
    unit_cost: float
    current_price: float
    recommended_price: float
    discount_percentage: float
    stock_remaining: int
    days_to_expiry: int
    hours_remaining: int = 0
    projected_units_sold: int
    projected_revenue: float
    projected_profit: float
    projected_waste_units: int
    reason: str
    pricing_mode: Literal["balanced", "max_profit", "near_expiry", "clearance", "bulk"]
    strategy_name: str = "Balanced Smart"
    status: Literal["pending", "approved", "rejected", "expired"] = "pending"
    guardrails_passed: bool = True
    min_allowed_price: float = 0.0
    shelf_life_elapsed_pct: float = 0.0
    expected_demand: float = 0.0
    surplus_units: int = 0
    bundle_details: dict | None = None
    why_selected: str | None = None
    why_others_not_selected: dict[str, str] = {}
    strategy_evaluations: dict = {}
    material_change_flag: bool = False
    eligibility_status: Literal["eligible", "ineligible", "expired", "depleted"] = "eligible"
    received_date: str | None = None
    expiry_date: str | None = None
    decision_reason: str | None = None
    alternatives_considered: list[dict] = []
    created_at: str
    updated_at: str | None = None


class DealApprovalRequest(BaseModel):
    custom_price: float | None = None


class DealResponse(BaseModel):
    id: str
    product_id: str
    product_name: str
    category: str
    batch_number: str
    original_price: float
    deal_price: float
    discount_percentage: float
    quantity_allocated: int
    quantity_sold: int
    start_date: str
    end_date: str
    status: Literal["active", "pending", "sold_out", "expired", "withdrawn", "paused"]
    published_to_customer: bool


# --- Store Settings Schemas ---

class StoreSettingsResponse(BaseModel):
    store_name: str
    max_discount_percent: float
    min_margin_percent: float
    expiry_warning_days: int
    auto_approve_near_expiry: bool
    currency: str = "INR (₹)"
    # Frontend backward compatibility aliases
    max_discount: int
    min_margin: int
    auto_approve_clearance: bool
    updated_at: datetime | str | None = None


# --- Simulation Schemas ---

class SimulationRunRequest(BaseModel):
    duration_days: int = Field(default=30, ge=7, le=60)
    sensitivity_mode: Literal["baseline", "half_response"] = "baseline"
    random_seed: int = Field(default=42, ge=1)


class MessageResponse(BaseModel):
    message: str


class RoleInfo(BaseModel):
    role: Literal["retailer", "customer"]
    note: str
