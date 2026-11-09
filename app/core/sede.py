"""
The default store, published by Odoo to holi-cache.

It is the store the app shows before the customer picks an address: a request
that arrives without X-Holi-Sede is answered with its catalog, prices and
stock. Odoo writes the key on every change, and writes a JSON null when no
store is marked, so a stale key never keeps serving a store that was unmarked.
"""
import json
import logging
from typing import Optional, TypedDict

from ..clients.redis_client import get_cache_redis
from .errors import ApiError

_logger = logging.getLogger(__name__)

CLAVE_SEDE_POR_DEFECTO = "content:default_store"


class SedePorDefecto(TypedDict):
    sede: str
    nombre: str


async def leer_sede_por_defecto() -> Optional[SedePorDefecto]:
    """
    The default store, or None when Odoo has none marked.

    Redis being down is a 503, not a None: answering "no default store" would
    tell the app to ask for an address when the store is simply unreadable.
    """
    try:
        crudo = await get_cache_redis().get(CLAVE_SEDE_POR_DEFECTO)
    except Exception as e:
        _logger.error("Could not read the default store from holi-cache: %s", e)
        raise ApiError("servicio-no-disponible", "No se pudo leer la sede por defecto.")

    try:
        dato = json.loads(crudo) if crudo else None
    except ValueError:
        _logger.error("The default store key holds invalid JSON")
        return None
    if not isinstance(dato, dict) or not dato.get("sede"):
        return None
    sede = str(dato["sede"]).strip().upper()
    return {"sede": sede, "nombre": str(dato.get("nombre") or sede)}
