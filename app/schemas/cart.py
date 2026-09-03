from typing import List, Optional
from pydantic import BaseModel, Field


class CartLine(BaseModel):
    sku: str
    qty: float = Field(gt=0)
    price_unit: float
    name: Optional[str] = None


class CartUpdateIn(BaseModel):
    sku: str
    qty: float = Field(ge=0)


class CartOut(BaseModel):
    cart_id: str
    sede: str
    lines: List[CartLine] = []
    total_amount: float = 0.0
    items_count: int = 0
