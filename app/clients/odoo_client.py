import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Dict, Any, Optional
import httpx
from ..core.config import settings
from ..core.breaker import CircuitBreaker, CircuitBreakerOpenError

_logger = logging.getLogger(__name__)

# RULE 5: Hard semaphore ceiling (max 20 calls in flight)
SEMAFORO_ODOO = asyncio.Semaphore(settings.ODOO_MAX_CONCURRENT)
_odoo_breaker = CircuitBreaker("odoo", fail_max=5, reset_timeout=30.0)


class OdooClientError(Exception):
    def __init__(self, status_code: int, title: str, detail: str, extra: Optional[Dict[str, Any]] = None):
        super().__init__(detail)
        self.status_code = status_code
        self.title = title
        self.detail = detail
        self.extra = extra or {}


class OdooClient:
    """Async client communicating with Odoo 18 holi_ecommerce_sync."""

    def __init__(self):
        self.base_url = settings.ODOO_BASE_URL.rstrip('/')
        self.secret = settings.ODOO_HMAC_SECRET
        self.timeout = settings.ODOO_TIMEOUT_SECONDS

    def _generate_hmac_headers(self, body_bytes: bytes, headers: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """Generates HMAC-SHA256 headers accepted by Odoo's @hmac_required decorator."""
        req_headers = headers.copy() if headers else {}
        ts = str(int(time.time()))
        req_headers["X-Holi-Timestamp"] = ts

        if self.secret:
            message = ts.encode('utf-8') + body_bytes
            sig = hmac.new(self.secret.encode('utf-8'), message, hashlib.sha256).hexdigest()
            req_headers["X-Holi-Signature"] = sig

        return req_headers

    async def _call_odoo(self, method: str, endpoint: str, data: Optional[Dict[str, Any]] = None,
                         headers: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """Executes HTTP call to Odoo protected by semaphore, circuit breaker and HMAC."""
        _odoo_breaker.before_call()

        body_bytes = json.dumps(data).encode('utf-8') if data else b""
        req_headers = self._generate_hmac_headers(body_bytes, headers)
        req_headers["Content-Type"] = "application/json"

        async with SEMAFORO_ODOO:
            try:
                async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
                    if method == "POST":
                        res = await client.post(endpoint, content=body_bytes, headers=req_headers)
                    else:
                        res = await client.get(endpoint, headers=req_headers)

                if res.status_code in (200, 201):
                    _odoo_breaker.record_success()
                    return res.json()

                # Parse RFC 7807 error from Odoo if available
                _odoo_breaker.record_failure()
                try:
                    problem = res.json()
                    title = problem.get("title", "Odoo Error")
                    detail = problem.get("detail", res.text)
                except Exception:
                    title = "Odoo Error"
                    detail = res.text

                raise OdooClientError(res.status_code, title, detail)

            except (httpx.TimeoutException, httpx.ConnectError) as net_err:
                _odoo_breaker.record_failure()
                _logger.error("Network failure calling Odoo (%s): %s", endpoint, net_err)
                raise OdooClientError(503, "Odoo Unavailable", "Odoo backend is temporarily unreachable.")

    async def execute_checkout(self, sede_code: str, order_data: Dict[str, Any], idempotency_key: str,
                               request_id: str) -> Dict[str, Any]:
        """Call Odoo checkout endpoint (Rule 1 row lock & Rule 2 async invoice)."""
        headers = {
            "Idempotency-Key": idempotency_key,
            "X-Request-Id": request_id,
            "X-Holi-Sede": sede_code,
        }
        return await self._call_odoo("POST", "/api/ecommerce/internal/checkout", data=order_data, headers=headers)

    async def fetch_product_sync(self, sku: str, sede_code: str, request_id: str) -> Dict[str, Any]:
        """Fetch product payload from Odoo upon cache-miss."""
        headers = {
            "X-Request-Id": request_id,
            "X-Holi-Sede": sede_code,
        }
        endpoint = f"/api/ecommerce/internal/product-sync?sku={sku}&sede={sede_code}"
        return await self._call_odoo("GET", endpoint, headers=headers)

    async def fetch_delivery_slots(self, sede_code: str, request_id: str) -> Dict[str, Any]:
        """Fetch available delivery slots for a store."""
        headers = {
            "X-Request-Id": request_id,
            "X-Holi-Sede": sede_code,
        }
        endpoint = f"/api/ecommerce/internal/delivery-slots?sede={sede_code}"
        return await self._call_odoo("GET", endpoint, headers=headers)
