from typing import Optional
from pydantic import BaseModel, Field

from .common import Lista

# ADR-012: Spanish on the public side. The geography follows the official
# Peruvian hierarchy — departamento, provincia, distrito — which is what the
# ubigeo code identifies and what the electronic receipt will carry.


class Departamento(BaseModel):
    id: int
    nombre: str
    codigo: str = Field(default="", description="Código del departamento")


class Provincia(BaseModel):
    id: int
    nombre: str
    codigo: str = Field(default="", description="Código de provincia")


class Distrito(BaseModel):
    id: int
    nombre: str
    codigo: str = Field(default="", description="Ubigeo de 6 dígitos")


class DepartamentosOut(Lista[Departamento]):
    pass


class ProvinciasOut(Lista[Provincia]):
    departamento_id: int


class DistritosOut(Lista[Distrito]):
    provincia_id: int


class TipoDocumento(BaseModel):
    id: int
    codigo: str = Field(default="", description="Código SUNAT del tipo de documento")
    nombre: str
    longitud: int = Field(default=0, description="Largo exacto esperado; 0 si no aplica")
    solo_digitos: bool = True
    ayuda: str = ""
    es_ruc: bool = False


class TiposDocumentoOut(Lista[TipoDocumento]):
    pass


class MensajeInicio(BaseModel):
    id: int
    titulo: str
    mensaje: str
    tipo: str = Field(description="info · aviso · promocion")


class MensajesInicioOut(Lista[MensajeInicio]):
    sede: Optional[str] = None


class TiendaCobertura(BaseModel):
    sede: str = Field(description="Código de sede para la cabecera X-Holi-Sede")
    nombre: str
    distancia_km: Optional[float] = Field(
        default=None, description="Nulo si la tienda no tiene coordenadas cargadas"
    )


class MensajeCobertura(BaseModel):
    titulo: str
    mensaje: str


class CoberturaOut(BaseModel):
    """
    Answer to 'do you deliver here'. Always HTTP 200: being outside the delivery
    area is an ordinary answer, not a failure, and the app shows a message
    rather than an error screen.
    """
    cobertura: bool
    tienda: Optional[TiendaCobertura] = None
    mensaje: Optional[MensajeCobertura] = Field(
        default=None, description="Presente solo cuando cobertura es false"
    )
