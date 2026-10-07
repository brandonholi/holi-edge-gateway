import json
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Path, Query
from redis.asyncio import Redis

from ..clients.redis_client import get_cache_redis
from ..core.deps import SedeRequerida
from ..core.errors import ApiError
from ..schemas.catalog import ProductosOut
from ..schemas.common import Paginacion
from .catalog import POR_PAGINA_MAX, hidratar_productos

router = APIRouter(prefix="/v1", tags=["Content"])


def ahora() -> datetime:
    """Current UTC time; tests replace it with monkeypatch."""
    return datetime.now(timezone.utc)


async def leer_json(redis: Redis, key: str) -> Optional[Any]:
    """JSON stored in a Redis string; None when missing or corrupt."""
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _fecha(valor: str) -> datetime:
    return datetime.fromisoformat(valor.replace("Z", "+00:00"))


def visible_en_sede(item: dict, sede: str, momento: datetime) -> bool:
    """Store filter plus validity window (inclusive); empty `tiendas` means every store."""
    tiendas = item.get("tiendas") or []
    if tiendas and sede not in tiendas:
        return False
    inicio, fin = item.get("inicio"), item.get("fin")
    if inicio and momento < _fecha(inicio):
        return False
    if fin and momento > _fecha(fin):
        return False
    return True


@router.get("/collections/{coleccion_id}/products", response_model=ProductosOut)
async def productos_de_coleccion(
    sede: SedeRequerida,
    coleccion_id: int = Path(..., ge=1),
    categoria_id: Optional[int] = Query(None, ge=1),
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(50, ge=1, le=POR_PAGINA_MAX),
):
    """Products of a collection in the sede: its categories (with descendants) plus loose SKUs."""
    redis = get_cache_redis()
    coleccion = await leer_json(redis, f"col:{coleccion_id}")
    if not coleccion:
        raise ApiError("no-encontrado", f"La colección {coleccion_id} no existe.")

    categorias = coleccion.get("categorias", [])
    if categoria_id is not None:
        categorias = [c for c in categorias if c["id"] == categoria_id]
        if not categorias:
            raise ApiError("parametros-invalidos", errores=[
                {"campo": "categoria_id", "mensaje": "La categoría no pertenece a la colección."}])

    v_cat = await redis.get(f"v:cat:{sede}") or "1"
    base = f"cat:{v_cat}:{sede}"
    descendientes = sorted({d for c in categorias for d in c.get("descendientes", [c["id"]])})

    pipe = redis.pipeline()
    for d in descendientes:
        pipe.smembers(f"{base}:{d}")
    if categoria_id is None:
        pipe.smembers(base)
    resultados = await pipe.execute()

    encontrados = set().union(*resultados[:len(descendientes)])
    if categoria_id is None:
        sueltos = set(coleccion.get("skus", []))
        encontrados |= sueltos & resultados[-1]

    ordenados = sorted(encontrados)
    inicio = (pagina - 1) * por_pagina
    return ProductosOut(
        sede=sede,
        version_catalogo=int(v_cat),
        items=await hidratar_productos(redis, sede, ordenados[inicio:inicio + por_pagina]),
        paginacion=Paginacion(pagina=pagina, por_pagina=por_pagina, total=len(ordenados)),
    )
