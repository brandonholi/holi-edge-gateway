from typing import List, Optional

from pydantic import BaseModel, Field

from .common import Lista


class LineaCompraIn(BaseModel):
    sku: str
    cantidad: float = Field(gt=0)
    precio_unitario: Optional[float] = None


class CompraIn(BaseModel):
    lineas: List[LineaCompraIn]
    cupon_codigo: Optional[str] = None
    franja_id: Optional[str] = None
    direccion_id: Optional[int] = None
    notas: Optional[str] = None


class CompraOut(BaseModel):
    pedido_id: int
    numero: str
    importe_total: float
    estado: str
    idempotente: bool = Field(default=False, description="true si es la respuesta guardada de un intento anterior")


class Franja(BaseModel):
    id: str
    nombre: str
    disponible: bool = True


class FranjasOut(Lista[Franja]):
    sede: str
