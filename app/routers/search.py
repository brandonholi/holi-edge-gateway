import logging
from typing import List, Optional
from fastapi import APIRouter, Header, Query, HTTPException
from ..clients.redis_client import get_cache_redis
from ..clients.meili_client import MeiliClient
from ..schemas.catalog import ProductOut, PriceInfo

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["Search"])
meili = MeiliClient()


@router.get("/search", response_model=List[ProductOut])
async def search_products(
    q: str = Query(..., min_length=1, max_length=100),
    limit: int = Query(20, le=50),
    x_holi_sede: Optional[str] = Header(None, alias="X-Holi-Sede")
):
    """
    Search endpoint:
    1. Meilisearch returns list of matching SKUs.
    2. Edge Gateway hydrates product attributes and prices from Redis holi-cache.
    """
    if not x_holi_sede:
        raise HTTPException(status_code=400, detail="X-Holi-Sede header is required.")

    sede = x_holi_sede.upper()
    redis = get_cache_redis()
    v_price = await redis.get(f"v:price:{sede}") or "1"

    # Search in Meilisearch
    skus = await meili.search_skus(q, sede, limit=limit)

    # Fallback to prefix matching in Redis if Meilisearch returned empty / unavailable
    if not skus:
        v_cat = await redis.get(f"v:cat:{sede}") or "1"
        all_skus = list(await redis.smembers(f"cat:{v_cat}:{sede}"))
        skus = [s for s in all_skus if q.lower() in s.lower()][:limit]

    products = []
    if not skus:
        return products

    pipe = redis.pipeline()
    for sku in skus:
        pipe.hgetall(f"prod:{sku}")
        pipe.hgetall(f"price:{v_price}:{sede}:{sku}")
        pipe.get(f"stock:{sede}:{sku}")

    results = await pipe.execute()
    for i in range(0, len(results), 3):
        prod_data = results[i]
        price_data = results[i + 1]
        stock_band = results[i + 2] or "disponible"

        if prod_data:
            price_info = None
            if price_data:
                price_info = PriceInfo(
                    list_price=float(price_data.get("list_price", 0.0)),
                    final_price=float(price_data.get("final_price", 0.0)),
                    discount_percent=float(price_data.get("discount_percent", 0.0)),
                    currency=price_data.get("currency", "PEN"),
                    promocion_id=price_data.get("promocion_id") or None,
                )

            products.append(ProductOut(
                sku=prod_data.get("sku", ""),
                product_id=int(prod_data.get("product_id", 0)),
                name=prod_data.get("name", ""),
                category_id=int(prod_data.get("category_id")) if prod_data.get("category_id") else None,
                category_name=prod_data.get("category_name") or None,
                uom=prod_data.get("uom", "UND"),
                barcode=prod_data.get("barcode") or None,
                image_url=prod_data.get("image_url") or None,
                price=price_info,
                availability=stock_band,
            ))

    return products
