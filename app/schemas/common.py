from typing import Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

# Shapes shared by every endpoint of the public API. The conventions they
# encode are written down in CLAUDE.md and in section 0-ter of the contract.

T = TypeVar("T")


class Paginacion(BaseModel):
    pagina: int = Field(description="Página actual, desde 1")
    por_pagina: int
    total: int = Field(description="Total de elementos en todas las páginas")


class Lista(BaseModel, Generic[T]):
    """
    Every list response. A list is never a bare JSON array: the object leaves
    room for context fields (sede, departamento_id) and for new optional fields
    without breaking the app.
    """
    items: List[T]


class ListaPaginada(Lista[T], Generic[T]):
    paginacion: Paginacion


class MensajeOut(BaseModel):
    """Answer of an action that has nothing else to return."""
    mensaje: str


class ErrorCampo(BaseModel):
    campo: str
    mensaje: str


class Problema(BaseModel):
    """RFC 7807 problem document (application/problem+json). See app/core/errors.py."""
    model_config = ConfigDict(extra="allow")

    type: str = Field(description="https://api.holi.com.pe/errors/{codigo}")
    title: str
    status: int
    detail: str
    instance: str
    request_id: str
    errores: Optional[List[ErrorCampo]] = Field(
        default=None, description="Solo en parametros-invalidos: qué campo falló y por qué"
    )


RESPUESTAS_ERROR = {
    "4XX": {"model": Problema, "description": "Error del cliente (application/problem+json)"},
    "5XX": {"model": Problema, "description": "Error del servidor (application/problem+json)"},
    422: {"model": Problema, "description": "Parámetros inválidos (application/problem+json)"},
}
