import json
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, Optional
import httpx
from ..core.config import settings
from ..core.breaker import CircuitBreaker

_logger = logging.getLogger(__name__)

_twilio_breaker = CircuitBreaker("twilio_verify", fail_max=5, reset_timeout=30.0)

# Public contract channel -> Twilio Verify channel (ADR-012: Spanish outside, English inside)
CHANNEL_MAP = {
    "sms": "sms",
    "whatsapp": "whatsapp",
    "correo": "email",
}


class OtpProviderError(Exception):
    """Raised when the provider refuses or cannot be reached."""

    def __init__(self, status_code: int, title: str, detail: str, reason: str = ""):
        super().__init__(detail)
        self.status_code = status_code
        self.title = title
        self.detail = detail
        self.reason = reason


@dataclass
class SendResult:
    sid: str
    channel: str
    status: str


@dataclass
class CheckResult:
    approved: bool
    status: str


class OtpProvider(ABC):
    """
    The seam between the registration flow and whoever sends the code.

    Everything above this interface is ours; swapping Twilio for another
    verification service should not reach the routers.
    """

    @abstractmethod
    async def send(self, destination: str, channel: str, locale: str = "es",
                   email_template: str = "", substitutions: Optional[Dict[str, str]] = None) -> SendResult:
        """`email_template` and `substitutions` only apply to the 'correo' channel."""
        ...

    @abstractmethod
    async def check(self, destination: str, code: str) -> CheckResult:
        ...


class TwilioVerifyProvider(OtpProvider):
    """
    Twilio Verify v2 over plain HTTP — no SDK, so nothing extra to import on a
    Cloud Run cold start. The code itself never passes through here on the way
    out and is never stored: we send a destination and later ask for a verdict.
    """

    def __init__(self):
        self.base_url = settings.TWILIO_BASE_URL.rstrip("/")
        self.service_sid = settings.TWILIO_VERIFY_SERVICE_SID
        self.auth = (settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        self.timeout = settings.TWILIO_TIMEOUT_SECONDS

    @property
    def configured(self) -> bool:
        return bool(settings.TWILIO_ACCOUNT_SID and settings.TWILIO_AUTH_TOKEN and self.service_sid)

    async def _post(self, path: str, form: dict) -> dict:
        if not self.configured:
            raise OtpProviderError(503, "OTP Provider Unconfigured",
                                   "Twilio Verify credentials are not set on this environment.",
                                   reason="proveedor_no_configurado")

        _twilio_breaker.before_call()
        url = f"{self.base_url}/Services/{self.service_sid}{path}"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                res = await client.post(url, data=form, auth=self.auth)

            if res.status_code in (200, 201):
                _twilio_breaker.record_success()
                return res.json()

            # A 4xx is a verdict about this request, not a sick provider: counting
            # it as a failure would trip the breaker on ordinary user mistakes.
            if 400 <= res.status_code < 500:
                _twilio_breaker.record_success()
            else:
                _twilio_breaker.record_failure()

            raise OtpProviderError(*_translate_twilio_error(res))

        except (httpx.TimeoutException, httpx.ConnectError) as net_err:
            _twilio_breaker.record_failure()
            _logger.error("Network failure calling Twilio Verify (%s): %s", path, net_err)
            raise OtpProviderError(503, "OTP Provider Unavailable",
                                   "The verification service is temporarily unreachable.",
                                   reason="proveedor_no_disponible")

    async def send(self, destination: str, channel: str, locale: str = "es",
                   email_template: str = "", substitutions: Optional[Dict[str, str]] = None) -> SendResult:
        twilio_channel = CHANNEL_MAP.get(channel)
        if not twilio_channel:
            raise OtpProviderError(400, "Unsupported Channel", f"Channel '{channel}' is not supported.",
                                   reason="canal_no_soportado")

        form = {"To": destination, "Channel": twilio_channel, "Locale": locale}
        if twilio_channel == "email":
            # The e-mail goes out through the SendGrid integration of the Verify
            # service; which template and which names it carries is chosen here.
            configuration: Dict[str, object] = {}
            if email_template:
                configuration["template_id"] = email_template
            if substitutions:
                configuration["substitutions"] = substitutions
            if configuration:
                form["ChannelConfiguration"] = json.dumps(configuration)

        data = await self._post("/Verifications", form)
        return SendResult(sid=data.get("sid", ""), channel=channel, status=data.get("status", "pending"))

    async def check(self, destination: str, code: str) -> CheckResult:
        try:
            data = await self._post("/VerificationCheck", {"To": destination, "Code": code})
        except OtpProviderError as e:
            # Verify drops the verification once it expires or is exhausted; from
            # the caller's side that is a rejected code, not a broken service.
            if e.reason == "verificacion_no_encontrada":
                return CheckResult(approved=False, status="expired")
            raise

        status = data.get("status", "pending")
        return CheckResult(approved=bool(data.get("valid")) and status == "approved", status=status)


def _translate_twilio_error(res: httpx.Response) -> tuple:
    """Maps Twilio error codes onto (status, title, detail, reason)."""
    try:
        body = res.json()
        code = int(body.get("code") or 0)
        message = body.get("message", res.text)
    except Exception:
        code, message = 0, res.text

    known = {
        20404: (410, "Verification Not Found",
                "The code expired or was already used. Request a new one.", "verificacion_no_encontrada"),
        60200: (400, "Invalid Destination",
                "The phone number or e-mail is not valid.", "destino_invalido"),
        60202: (429, "Too Many Attempts",
                "Too many failed attempts on this code. Request a new one.", "intentos_agotados"),
        60203: (429, "Too Many Sends",
                "Too many codes sent to this destination. Try again later.", "limite_proveedor"),
        60205: (400, "Channel Unsupported For Destination",
                "This destination cannot receive the code on the chosen channel.", "canal_no_soportado"),
        60212: (429, "Too Many Concurrent Requests",
                "Too many concurrent verifications. Try again shortly.", "limite_proveedor"),
    }
    if code in known:
        return known[code]

    _logger.warning("Unmapped Twilio Verify error %s (HTTP %s): %s", code, res.status_code, message)
    status = res.status_code if res.status_code >= 400 else 502
    return (status, "OTP Provider Error", message, "proveedor_error")


_provider: Optional[OtpProvider] = None


def get_otp_provider() -> OtpProvider:
    global _provider
    if _provider is None:
        _provider = TwilioVerifyProvider()
    return _provider


def set_otp_provider(provider: Optional[OtpProvider]) -> None:
    """Test seam: lets the suite run the whole flow without touching Twilio."""
    global _provider
    _provider = provider
