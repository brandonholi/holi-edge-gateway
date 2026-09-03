import json
import logging
import asyncio
from typing import Optional
from fastapi import APIRouter, Header, HTTPException, Request
from ..clients.redis_client import get_state_redis
from ..clients.odoo_client import OdooClient, OdooClientError
from ..schemas.checkout import CheckoutIn, CheckoutOut, DeliverySlotsOut, DeliverySlot

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/checkout", tags=["Checkout"])
odoo_client = OdooClient()


@router.get("/slots", response_model=DeliverySlotsOut)
async def get_delivery_slots(
    request: Request,
    x_holi_sede: Optional[str] = Header(None, alias="X-Holi-Sede")
):
    """Retrieve available delivery windows for store."""
    if not x_holi_sede:
        raise HTTPException(status_code=400, detail="X-Holi-Sede header is required.")

    sede = x_holi_sede.upper()
    request_id = request.headers.get("X-Request-Id", "edge-slots")
    try:
        data = await odoo_client.fetch_delivery_slots(sede, request_id)
        raw_slots = data.get("slots", [])
        slots = [DeliverySlot(slot_id=s["slot_id"], name=s["name"], available=s.get("available", True)) for s in raw_slots]
        return DeliverySlotsOut(sede=sede, slots=slots)
    except OdooClientError as e:
        raise HTTPException(status_code=e.status_code, detail=e.detail)


@router.post("", response_model=CheckoutOut)
async def confirm_checkout(
    payload: CheckoutIn,
    request: Request,
    idempotency_key: Optional[str] = Header(None, alias="Idempotency-Key"),
    x_holi_sede: Optional[str] = Header(None, alias="X-Holi-Sede"),
    authorization: Optional[str] = Header(None)
):
    """
    Checkout Orchestration adhering to Rules 1, 2, 3 and 5:
    1. Idempotency reservation in holi-estado BEFORE touching Odoo.
    2. Calls Odoo under SEMAFORO_ODOO (max 20).
    3. Odoo decides stock via PostgreSQL SELECT ... FOR UPDATE.
    4. Caches final result in holi-estado.
    """
    if not x_holi_sede:
        raise HTTPException(status_code=400, detail="X-Holi-Sede header is required.")
    if not idempotency_key:
        raise HTTPException(status_code=400, detail="Idempotency-Key header is mandatory for checkout.")

    sede = x_holi_sede.upper()
    request_id = request.headers.get("X-Request-Id", "edge-checkout")
    state_redis = get_state_redis()

    # RULE 3: Atomic reservation before calling Odoo
    idem_key = f"idem:{idempotency_key}"
    reserved = await state_redis.set(idem_key, "en_curso", nx=True, ex=86400)

    if not reserved:
        # Key already exists: check if processing or already completed
        stored_val = await state_redis.get(idem_key)
        if stored_val == "en_curso":
            # Concurrent retry in flight; wait briefly
            for _ in range(5):
                await asyncio.sleep(0.5)
                stored_val = await state_redis.get(idem_key)
                if stored_val and stored_val != "en_curso":
                    break

        if stored_val and stored_val != "en_curso":
            try:
                cached_resp = json.loads(stored_val)
                cached_resp["idempotent"] = True
                _logger.info("Idempotent hit for key %s -> Returning cached order %s",
                             idempotency_key, cached_resp.get("order_name"))
                return CheckoutOut(**cached_resp)
            except Exception:
                pass

        raise HTTPException(
            status_code=409,
            detail="A checkout request with this Idempotency-Key is currently in flight. Please wait."
        )

    # Build payload for Odoo
    odoo_order_vals = {
        "sede_code": sede,
        "lines": [{"sku": l.sku, "qty": l.qty, "price_unit": l.price_unit} for l in payload.lines],
        "coupon_code": payload.coupon_code,
        "delivery_slot_id": payload.delivery_slot_id,
        "delivery_address_id": payload.delivery_address_id,
        "notes": payload.notes,
    }

    try:
        # Call Odoo under SEMAFORO_ODOO
        result = await odoo_client.execute_checkout(sede, odoo_order_vals, idempotency_key, request_id)

        # Store successful result in holi-estado
        resp_payload = {
            "order_id": result["order_id"],
            "order_name": result["order_name"],
            "amount_total": float(result.get("amount_total", 0.0)),
            "state": result.get("state", "sale"),
            "idempotent": False,
        }
        await state_redis.set(idem_key, json.dumps(resp_payload), ex=86400)
        return CheckoutOut(**resp_payload)

    except OdooClientError as e:
        # Release reservation if it was a client error (e.g. stock exhausted)
        if e.status_code == 409:
            await state_redis.delete(idem_key)
        else:
            # Store failure message
            await state_redis.set(idem_key, json.dumps({"error": e.detail}), ex=300)

        raise HTTPException(status_code=e.status_code, detail=e.detail)

    except Exception as general_err:
        await state_redis.delete(idem_key)
        _logger.exception("Unexpected error during checkout: %s", general_err)
        raise HTTPException(status_code=500, detail="Internal server error executing checkout.")
