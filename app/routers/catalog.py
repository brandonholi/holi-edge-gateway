import json
import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, Query
from redis.asyncio import Redis

from ..clients.odoo_client import OdooClient
from ..clients.redis_client import get_cache_redis
from ..core.deps import RequestId, SedeRequerida
from ..core.errors import ApiError
from ..schemas.catalog import (
    Categoria,
    CategoriasOut,
    DisponibilidadOut,
    InicioOut,
    Precio,
    Producto,
    ProductosOut,
    Promocion,
)
from ..schemas.common import Paginacion

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["Catalog"])
odoo_client = OdooClient()

POR_PAGINA_MAX = 100
# A missing band means "unknown", and unknown stock is never sold as available (ADR-011).
BANDA_SIN_DATO = "agotado"


def banda_o_agotado(banda: Optional[str], sku: str, sede: str) -> str:
    if banda:
        return banda
    _logger.warning("Missing stock band for %s on %s", sku, sede)
    return BANDA_SIN_DATO


def producto_desde_cache(prod: Dict[str, str], price: Optional[Dict[str, str]], banda: str,
                         sku: str = "", sede: str = "") -> Producto:
    """Translates the English Redis hashes into the public product (ADR-012)."""
    precio = None
    if price:
        promocion_id = price.get("promocion_id")
        precio = Precio(
            lista=float(price.get("list_price", 0.0)),
            final=float(price.get("final_price", 0.0)),
            descuento_pct=float(price.get("discount_percent", 0.0)),
            moneda=price.get("currency", "PEN"),
            promocion=Promocion(id=str(promocion_id)) if promocion_id else None,
        )

    return Producto(
        id=int(prod.get("product_id", 0)),
        sku=prod.get("sku", sku),
        nombre=prod.get("name", ""),
        categoria_id=int(prod["category_id"]) if prod.get("category_id") else None,
        categoria=prod.get("category_name") or None,
        unidad=prod.get("uom", "UND"),
        codigo_barras=prod.get("barcode") or None,
        imagen=prod.get("image_url") or None,
        precio=precio,
        disponibilidad=banda_o_agotado(banda, prod.get("sku", sku), sede),
    )


async def hidratar_productos(redis: Redis, sede: str, skus: List[str]) -> List[Producto]:
    """Reads product, price and stock band of each SKU in one round trip."""
    if not skus:
        return []

    v_price = await redis.get(f"v:price:{sede}") or "1"
    pipe = redis.pipeline()
    for sku in skus:
        pipe.hgetall(f"prod:{sku}")
        pipe.hgetall(f"price:{v_price}:{sede}:{sku}")
        pipe.get(f"stock:{sede}:{sku}")
    results = await pipe.execute()

    productos = []
    for i in range(0, len(results), 3):
        if results[i]:
            productos.append(producto_desde_cache(results[i], results[i + 1], results[i + 2], skus[i // 3], sede))
    return productos


@router.get("/catalog", response_model=ProductosOut)
async def listar_catalogo(
    sede: SedeRequerida,
    categoria_id: Optional[int] = Query(None, ge=1),
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(50, ge=1, le=POR_PAGINA_MAX),
):
    """Catalog of the sede, strictly from Redis holi-cache."""
    redis = get_cache_redis()
    v_cat = await redis.get(f"v:cat:{sede}") or "1"

    set_key = f"cat:{v_cat}:{sede}:{categoria_id}" if categoria_id else f"cat:{v_cat}:{sede}"
    # Sorted so a page is stable between two calls; a Redis set has no order.
    skus = sorted(await redis.smembers(set_key))
    inicio = (pagina - 1) * por_pagina

    return ProductosOut(
        sede=sede,
        version_catalogo=int(v_cat),
        items=await hidratar_productos(redis, sede, skus[inicio:inicio + por_pagina]),
        paginacion=Paginacion(pagina=pagina, por_pagina=por_pagina, total=len(skus)),
    )


@router.get("/catalog/{sku}", response_model=Producto)
async def detalle_producto(sku: str, sede: SedeRequerida, request_id: RequestId):
    """Product detail. Falls back to Odoo on cache-miss and hydrates Redis."""
    redis = get_cache_redis()

    v_price = await redis.get(f"v:price:{sede}") or "1"
    prod_data = await redis.hgetall(f"prod:{sku}")
    price_data = await redis.hgetall(f"price:{v_price}:{sede}:{sku}")
    banda = await redis.get(f"stock:{sede}:{sku}")

    if not prod_data or not price_data:
        _logger.info("Cache miss for SKU %s on Sede %s. Hydrating from Odoo...", sku, sede)
        odoo_resp = await odoo_client.fetch_product_sync(sku, sede, request_id)
        master = odoo_resp.get("master", {})
        price = odoo_resp.get("price", {})
        banda = odoo_resp.get("stock_band")

        if master:
            pipe = redis.pipeline()
            pipe.hset(f"prod:{sku}", mapping=master)
            pipe.expire(f"prod:{sku}", 21600)
            pipe.hset(f"price:{v_price}:{sede}:{sku}", mapping=price)
            pipe.expire(f"price:{v_price}:{sede}:{sku}", 21600)
            if banda:
                pipe.setex(f"stock:{sede}:{sku}", 15, banda)
            await pipe.execute()

            prod_data = master
            price_data = price

    if not prod_data:
        raise ApiError("no-encontrado", f"El producto {sku} no se publica en la sede {sede}.")

    return producto_desde_cache(prod_data, price_data, banda, sku, sede)


@router.get("/categories", response_model=CategoriasOut)
async def listar_categorias(sede: SedeRequerida):
    """Category tree of the sede from Redis holi-cache."""
    redis = get_cache_redis()
    v_cat = await redis.get(f"v:cat:{sede}") or "1"

    tree_data = await redis.hgetall(f"tree:{v_cat}:{sede}")
    categorias = []
    for cat_id, cat_json in tree_data.items():
        try:
            item = json.loads(cat_json)
            categorias.append(Categoria(
                id=item.get("id", int(cat_id)),
                nombre=item.get("name", ""),
                padre_id=item.get("parent_id"),
                total_skus=item.get("total_skus", 0),
            ))
        except Exception:
            continue

    return CategoriasOut(sede=sede, items=categorias)


@router.get("/availability/{sku}", response_model=DisponibilidadOut)
async def disponibilidad(sku: str, sede: SedeRequerida):
    """Stock availability band (TTL 15s) from Redis."""
    banda = await get_cache_redis().get(f"stock:{sede}:{sku}")
    return DisponibilidadOut(sku=sku, disponibilidad=banda_o_agotado(banda, sku, sede))


@router.get("/home", response_model=InicioOut)
async def inicio(sede: SedeRequerida):
    """Featured and top selling SKUs for the home screen."""
    top_skus = await get_cache_redis().zrevrange(f"top:{sede}", 0, 9)
    return InicioOut(sede=sede, skus_destacados=top_skus[:5], skus_mas_vendidos=top_skus)
