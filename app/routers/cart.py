import json
import uuid
from typing import Optional

from fastapi import APIRouter, Header

from ..clients.redis_client import get_cache_redis, get_state_redis
from ..core.deps import SedeRequerida
from ..schemas.cart import CarritoOut, LineaCarrito, LineaCarritoIn

router = APIRouter(prefix="/v1/cart", tags=["Cart"])

CARRITO_TTL_SECONDS = 604800


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
        await state_redis.set(f"cart:user:{partner_id}", new_id, ex=CARRITO_TTL_SECONDS)
    elif device_id:
        await state_redis.set(f"cart:device:{device_id}", new_id, ex=CARRITO_TTL_SECONDS)
    return new_id


async def _leer_carrito(state_redis, cart_id: str, sede: str) -> CarritoOut:
    # Redis keeps the English line fields (qty, price_unit, name); ADR-012.
    raw_lines = await state_redis.hgetall(f"cart:{cart_id}:lines")
    lineas = []
    total = 0.0

    for sku, line_json in raw_lines.items():
        try:
            data = json.loads(line_json)
            cantidad = float(data.get("qty", 0.0))
            precio_unitario = float(data.get("price_unit", 0.0))
            total += cantidad * precio_unitario
            lineas.append(LineaCarrito(
                sku=sku,
                nombre=data.get("name", sku),
                cantidad=cantidad,
                precio_unitario=precio_unitario,
            ))
        except Exception:
            continue

    return CarritoOut(
        carrito_id=cart_id,
        sede=sede,
        lineas=lineas,
        total=round(total, 2),
        cantidad_lineas=len(lineas),
    )


@router.get("", response_model=CarritoOut)
async def ver_carrito(
    sede: SedeRequerida,
    x_device_id: Optional[str] = Header(None, alias="X-Device-Id"),
):
    """Active cart from Redis holi-estado."""
    state_redis = get_state_redis()
    cart_id = await _resolve_cart_id(state_redis, None, x_device_id)
    return await _leer_carrito(state_redis, cart_id, sede)


@router.put("/lines", response_model=CarritoOut)
async def actualizar_linea(
    linea: LineaCarritoIn,
    sede: SedeRequerida,
    x_device_id: Optional[str] = Header(None, alias="X-Device-Id"),
):
    """Sets the quantity of a SKU in the cart; 0 removes the line."""
    state_redis = get_state_redis()
    cache_redis = get_cache_redis()
    cart_id = await _resolve_cart_id(state_redis, None, x_device_id)

    lines_key = f"cart:{cart_id}:lines"

    if linea.cantidad <= 0:
        await state_redis.hdel(lines_key, linea.sku)
    else:
        v_price = await cache_redis.get(f"v:price:{sede}") or "1"
        price_data = await cache_redis.hgetall(f"price:{v_price}:{sede}:{linea.sku}")
        prod_data = await cache_redis.hgetall(f"prod:{linea.sku}")

        final_price = float(price_data.get("final_price", 0.0)) if price_data else 0.0
        name = prod_data.get("name", linea.sku) if prod_data else linea.sku

        payload = json.dumps({"qty": linea.cantidad, "price_unit": final_price, "name": name})
        await state_redis.hset(lines_key, linea.sku, payload)
        await state_redis.expire(lines_key, CARRITO_TTL_SECONDS)

    return await _leer_carrito(state_redis, cart_id, sede)
