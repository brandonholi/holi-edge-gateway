import logging

from fastapi import APIRouter, Query

from ..clients.meili_client import MeiliClient
from ..clients.redis_client import get_cache_redis
from ..core.deps import SedeRequerida
from ..schemas.catalog import BusquedaOut
from ..schemas.common import Paginacion
from .catalog import hidratar_productos

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["Search"])
meili = MeiliClient()

POR_PAGINA_MAX = 50


@router.get("/search", response_model=BusquedaOut)
async def buscar_productos(
    sede: SedeRequerida,
    q: str = Query(..., min_length=1, max_length=100),
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(20, ge=1, le=POR_PAGINA_MAX),
):
    """
    Search endpoint (ADR-006):
    1. Meilisearch returns the page of matching SKUs.
    2. The Edge hydrates attributes and prices from Redis holi-cache.
    """
    redis = get_cache_redis()
    inicio = (pagina - 1) * por_pagina

    skus, total = await meili.search_skus(q, sede, limit=por_pagina, offset=inicio)

    # Degraded mode: substring match on the SKU codes held in Redis.
    if not skus and total == 0:
        v_cat = await redis.get(f"v:cat:{sede}") or "1"
        coincidencias = sorted(s for s in await redis.smembers(f"cat:{v_cat}:{sede}") if q.lower() in s.lower())
        total = len(coincidencias)
        skus = coincidencias[inicio:inicio + por_pagina]

    return BusquedaOut(
        sede=sede,
        q=q,
        items=await hidratar_productos(redis, sede, skus),
        paginacion=Paginacion(pagina=pagina, por_pagina=por_pagina, total=total),
    )
