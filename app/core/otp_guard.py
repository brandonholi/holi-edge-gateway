import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Optional
from redis.asyncio import Redis
from .config import settings
from .errors import ApiError

_logger = logging.getLogger(__name__)


@dataclass
class GuardVerdict:
    allowed: bool
    reason: str = ""
    retry_after: int = 0


def destination_hash(destination: str) -> str:
    """
    Short digest of a phone number or e-mail used as a counter key.

    Counters live in Redis for a day; the raw destination does not need to be
    there to count it, and keeping it out narrows what a dump of holi-estado
    would expose (Ley 29733).
    """
    return hashlib.sha256(destination.strip().lower().encode("utf-8")).hexdigest()[:20]


async def _incr_window(redis_state: Redis, prefix: str, identifier: str, window_seconds: int) -> int:
    """
    Fixed-window counter, same idiom as check_rate_limit.

    The window number is part of the key, so refreshing the TTL on every
    increment is harmless — the key rolls over on its own. That avoids needing
    EXPIRE ... NX, which would tie us to Redis 7.
    """
    window = int(time.time()) // window_seconds
    key = f"{prefix}:{identifier}:{window}"
    pipe = redis_state.pipeline()
    pipe.incr(key)
    pipe.expire(key, window_seconds * 2)
    results = await pipe.execute()
    return int(results[0])


async def check_send_allowed(redis_state: Redis, destination: str, client_ip: str,
                             registration_id: str) -> GuardVerdict:
    """
    Decides whether one more code may be sent.

    Unlike check_rate_limit this fails CLOSED: if holi-estado is unreachable we
    cannot count, and an uncounted send endpoint is how an SMS pumping attack
    turns into a five-figure invoice overnight. A blocked sign-up is an
    inconvenience; the alternative is not.
    """
    dest_key = destination_hash(destination)

    try:
        cooldown_key = f"otp:cd:{registration_id}"
        remaining = await redis_state.ttl(cooldown_key)
        if remaining and remaining > 0:
            return GuardVerdict(False, "cooldown", retry_after=int(remaining))

        hourly = await _incr_window(redis_state, "otp:snd:h", dest_key, 3600)
        if hourly > settings.OTP_MAX_SENDS_PER_PHONE_HOUR:
            return GuardVerdict(False, "limite_destino_hora", retry_after=3600)

        daily = await _incr_window(redis_state, "otp:snd:d", dest_key, 86400)
        if daily > settings.OTP_MAX_SENDS_PER_PHONE_DAY:
            return GuardVerdict(False, "limite_destino_dia", retry_after=86400)

        if client_ip:
            per_ip = await _incr_window(redis_state, "otp:ip:h", destination_hash(client_ip), 3600)
            if per_ip > settings.OTP_MAX_SENDS_PER_IP_HOUR:
                return GuardVerdict(False, "limite_ip_hora", retry_after=3600)

        return GuardVerdict(True)

    except Exception as e:
        _logger.error("OTP guard could not reach holi-estado, refusing send (fail closed): %s", e)
        return GuardVerdict(False, "estado_no_disponible", retry_after=30)


_MENSAJES_RECHAZO = {
    "cooldown": "Espera {segundos} segundos antes de pedir otro código.",
    "limite_destino_hora": "Alcanzaste el límite de códigos por hora. Inténtalo más tarde.",
    "limite_destino_dia": "Alcanzaste el límite de códigos por día. Inténtalo mañana.",
    "limite_ip_hora": "Demasiadas solicitudes desde esta conexión. Inténtalo más tarde.",
    "estado_no_disponible": "No podemos enviar el código en este momento. Inténtalo en unos minutos.",
}


def send_refusal(verdict: GuardVerdict) -> ApiError:
    """The public error for a send the guard turned down, with Retry-After."""
    codigo = {
        "cooldown": "espera-para-reenviar",
        "estado_no_disponible": "servicio-no-disponible",
    }.get(verdict.reason, "limite-excedido")
    mensaje = _MENSAJES_RECHAZO.get(verdict.reason, "").format(segundos=verdict.retry_after) or None
    headers = {"Retry-After": str(verdict.retry_after)} if verdict.retry_after else None
    return ApiError(codigo, mensaje, headers=headers)


async def start_cooldown(redis_state: Redis, registration_id: str) -> int:
    """Blocks resends for a moment so the 'reenviar' button cannot be leaned on."""
    cooldown = settings.OTP_RESEND_COOLDOWN_SECONDS
    try:
        await redis_state.set(f"otp:cd:{registration_id}", "1", ex=cooldown)
    except Exception as e:
        _logger.warning("Could not set OTP resend cooldown: %s", e)
    return cooldown


async def register_check_attempt(redis_state: Redis, registration_id: str) -> GuardVerdict:
    """
    Counts one code verification attempt. Past the ceiling the handshake is dead
    and the caller must drop the draft: six digits are guessable if you are
    allowed to keep guessing.
    """
    try:
        key = f"otp:try:{registration_id}"
        pipe = redis_state.pipeline()
        pipe.incr(key)
        pipe.expire(key, settings.OTP_SESSION_TTL_SECONDS)
        results = await pipe.execute()
        attempts = int(results[0])

        if attempts > settings.OTP_MAX_CHECK_ATTEMPTS:
            return GuardVerdict(False, "intentos_agotados")
        return GuardVerdict(True)
    except Exception as e:
        _logger.error("OTP guard could not count check attempt, refusing (fail closed): %s", e)
        return GuardVerdict(False, "estado_no_disponible")


async def burn_verification_token(redis_state: Redis, jti: str, ttl_seconds: Optional[int] = None) -> bool:
    """
    Marks a verification token as spent. Returns False if it had already been
    spent, which means the same approved code is being replayed.
    """
    ttl = ttl_seconds or (settings.VERIFICATION_TOKEN_EXPIRE_MINUTES * 60)
    try:
        was_set = await redis_state.set(f"vt:used:{jti}", "1", nx=True, ex=ttl)
        return bool(was_set)
    except Exception as e:
        _logger.error("Could not burn verification token, refusing (fail closed): %s", e)
        return False
