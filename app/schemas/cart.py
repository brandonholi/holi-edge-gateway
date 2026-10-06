from typing import List, Optional

from pydantic import BaseModel, Field


class LineaCarrito(BaseModel):
    sku: str
    nombre: Optional[str] = None
    cantidad: float = Field(gt=0)
    precio_unitario: float


class LineaCarritoIn(BaseModel):
    sku: str
    cantidad: float = Field(ge=0, description="0 quita la línea del carrito")


class CarritoOut(BaseModel):
    carrito_id: str
    sede: str
    lineas: List[LineaCarrito] = []
    total: float = 0.0
    cantidad_lineas: int = 0
