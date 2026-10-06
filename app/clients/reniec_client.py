import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Optional

import httpx
from redis.asyncio import Redis

from ..core.breaker import CircuitBreaker
from ..core.config import settings

_logger = logging.getLogger(__name__)

_reniec_breaker = CircuitBreaker("reniec", fail_max=5, reset_timeout=30.0)

CACHE_PREFIX = "reniec:"


@dataclass
class ReniecResult:
    """
    'available' is about the service, not the person: False means we could not
    ask, which RF-001 escenario 3 turns into a pending account rather than a
    refusal. 'found' False means we asked and the document does not exist.
    """
    available: bool
    found: bool = False
    full_name: str = ""
    nombres: str = ""
    apellidos: str = ""
    reason: str = ""

    @property
    def validation_state(self) -> str:
        return "validado" if (self.available and self.found) else "pendiente"


def _cache_key(dni: str) -> str:
    """
    Keyed by digest so a dump of holi-estado is not a list of DNIs. The names
    behind it are still personal data, which is why the entry is short-lived.
    """
    return CACHE_PREFIX + hashlib.sha256(dni.encode("utf-8")).hexdigest()[:20]


class ReniecClient:
    """
    DNI lookup against json.pe (POST /api/dni, bearer token).

    This runs in the Edge and not in Odoo on purpose: a four-second wait on a
    third party would otherwise hold an Odoo.sh worker and one of the twenty
    slots of the semaphore for its whole duration (ADR-014).

    Each call spends a credit, so the answer is cached: a customer who fixes a
    typo and resubmits does not pay twice.
    """

    def __init__(self):
        self.url = settings.RENIEC_API_URL
        self.token = settings.RENIEC_API_TOKEN
        self.timeout = settings.RENIEC_TIMEOUT_SECONDS

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    async def lookup(self, dni: str, state_redis: Optional[Redis] = None) -> ReniecResult:
        if not self.configured:
            return ReniecResult(available=False, reason="proveedor_no_configurado")

        cached = await self._read_cache(state_redis, dni)
        if cached is not None:
            return cached

        try:
            _reniec_breaker.before_call()
        except Exception:
            # An open circuit is the provider being down, which is a pending
            # account, never a blocked sign-up.
            return ReniecResult(available=False, reason="proveedor_no_disponible")

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    self.url,
                    json={"dni": dni},
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Content-Type": "application/json",
                    },
                )
        except (httpx.TimeoutException, httpx.ConnectError) as net_err:
            _reniec_breaker.record_failure()
            _logger.warning("RENIEC lookup could not reach the provider: %s", net_err)
            return ReniecResult(available=False, reason="proveedor_no_disponible")

        result = self._interpret(response)

        # Only a real answer is worth caching, and a 404 is a real answer: it
        # stops a wrong DNI from being retried at a credit a time.
        if result.available and state_redis is not None:
            await self._write_cache(state_redis, dni, result)

        return result

    def _interpret(self, response: httpx.Response) -> ReniecResult:
        if response.status_code == 404:
            _reniec_breaker.record_success()
            return ReniecResult(available=True, found=False, reason="documento_no_encontrado")

        if response.status_code in (401, 403):
            # Bad token or credits gone. Not the customer's problem, but it is
            # ours: this one has to be loud.
            _reniec_breaker.record_failure()
            _logger.error("RENIEC provider rejected our credentials (HTTP %s). Check token and credits.",
                          response.status_code)
            return ReniecResult(available=False, reason="proveedor_rechazo_credenciales")

        if response.status_code != 200:
            _reniec_breaker.record_failure()
            _logger.warning("RENIEC provider answered HTTP %s", response.status_code)
            return ReniecResult(available=False, reason="proveedor_error")

        try:
            body = response.json()
        except ValueError:
            _reniec_breaker.record_failure()
            _logger.warning("RENIEC provider answered something that is not JSON.")
            return ReniecResult(available=False, reason="proveedor_error")

        _reniec_breaker.record_success()

        if not body.get("success"):
            return ReniecResult(available=True, found=False, reason="documento_no_encontrado")

        data = body.get("data") or {}
        nombres = (data.get("nombres") or "").strip()
        apellidos = " ".join(
            p for p in [
                (data.get("apellido_paterno") or "").strip(),
                (data.get("apellido_materno") or "").strip(),
            ] if p
        ).strip()

        # The provider's nombre_completo reads "APELLIDOS, NOMBRES"; the app
        # greets people, so it gets the natural order.
        full_name = " ".join(p for p in [nombres, apellidos] if p).strip()
        if not full_name:
            return ReniecResult(available=True, found=False, reason="documento_no_encontrado")

        return ReniecResult(
            available=True, found=True, full_name=full_name, nombres=nombres, apellidos=apellidos
        )

    async def _read_cache(self, state_redis: Optional[Redis], dni: str) -> Optional[ReniecResult]:
        if state_redis is None:
            return None
        try:
            raw = await state_redis.get(_cache_key(dni))
        except Exception as e:
            _logger.warning("Could not read the RENIEC cache: %s", e)
            return None
        if not raw:
            return None
        try:
            stored = json.loads(raw)
        except ValueError:
            return None
        return ReniecResult(
            available=True,
            found=bool(stored.get("found")),
            full_name=stored.get("full_name", ""),
            nombres=stored.get("nombres", ""),
            apellidos=stored.get("apellidos", ""),
            reason="cache",
        )

    async def _write_cache(self, state_redis: Redis, dni: str, result: ReniecResult) -> None:
        try:
            await state_redis.set(
                _cache_key(dni),
                json.dumps({
                    "found": result.found,
                    "full_name": result.full_name,
                    "nombres": result.nombres,
                    "apellidos": result.apellidos,
                }),
                ex=settings.RENIEC_CACHE_TTL_SECONDS,
            )
        except Exception as e:
            _logger.warning("Could not write the RENIEC cache: %s", e)


_client: Optional[ReniecClient] = None


def get_reniec_client() -> ReniecClient:
    global _client
    if _client is None:
        _client = ReniecClient()
    return _client


def set_reniec_client(client: Optional[ReniecClient]) -> None:
    """Test seam, mirroring the OTP provider."""
    global _client
    _client = client
