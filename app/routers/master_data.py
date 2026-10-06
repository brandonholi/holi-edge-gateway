import json
import logging
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Query

from ..clients.odoo_client import OdooClient
from ..clients.redis_client import get_cache_redis
from ..core.config import settings
from ..core.deps import RequestId, SedeOpcional
from ..core.geo import resolve_zone
from ..schemas.master_data import (
    CoberturaOut,
    DepartamentosOut,
    DistritosOut,
    MensajeCobertura,
    MensajesInicioOut,
    ProvinciasOut,
    TiendaCobertura,
    TiposDocumentoOut,
)

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["Master Data"])
odoo_client = OdooClient()


async def _cached(cache_key: str, ttl: int, fetch: Callable) -> Dict[str, Any]:
    """
    Reads through holi-cache to Odoo.

    None of this changes between two sign-ups, so serving it from Odoo on every
    app launch would spend the semaphore of 20 on a list of districts. Redis
    being down only makes it slower, never broken: the fetch still happens.
    Odoo failures propagate and are answered by the handlers in core/errors.py.
    """
    cache_redis = None
    try:
        cache_redis = get_cache_redis()
        cached = await cache_redis.get(cache_key)
        if cached:
            return json.loads(cached)
    except Exception as e:
        _logger.warning("Master data cache unavailable for %s: %s", cache_key, e)

    data = await fetch()

    if cache_redis is not None:
        try:
            await cache_redis.set(cache_key, json.dumps(data), ex=ttl)
        except Exception as e:
            _logger.warning("Could not cache master data for %s: %s", cache_key, e)

    return data


# --------------------------------------------------------------------------- #
# Geography — official ubigeo catalogues for the address and the receipt
# --------------------------------------------------------------------------- #

@router.get("/locations/departments", response_model=DepartamentosOut)
async def listar_departamentos(request_id: RequestId):
    """
    All departments of Peru.

    This is reference data for the address, not a statement about where we
    deliver — that is answered per coordinate by /coverage. Filtering these
    lists by coverage would be a bug: a point can fall inside a delivery zone
    whose district was never flagged, and the customer would not find their own
    district in the selector.

    No X-Holi-Sede here either: the address is what decides the sede.
    """
    data = await _cached(
        "md:loc:dep",
        settings.MASTER_DATA_TTL_SECONDS,
        lambda: odoo_client.fetch_departments(request_id),
    )
    return DepartamentosOut(items=data.get("departamentos", []))


@router.get("/locations/provinces", response_model=ProvinciasOut)
async def listar_provincias(request_id: RequestId, departamento_id: int = Query(..., ge=1)):
    data = await _cached(
        f"md:loc:prov:{departamento_id}",
        settings.MASTER_DATA_TTL_SECONDS,
        lambda: odoo_client.fetch_provinces(departamento_id, request_id),
    )
    return ProvinciasOut(departamento_id=departamento_id, items=data.get("provincias", []))


@router.get("/locations/districts", response_model=DistritosOut)
async def listar_distritos(request_id: RequestId, provincia_id: int = Query(..., ge=1)):
    """
    Districts of a province, with the six-digit ubigeo.

    The ubigeo is what the address record and the electronic receipt need, and
    a coordinate alone does not give it — which is why these catalogues survive
    the move to polygons.
    """
    data = await _cached(
        f"md:loc:dist:{provincia_id}",
        settings.MASTER_DATA_TTL_SECONDS,
        lambda: odoo_client.fetch_districts(provincia_id, request_id),
    )
    return DistritosOut(provincia_id=provincia_id, items=data.get("distritos", []))


# --------------------------------------------------------------------------- #
# Identity document types
# --------------------------------------------------------------------------- #

async def tipos_documento(cliente: OdooClient, request_id: str) -> list:
    """The document types the channel accepts. Sign-up validates against these."""
    data = await _cached(
        "md:doctypes",
        settings.MASTER_DATA_TTL_SECONDS,
        lambda: cliente.fetch_document_types(request_id),
    )
    return data.get("tipos_documento", [])


@router.get("/document-types", response_model=TiposDocumentoOut)
async def listar_tipos_documento(request_id: RequestId):
    """
    Identity documents the channel accepts, in the order the selector shows them.

    These come from the Peruvian localization catalogue (DNI, RUC and the rest),
    flagged one by one in the backoffice. The app renders what arrives and does
    not carry its own list.
    """
    return TiposDocumentoOut(items=await tipos_documento(odoo_client, request_id))


# --------------------------------------------------------------------------- #
# Delivery coverage — the polygon decides, per coordinate
# --------------------------------------------------------------------------- #

@router.get("/coverage", response_model=CoberturaOut)
async def resolver_cobertura(
    request_id: RequestId,
    lat: float = Query(..., ge=-18.5, le=0.0, description="Latitud del punto"),
    lng: float = Query(..., ge=-81.5, le=-68.5, description="Longitud del punto"),
):
    """
    Answers whether a point can be delivered to, and by which store.

    Always HTTP 200. Being outside the delivery area is an ordinary answer, and
    an error status would push the app into an error screen instead of the
    message it should be showing.

    The latitude and longitude bounds are Peru's. A coordinate outside them is
    almost always the two values swapped, and rejecting it with 422 says so far
    more clearly than answering that we do not deliver to the Pacific.
    """
    data = await _cached(
        "md:coverage:zones",
        settings.COVERAGE_ZONES_TTL_SECONDS,
        lambda: odoo_client.fetch_coverage_zones(request_id),
    )

    match = resolve_zone(lat, lng, data.get("zonas", []))

    if not match:
        mensaje = data.get("mensaje_sin_cobertura") or {}
        return CoberturaOut(
            cobertura=False,
            tienda=None,
            mensaje=MensajeCobertura(
                titulo=mensaje.get("titulo", "Aún no llegamos a tu zona"),
                mensaje=mensaje.get(
                    "mensaje",
                    "Por ahora no tenemos tiendas que repartan a esta dirección.",
                ),
            ),
        )

    zona = match["zona"]
    return CoberturaOut(
        cobertura=True,
        tienda=TiendaCobertura(
            sede=zona.get("sede", ""),
            nombre=zona.get("tienda", ""),
            distancia_km=match["distancia_km"],
        ),
        mensaje=None,
    )


# --------------------------------------------------------------------------- #
# Home screen messages
# --------------------------------------------------------------------------- #

@router.get("/home-messages", response_model=MensajesInicioOut)
async def listar_mensajes_inicio(
    request_id: RequestId,
    sede_cabecera: SedeOpcional,
    sede: Optional[str] = Query(default=None, description="Opcional: limita a una sede"),
):
    """
    Editorial messages for the opening screen, before signing in.

    Read with no session, so it carries nothing customer-specific. The window of
    each message is resolved in Odoo, which is why this is cached for minutes
    and not for a day: a notice taken down has to disappear quickly.
    """
    sede_code = (sede or "").strip().upper() or sede_cabecera

    data = await _cached(
        f"md:home:{sede_code or '_'}",
        settings.HOME_MESSAGES_TTL_SECONDS,
        lambda: odoo_client.fetch_home_messages(sede_code, request_id),
    )
    return MensajesInicioOut(sede=sede_code, items=data.get("mensajes", []))
