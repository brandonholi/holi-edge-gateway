from typing import List, Optional

from pydantic import BaseModel, Field

from .common import Lista, ListaPaginada

# ADR-012: Spanish on the public side. Redis keeps the English hash fields
# (name, final_price...) and the routers translate when building these.


class Promocion(BaseModel):
    id: str


class Precio(BaseModel):
    lista: float
    final: float = Field(description="Ya incluye el descuento. La app no calcula descuentos")
    descuento_pct: float = 0.0
    moneda: str = "PEN"
    promocion: Optional[Promocion] = None


class Producto(BaseModel):
    id: int
    sku: str
    nombre: str
    categoria_id: Optional[int] = None
    categoria: Optional[str] = None
    unidad: str = "UND"
    codigo_barras: Optional[str] = None
    imagen: Optional[str] = None
    precio: Optional[Precio] = None
    disponibilidad: str = Field(default="disponible", description="disponible · ultimas_unidades · agotado")


class ProductosOut(ListaPaginada[Producto]):
    sede: str
    version_catalogo: int


class BusquedaOut(ListaPaginada[Producto]):
    sede: str
    q: str


class Categoria(BaseModel):
    id: int
    nombre: str
    padre_id: Optional[int] = None
    total_skus: int = 0


class CategoriasOut(Lista[Categoria]):
    sede: str


class DisponibilidadOut(BaseModel):
    sku: str
    disponibilidad: str = Field(description="disponible · ultimas_unidades · agotado")


class InicioOut(BaseModel):
    sede: str
    skus_destacados: List[str] = []
    skus_mas_vendidos: List[str] = []
