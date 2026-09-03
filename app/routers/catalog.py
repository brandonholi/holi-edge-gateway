import json
import logging
from typing import List, Optional
from fastapi import APIRouter, Header, Query, HTTPException, Request
from ..clients.redis_client import get_cache_redis
from ..clients.odoo_client import OdooClient, OdooClientError
from ..schemas.catalog import ProductOut, CategoryOut, AvailabilityOut, PriceInfo, HomeOut

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["Catalog"])
odoo_client = OdooClient()


@router.get("/catalog", response_model=List[ProductOut])
async def get_catalog(
    category_id: Optional[int] = Query(None),
    limit: int = Query(50, le=200),
    x_holi_sede: Optional[str] = Header(None, alias="X-Holi-Sede")
):
    """Retrieve catalog products strictly from Redis holi-cache."""
    if not x_holi_sede:
        raise HTTPException(status_code=400, detail="X-Holi-Sede header is required.")

    redis = get_cache_redis()
    sede = x_holi_sede.upper()

    # Get active version counters
    v_cat = await redis.get(f"v:cat:{sede}") or "1"
    v_price = await redis.get(f"v:price:{sede}") or "1"

    # Fetch SKUs from set
    set_key = f"cat:{v_cat}:{sede}:{category_id}" if category_id else f"cat:{v_cat}:{sede}"
    skus = list(await redis.smembers(set_key))[:limit]

    products = []
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


@router.get("/catalog/{sku}", response_model=ProductOut)
async def get_product_detail(
    sku: str,
    request: Request,
    x_holi_sede: Optional[str] = Header(None, alias="X-Holi-Sede")
):
    """Retrieve detailed product information. Falls back to Odoo on cache-miss."""
    if not x_holi_sede:
        raise HTTPException(status_code=400, detail="X-Holi-Sede header is required.")

    redis = get_cache_redis()
    sede = x_holi_sede.upper()

    v_price = await redis.get(f"v:price:{sede}") or "1"
    prod_data = await redis.hgetall(f"prod:{sku}")
    price_data = await redis.hgetall(f"price:{v_price}:{sede}:{sku}")
    stock_band = await redis.get(f"stock:{sede}:{sku}") or "disponible"

    # Cache miss fallback to Odoo
    if not prod_data or not price_data:
        request_id = request.headers.get("X-Request-Id", "edge-cache-miss")
        _logger.info("Cache miss for SKU %s on Sede %s. Hydrating from Odoo...", sku, sede)
        try:
            odoo_resp = await odoo_client.fetch_product_sync(sku, sede, request_id)
            master = odoo_resp.get("master", {})
            price = odoo_resp.get("price", {})
            stock_band = odoo_resp.get("stock_band", "disponible")

            if master:
                # Populate Redis
                pipe = redis.pipeline()
                pipe.hset(f"prod:{sku}", mapping=master)
                pipe.expire(f"prod:{sku}", 21600)
                pipe.hset(f"price:{v_price}:{sede}:{sku}", mapping=price)
                pipe.expire(f"price:{v_price}:{sede}:{sku}", 21600)
                pipe.setex(f"stock:{sede}:{sku}", 15, stock_band)
                await pipe.execute()

                prod_data = master
                price_data = price
        except OdooClientError as e:
            raise HTTPException(status_code=e.status_code, detail=e.detail)

    if not prod_data:
        raise HTTPException(status_code=404, detail=f"Product with SKU '{sku}' not found.")

    price_info = None
    if price_data:
        price_info = PriceInfo(
            list_price=float(price_data.get("list_price", 0.0)),
            final_price=float(price_data.get("final_price", 0.0)),
            discount_percent=float(price_data.get("discount_percent", 0.0)),
            currency=price_data.get("currency", "PEN"),
            promocion_id=price_data.get("promocion_id") or None,
        )

    return ProductOut(
        sku=prod_data.get("sku", sku),
        product_id=int(prod_data.get("product_id", 0)),
        name=prod_data.get("name", ""),
        category_id=int(prod_data.get("category_id")) if prod_data.get("category_id") else None,
        category_name=prod_data.get("category_name") or None,
        uom=prod_data.get("uom", "UND"),
        barcode=prod_data.get("barcode") or None,
        image_url=prod_data.get("image_url") or None,
        price=price_info,
        availability=stock_band,
    )


@router.get("/categories", response_model=List[CategoryOut])
async def get_categories(
    x_holi_sede: Optional[str] = Header(None, alias="X-Holi-Sede")
):
    """Retrieve category tree from Redis holi-cache."""
    if not x_holi_sede:
        raise HTTPException(status_code=400, detail="X-Holi-Sede header is required.")

    redis = get_cache_redis()
    sede = x_holi_sede.upper()
    v_cat = await redis.get(f"v:cat:{sede}") or "1"

    tree_data = await redis.hgetall(f"tree:{v_cat}:{sede}")
    categories = []
    for cat_id, cat_json in tree_data.items():
        try:
            item = json.loads(cat_json)
            categories.append(CategoryOut(
                id=item.get("id", int(cat_id)),
                name=item.get("name", ""),
                parent_id=item.get("parent_id"),
                total_skus=item.get("total_skus", 0),
            ))
        except Exception:
            continue

    return categories


@router.get("/availability/{sku}", response_model=AvailabilityOut)
async def get_availability(
    sku: str,
    x_holi_sede: Optional[str] = Header(None, alias="X-Holi-Sede")
):
    """Retrieve fast stock availability band (TTL 15s) from Redis."""
    if not x_holi_sede:
        raise HTTPException(status_code=400, detail="X-Holi-Sede header is required.")

    redis = get_cache_redis()
    sede = x_holi_sede.upper()

    band = await redis.get(f"stock:{sede}:{sku}") or "disponible"
    return AvailabilityOut(sku=sku, status=band)


@router.get("/home", response_model=HomeOut)
async def get_home(
    x_holi_sede: Optional[str] = Header(None, alias="X-Holi-Sede")
):
    """Retrieve featured and top selling SKUs for home screen."""
    if not x_holi_sede:
        raise HTTPException(status_code=400, detail="X-Holi-Sede header is required.")

    redis = get_cache_redis()
    sede = x_holi_sede.upper()

    top_skus = await redis.zrevrange(f"top:{sede}", 0, 9)
    return HomeOut(
        sede=sede,
        featured_skus=top_skus[:5],
        top_selling_skus=top_skus,
    )
