"""
Request inputs that repeat across the public API, declared once.

A router asks for them by type (`sede: SedeRequerida`) instead of reading and
checking the headers itself, so a missing header answers the same way on
every route and /docs lists it on every route that needs it.
"""
from typing import Annotated, Optional

from fastapi import Depends, Header, Request, Response

from .errors import ApiError
from .sede import leer_sede_por_defecto
from .security import SecurityError, verify_access_token


async def _sede_requerida(
    response: Response,
    x_holi_sede: Optional[str] = Header(None, alias="X-Holi-Sede"),
) -> str:
    """
    The store a request is answered for: the header, or else the default store
    Odoo marks. The response echoes it in X-Holi-Sede so the app always knows
    whose prices it is showing.
    """
    sede = (x_holi_sede or "").strip().upper()
    if not sede:
        por_defecto = await leer_sede_por_defecto()
        if not por_defecto:
            raise ApiError("parametros-invalidos",
                           "Falta la cabecera X-Holi-Sede y no hay una sede por defecto.",
                           errores=[{"campo": "X-Holi-Sede", "mensaje": "Cabecera obligatoria"}])
        sede = por_defecto["sede"]
    response.headers["X-Holi-Sede"] = sede
    return sede


def _sede_opcional(x_holi_sede: Optional[str] = Header(None, alias="X-Holi-Sede")) -> Optional[str]:
    return (x_holi_sede or "").strip().upper() or None


def _idempotency_key(idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key")) -> str:
    if not (idempotency_key or "").strip():
        raise ApiError("idempotency-key-requerida")
    return idempotency_key.strip()


def _cliente_id(authorization: Optional[str] = Header(None)) -> int:
    """The customer behind the access token, verified locally with no network hop."""
    esquema, _, token = (authorization or "").partition(" ")
    if esquema.lower() != "bearer" or not token.strip():
        raise ApiError("sesion-requerida")
    try:
        return int(verify_access_token(token.strip())["sub"])
    except (SecurityError, KeyError, ValueError):
        raise ApiError("sesion-expirada", "El token de acceso no es válido o expiró.")


def _request_id(request: Request) -> str:
    # Set by the correlation middleware in main.py before any route runs.
    return request.state.request_id


SedeRequerida = Annotated[str, Depends(_sede_requerida)]
SedeOpcional = Annotated[Optional[str], Depends(_sede_opcional)]
IdempotencyKey = Annotated[str, Depends(_idempotency_key)]
RequestId = Annotated[str, Depends(_request_id)]
ClienteId = Annotated[int, Depends(_cliente_id)]
