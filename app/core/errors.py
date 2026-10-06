"""
The one error format of the public API (contrato-api-v1.md, section 4).

Every error the app can receive is listed in CATALOGO. A router raises
ApiError with a code from that list and never builds a response body itself,
so the status, the title and the `type` URL of a given code cannot drift from
one endpoint to another. A code that is not in the list fails at raise time.
"""
import logging
from typing import Any, Dict, Optional, Tuple

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..clients.odoo_client import OdooClientError
from ..clients.otp_provider import OtpProviderError
from .breaker import CircuitBreakerOpenError

_logger = logging.getLogger(__name__)

TIPO_BASE = "https://api.holi.com.pe/errors/"
MEDIA_TYPE = "application/problem+json"

# codigo -> (status, title, default detail). Mirrors the table in section 4 of
# the contract; a new code goes into both places in the same change.
CATALOGO: Dict[str, Tuple[int, str, str]] = {
    # 400 — the request is well formed but the business rule says no
    "codigo-incorrecto": (400, "Código incorrecto", "El código ingresado no es correcto."),
    "terminos-no-aceptados": (400, "Términos no aceptados",
                              "Debes aceptar los términos y condiciones para registrarte."),
    "nombres-requeridos": (400, "Nombres requeridos",
                           "No pudimos validar tu documento y no recibimos tus nombres. "
                           "Complétalos e inténtalo de nuevo."),
    "canal-no-disponible": (400, "Canal no disponible", "El canal de verificación no está disponible."),
    "destino-invalido": (400, "Destino inválido", "El número o correo no es válido para recibir el código."),
    "contrasena-debil": (400, "Contraseña débil", "La contraseña no cumple la política de seguridad."),
    "solicitud-rechazada": (400, "Solicitud rechazada", "La solicitud no pudo procesarse."),
    # 401
    "sesion-requerida": (401, "Sesión requerida", "Inicia sesión para continuar."),
    "credenciales-invalidas": (401, "Credenciales inválidas", "Correo o contraseña incorrectos."),
    "token-invalido": (401, "Token inválido", "El token de verificación no es válido o expiró."),
    "sesion-expirada": (401, "Sesión expirada", "El token de renovación no es válido o expiró."),
    "sesion-revocada": (401, "Sesión revocada",
                        "Sesión invalidada por motivos de seguridad. Vuelve a iniciar sesión."),
    # 403
    "datos-fiscales-incompletos": (403, "Datos fiscales incompletos", "Completa tu documento de identidad."),
    "telefono-no-verificado": (403, "Teléfono no verificado", "Debes verificar tu número de teléfono."),
    "correo-no-verificado": (403, "Correo no verificado", "Verifica tu correo para continuar."),
    # 404 / 405
    "no-encontrado": (404, "No encontrado", "El recurso solicitado no existe."),
    "metodo-no-permitido": (405, "Método no permitido", "Esta ruta no admite el método usado."),
    # 409
    "cuenta-ya-existe": (409, "Cuenta ya existe",
                         "Ya existe una cuenta con estos datos. Recupera tu acceso en lugar de crear una nueva."),
    "registro-ya-completado": (409, "Registro ya completado", "Esta solicitud ya fue completada."),
    "correo-ya-verificado": (409, "Correo ya verificado", "Tu correo ya está verificado."),
    "stock-insuficiente": (409, "Stock insuficiente", "Algunas líneas no tienen stock disponible."),
    "cupon-no-valido": (409, "Cupón no válido", "El cupón no es válido para esta compra."),
    "franja-agotada": (409, "Franja agotada", "La franja de entrega elegida ya no tiene cupo."),
    "solicitud-en-curso": (409, "Solicitud en curso",
                           "Una solicitud con esta Idempotency-Key se está procesando. Reintenta en unos segundos."),
    "conflicto": (409, "Conflicto", "La solicitud entra en conflicto con el estado actual."),
    # 410
    "registro-expirado": (410, "Registro expirado", "El registro expiró o no existe. Vuelve a iniciar el proceso."),
    "codigo-expirado": (410, "Código expirado", "El código expiró. Solicita uno nuevo."),
    # 422 — the request itself is malformed: a defect of the app, not of the user
    "parametros-invalidos": (422, "Parámetros inválidos", "La solicitud tiene parámetros inválidos."),
    "idempotency-key-requerida": (422, "Idempotency-Key requerida",
                                  "Esta operación exige la cabecera Idempotency-Key."),
    # 423
    "cuenta-bloqueada": (423, "Cuenta bloqueada",
                         "Demasiadas contraseñas fallidas. La cuenta se desbloquea sola en 15 minutos."),
    # 429
    "espera-para-reenviar": (429, "Espera para reenviar", "Espera antes de pedir otro código."),
    "intentos-agotados": (429, "Intentos agotados",
                          "Demasiados intentos fallidos. Vuelve a iniciar el proceso de verificación."),
    "limite-excedido": (429, "Límite excedido", "Demasiadas solicitudes. Inténtalo más tarde."),
    # 5xx
    "error-interno": (500, "Error interno", "Ocurrió un error inesperado."),
    "servicio-no-disponible": (503, "Servicio no disponible", "El servicio no está disponible en este momento."),
}

# Fallback for errors that arrive with only a status: framework errors, and
# upstream errors whose type is not one of ours.
_CODIGO_POR_ESTADO = {
    400: "solicitud-rechazada",
    401: "sesion-expirada",
    403: "telefono-no-verificado",
    404: "no-encontrado",
    405: "metodo-no-permitido",
    409: "conflicto",
    410: "registro-expirado",
    422: "parametros-invalidos",
    423: "cuenta-bloqueada",
    429: "limite-excedido",
}

# Twilio reasons (otp_provider._translate_twilio_error) -> public code.
_CODIGO_POR_MOTIVO_OTP = {
    "verificacion_no_encontrada": "registro-expirado",
    "destino_invalido": "destino-invalido",
    "intentos_agotados": "intentos-agotados",
    "limite_proveedor": "limite-excedido",
    "canal_no_soportado": "canal-no-disponible",
}


def codigo_para_estado(status: int) -> str:
    if status >= 500:
        return "error-interno" if status == 500 else "servicio-no-disponible"
    return _CODIGO_POR_ESTADO.get(status, "solicitud-rechazada")


class ApiError(Exception):
    """
    The only exception a router raises to answer with an error.

        raise ApiError("registro-expirado")
        raise ApiError("stock-insuficiente", "2 líneas sin stock", lineas=[...])

    `detalle` defaults to the catalogue text. Keyword arguments become extra
    members of the problem document, next to the standard ones.
    """

    def __init__(self, codigo: str, detalle: Optional[str] = None,
                 headers: Optional[Dict[str, str]] = None, **extra: Any):
        if codigo not in CATALOGO:
            raise KeyError(f"Código de error '{codigo}' no está en el catálogo (app/core/errors.py).")
        self.codigo = codigo
        self.status, self.titulo, detalle_defecto = CATALOGO[codigo]
        self.detalle = detalle or detalle_defecto
        self.headers = headers or {}
        self.extra = extra
        super().__init__(self.detalle)


def desde_odoo(e: OdooClientError) -> ApiError:
    """
    Odoo speaks English internally (ADR-012): the app always gets the
    catalogue text. A code of the contract passes through with its extra
    members (stock lines, for instance).

    A 401 or 403 without a code is Odoo refusing the Edge itself (signature,
    allowed IP), never the customer: answering sesion-expirada or
    telefono-no-verificado would send the app to the wrong screen.
    """
    codigo = (e.extra or {}).get("codigo")
    if codigo in CATALOGO and CATALOGO[codigo][0] == e.status_code:
        extra = {k: v for k, v in e.extra.items() if k != "codigo"}
        return ApiError(codigo, **extra)
    if e.status_code in (401, 403):
        _logger.error("Odoo refused the Edge (%s %s): check ODOO_HMAC_SECRET and the allowed IPs",
                      e.status_code, e.title)
        return ApiError("servicio-no-disponible")
    if e.status_code >= 500:
        _logger.warning("Odoo answered %s: %s", e.status_code, e.detail)
    return ApiError(codigo_para_estado(e.status_code))


def desde_otp(e: OtpProviderError) -> ApiError:
    codigo = _CODIGO_POR_MOTIVO_OTP.get(e.reason)
    if codigo:
        return ApiError(codigo)
    # A 401/403 from the provider is about our Twilio account (credentials,
    # compliance profile, geo permissions), never about the customer.
    if e.status_code in (401, 403):
        _logger.error("Twilio refused the Edge (%s): %s", e.status_code, e.detail)
        return ApiError("servicio-no-disponible")
    return ApiError(codigo_para_estado(e.status_code))


def _respuesta(request: Request, error: ApiError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None) or request.headers.get("X-Request-Id", "")
    cuerpo = {
        "type": TIPO_BASE + error.codigo,
        "title": error.titulo,
        "status": error.status,
        "detail": error.detalle,
        "instance": request.url.path,
        "request_id": request_id,
        **error.extra,
    }
    headers = {"X-Request-Id": request_id, **error.headers} if request_id else error.headers
    return JSONResponse(status_code=error.status, content=cuerpo, headers=headers, media_type=MEDIA_TYPE)


def _campo(loc: tuple) -> str:
    # ("query", "provincia_id") -> "provincia_id"; ("body", "lineas", 0, "sku") -> "lineas.0.sku"
    partes = [str(p) for p in loc if p not in ("body", "query", "path", "header", "cookie")]
    return ".".join(partes) or ".".join(str(p) for p in loc)


def instalar_manejadores(app: FastAPI) -> None:
    """Every failure path of the app ends in the same problem document."""

    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return _respuesta(request, exc)

    @app.exception_handler(RequestValidationError)
    async def _validacion(request: Request, exc: RequestValidationError):
        errores = [{"campo": _campo(err.get("loc", ())), "mensaje": err.get("msg", "")} for err in exc.errors()]
        return _respuesta(request, ApiError("parametros-invalidos", errores=errores))

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        return _respuesta(request, ApiError(codigo_para_estado(exc.status_code), headers=exc.headers))

    @app.exception_handler(OdooClientError)
    async def _odoo(request: Request, exc: OdooClientError):
        return _respuesta(request, desde_odoo(exc))

    @app.exception_handler(OtpProviderError)
    async def _otp(request: Request, exc: OtpProviderError):
        return _respuesta(request, desde_otp(exc))

    @app.exception_handler(CircuitBreakerOpenError)
    async def _cortacircuitos(request: Request, exc: CircuitBreakerOpenError):
        return _respuesta(request, ApiError("servicio-no-disponible"))

    @app.exception_handler(Exception)
    async def _inesperado(request: Request, exc: Exception):
        _logger.exception("Unhandled error on %s", request.url.path)
        return _respuesta(request, ApiError("error-interno"))
