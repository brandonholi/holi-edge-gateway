import json
import uuid
from typing import Optional
from fastapi import APIRouter, Header, HTTPException
from ..clients.redis_client import get_state_redis, get_cache_redis
from ..schemas.cart import CartOut, CartLine, CartUpdateIn

router = APIRouter(prefix="/v1/cart", tags=["Cart"])


async def _resolve_cart_id(state_redis, partner_id: Optional[str], device_id: Optional[str]) -> str:
    """Find existing cart or generate new UUID."""
    if partner_id:
        existing = await state_redis.get(f"cart:user:{partner_id}")
        if existing:
            return existing
    if device_id:
        existing = await state_redis.get(f"cart:device:{device_id}")
        if existing:
            return existing

    new_id = str(uuid.uuid4())
    if partner_id:
        await state_redis.set(f"cart:user:{partner_id}", new_id, ex=604800)
    elif device_id:
        await state_redis.set(f"cart:device:{device_id}", new_id, ex=604800)
    return new_id


@router.get("", response_model=CartOut)
async def get_cart(
    x_holi_sede: Optional[str] = Header(None, alias="X-Holi-Sede"),
    x_device_id: Optional[str] = Header(None, alias="X-Device-Id"),
    authorization: Optional[str] = Header(None)
):
    """Retrieve active cart from Redis holi-estado."""
    if not x_holi_sede:
        raise HTTPException(status_code=400, detail="X-Holi-Sede header is required.")

    state_redis = get_state_redis()
    sede = x_holi_sede.upper()
    cart_id = await _resolve_cart_id(state_redis, None, x_device_id)

    raw_lines = await state_redis.hgetall(f"cart:{cart_id}:lines")
    lines = []
    total_amount = 0.0

    for sku, line_json in raw_lines.items():
        try:
            data = json.loads(line_json)
            qty = float(data.get("qty", 0.0))
            price_unit = float(data.get("price_unit", 0.0))
            total_amount += qty * price_unit
            lines.append(CartLine(
                sku=sku,
                qty=qty,
                price_unit=price_unit,
                name=data.get("name", sku)
            ))
        except Exception:
            continue

    return CartOut(
        cart_id=cart_id,
        sede=sede,
        lines=lines,
        total_amount=round(total_amount, 2),
        items_count=len(lines),
    )


@router.put("/lines", response_model=CartOut)
async def update_cart_line(
    item: CartUpdateIn,
    x_holi_sede: Optional[str] = Header(None, alias="X-Holi-Sede"),
    x_device_id: Optional[str] = Header(None, alias="X-Device-Id")
):
    """Add, update or remove SKU quantity in cart."""
    if not x_holi_sede:
        raise HTTPException(status_code=400, detail="X-Holi-Sede header is required.")

    state_redis = get_state_redis()
    cache_redis = get_cache_redis()
    sede = x_holi_sede.upper()
    cart_id = await _resolve_cart_id(state_redis, None, x_device_id)

    lines_key = f"cart:{cart_id}:lines"

    if item.qty <= 0:
        await state_redis.hdel(lines_key, item.sku)
    else:
        # Fetch current price and name from cache_redis
        v_price = await cache_redis.get(f"v:price:{sede}") or "1"
        price_data = await cache_redis.hgetall(f"price:{v_price}:{sede}:{item.sku}")
        prod_data = await cache_redis.hgetall(f"prod:{item.sku}")

        final_price = float(price_data.get("final_price", 0.0)) if price_data else 0.0
        name = prod_data.get("name", item.sku) if prod_data else item.sku

        payload = json.dumps({"qty": item.qty, "price_unit": final_price, "name": name})
        await state_redis.hset(lines_key, item.sku, payload)
        await state_redis.expire(lines_key, 604800)

    return await get_cart(x_holi_sede=x_holi_sede, x_device_id=x_device_id)
