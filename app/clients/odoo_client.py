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
                extra: Dict[str, Any] = {}
                try:
                    problem = res.json()
                    title = problem.get("title", "Odoo Error")
                    detail = problem.get("detail", res.text)
                    # The last segment of `type` is the public error code when Odoo
                    # raises one of ours (stock-insuficiente, cupon-no-valido...).
                    extra = {k: v for k, v in problem.items()
                             if k not in ("type", "title", "status", "detail", "instance")}
                    extra["codigo"] = str(problem.get("type") or "").rstrip("/").rsplit("/", 1)[-1]
                except Exception:
                    title = "Odoo Error"
                    detail = res.text

                raise OdooClientError(res.status_code, title, detail, extra)

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

    # --- Identity (RF-001, RF-051). Internal payloads are English by ADR-012. ---

    async def auth_register_precheck(self, document_type_id: int, document_number: str, email: str,
                                     phone: str, request_id: str) -> Dict[str, Any]:
        """Whether the document, e-mail and phone are still free for a new app account."""
        headers = {"X-Request-Id": request_id}
        payload = {"document_type_id": document_type_id, "document_number": document_number,
                   "email": email, "phone": phone}
        return await self._call_odoo("POST", "/api/ecommerce/internal/auth/register-precheck",
                                     data=payload, headers=headers)

    async def auth_partner_create(self, partner_data: Dict[str, Any], idempotency_key: str,
                                  request_id: str) -> Dict[str, Any]:
        """First and only write of the sign-up handshake (step 4)."""
        headers = {
            "Idempotency-Key": idempotency_key,
            "X-Request-Id": request_id,
        }
        return await self._call_odoo("POST", "/api/ecommerce/internal/auth/partner-create",
                                     data=partner_data, headers=headers)

    async def auth_verify_credentials(self, email: str, password: str, request_id: str) -> Dict[str, Any]:
        """
        Odoo owns the hash and returns the verdict (RF-001).

        The password crosses this hop once, signed and over TLS, and the Odoo
        side must never write the body to a log.
        """
        headers = {"X-Request-Id": request_id}
        payload = {"email": email, "password": password}
        return await self._call_odoo("POST", "/api/ecommerce/internal/auth/verify-credentials",
                                     data=payload, headers=headers)

    async def auth_reset_lookup(self, email: str, request_id: str) -> Dict[str, Any]:
        """Resolves the destination for a password reset code."""
        headers = {"X-Request-Id": request_id}
        return await self._call_odoo("POST", "/api/ecommerce/internal/auth/reset-lookup",
                                     data={"email": email}, headers=headers)

    async def auth_set_password(self, partner_id: int, password: str, request_id: str) -> Dict[str, Any]:
        """Closes a password reset once the code was approved."""
        headers = {"X-Request-Id": request_id}
        payload = {"partner_id": partner_id, "password": password}
        return await self._call_odoo("POST", "/api/ecommerce/internal/auth/set-password",
                                     data=payload, headers=headers)

    async def auth_email_status(self, partner_id: int, request_id: str) -> Dict[str, Any]:
        """Current e-mail of the customer, its name and whether that address is verified."""
        headers = {"X-Request-Id": request_id}
        return await self._call_odoo("POST", "/api/ecommerce/internal/auth/email-status",
                                     data={"partner_id": partner_id}, headers=headers)

    async def auth_mark_email_verified(self, partner_id: int, email: str, request_id: str) -> Dict[str, Any]:
        """
        Records that `email` was proven. Odoo refuses with 409 if the customer's
        address changed while the code was in flight.
        """
        headers = {"X-Request-Id": request_id}
        payload = {"partner_id": partner_id, "email": email}
        return await self._call_odoo("POST", "/api/ecommerce/internal/auth/email-verified",
                                     data=payload, headers=headers)

    # --- Master data for the app selectors (slow-moving, cached by the Edge) ---

    async def fetch_departments(self, request_id: str) -> Dict[str, Any]:
        headers = {"X-Request-Id": request_id}
        return await self._call_odoo("GET", "/api/ecommerce/internal/master/departments", headers=headers)

    async def fetch_provinces(self, department_id: int, request_id: str) -> Dict[str, Any]:
        headers = {"X-Request-Id": request_id}
        endpoint = f"/api/ecommerce/internal/master/provinces?department_id={department_id}"
        return await self._call_odoo("GET", endpoint, headers=headers)

    async def fetch_districts(self, province_id: int, request_id: str) -> Dict[str, Any]:
        headers = {"X-Request-Id": request_id}
        endpoint = f"/api/ecommerce/internal/master/districts?province_id={province_id}"
        return await self._call_odoo("GET", endpoint, headers=headers)

    async def fetch_document_types(self, request_id: str) -> Dict[str, Any]:
        headers = {"X-Request-Id": request_id}
        return await self._call_odoo("GET", "/api/ecommerce/internal/master/document-types", headers=headers)

    async def fetch_home_messages(self, sede_code: Optional[str], request_id: str) -> Dict[str, Any]:
        headers = {"X-Request-Id": request_id}
        endpoint = "/api/ecommerce/internal/master/home-messages"
        if sede_code:
            endpoint = f"{endpoint}?sede={sede_code}"
        return await self._call_odoo("GET", endpoint, headers=headers)

    async def fetch_coverage_zones(self, request_id: str) -> Dict[str, Any]:
        """Every delivery polygon at once; the Edge resolves points locally."""
        headers = {"X-Request-Id": request_id}
        return await self._call_odoo("GET", "/api/ecommerce/internal/master/coverage-zones",
                                     headers=headers)
