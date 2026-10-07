from typing import List, Literal, Optional

from pydantic import BaseModel

from .common import Lista


class ImagenCdn(BaseModel):
    md: Optional[str] = None
    lg: Optional[str] = None


class Destino(BaseModel):
    tipo: Literal["coleccion", "categoria", "producto", "mundo"]
    id: str


class ImagenBanner(BaseModel):
    imagen: ImagenCdn
    texto_alternativo: str
    etiqueta: Optional[str] = None
    titulo: Optional[str] = None
    destino: Destino


class Banner(BaseModel):
    id: int
    tipo: Literal["carrusel", "estatico"]
    ubicacion: Literal["principal", "categoria"]
    categoria_id: Optional[int] = None
    imagenes: List[ImagenBanner]


class BannersOut(Lista[Banner]):
    pass


class Mundo(BaseModel):
    id: int
    nombre: str
    subtitulo: Optional[str] = None
    imagen: Optional[ImagenCdn] = None
    coleccion_id: int


class MundosOut(Lista[Mundo]):
    pass


class CategoriaChip(BaseModel):
    id: int
    nombre: str


class MundoDetalle(Mundo):
    categorias: List[CategoriaChip]
