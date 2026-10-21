from typing import List, Literal, Optional

from pydantic import BaseModel, Field

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


class RecursoApp(BaseModel):
    codigo: str = Field(description="Lugar de la app: portada_inicio · animacion_carga (pueden sumarse otros)")
    url: str = Field(description="URL absoluta en el CDN; cambia cuando cambia el archivo")
    tipo_mime: str = Field(description="application/json (Lottie) · image/webp · image/gif · image/png · image/jpeg")
    hash: str = Field(description="Huella del archivo: volver a descargar solo si cambia")
    actualizado_el: Optional[str] = Field(default=None, description="ISO 8601 UTC")


class RecursosAppOut(Lista[RecursoApp]):
    pass


class CategoriaApp(BaseModel):
    id: int = Field(description="id de la categoría: el mismo de /catalog?categoria_id=")
    nombre: str
    imagen: Optional[ImagenCdn] = Field(default=None, description="Nula si no tiene imagen: usar el ícono por defecto")
    destacada: bool = Field(description="Va en la pantalla principal")


class CategoriasAppOut(Lista[CategoriaApp]):
    pass
