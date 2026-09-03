from typing import List, Optional
from pydantic import BaseModel


class PriceInfo(BaseModel):
    list_price: float
    final_price: float
    discount_percent: float = 0.0
    currency: str = "PEN"
    promocion_id: Optional[str] = None


class ProductOut(BaseModel):
    sku: str
    product_id: int
    name: str
    category_id: Optional[int] = None
    category_name: Optional[str] = None
    uom: str = "UND"
    barcode: Optional[str] = None
    image_url: Optional[str] = None
    price: Optional[PriceInfo] = None
    availability: Optional[str] = None  # disponible, ultimas_unidades, agotado


class CategoryOut(BaseModel):
    id: int
    name: str
    parent_id: Optional[int] = None
    total_skus: int = 0


class AvailabilityOut(BaseModel):
    sku: str
    status: str  # disponible, ultimas_unidades, agotado


class HomeOut(BaseModel):
    sede: str
    featured_skus: List[str] = []
    top_selling_skus: List[str] = []
