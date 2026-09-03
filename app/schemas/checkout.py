from typing import List, Optional
from pydantic import BaseModel, Field


class CheckoutLineItem(BaseModel):
    sku: str
    qty: float = Field(gt=0)
    price_unit: Optional[float] = None


class CheckoutIn(BaseModel):
    lines: List[CheckoutLineItem]
    coupon_code: Optional[str] = None
    delivery_slot_id: Optional[str] = None
    delivery_address_id: Optional[int] = None
    notes: Optional[str] = None


class CheckoutOut(BaseModel):
    order_id: int
    order_name: str
    amount_total: float
    state: str
    idempotent: bool = False


class DeliverySlot(BaseModel):
    slot_id: str
    name: str
    available: bool = True


class DeliverySlotsOut(BaseModel):
    sede: str
    slots: List[DeliverySlot] = []
