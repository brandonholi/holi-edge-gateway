import re
from typing import List, Optional
from pydantic import BaseModel, Field, field_validator

# ADR-012: the public contract speaks Spanish. Odoo's internal endpoints and Redis
# speak English, and the translation happens here in the Edge and nowhere else.

_CORREO_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class RegistroIn(BaseModel):
    """Step 1 of the sign-up handshake. Nothing is written to Odoo yet."""
    tipo_documento_id: int = Field(ge=1, description="id de un tipo de /document-types")
    numero_documento: str = Field(description="Se valida con la longitud y solo_digitos del tipo")
    correo: str
    telefono: str = Field(description="Celular peruano de 9 dígitos, sin prefijo país")
    nombre: Optional[str] = Field(
        default=None, description="Obligatorio si el documento no es DNI o si RENIEC no responde")
    apellido: Optional[str] = Field(
        default=None, description="Obligatorio si el documento no es DNI o si RENIEC no responde")
    fecha_nacimiento: Optional[str] = None
    acepta_terminos: bool = False
    acepta_comerciales: bool = False

    @field_validator("numero_documento")
    @classmethod
    def _numero_normalizado(cls, v: str) -> str:
        # Same normalization as Odoo: no spaces or dashes, upper case.
        v = "".join((v or "").split()).replace("-", "").upper()
        if not v:
            raise ValueError("Ingresa el número de documento.")
        return v

    @field_validator("telefono")
    @classmethod
    def _telefono_valido(cls, v: str) -> str:
        v = re.sub(r"\D", "", v or "")
        if not re.fullmatch(r"9\d{8}", v):
            raise ValueError("El teléfono debe tener 9 dígitos y empezar en 9.")
        return v

    @field_validator("correo")
    @classmethod
    def _correo_valido(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not _CORREO_RE.fullmatch(v):
            raise ValueError("Ingresa un correo electrónico válido.")
        return v


class RegistroOut(BaseModel):
    registro_id: str
    nombre_precargado: Optional[str] = None
    nombres_precargados: Optional[str] = None
    apellidos_precargados: Optional[str] = None
    datos_desde_reniec: bool = Field(
        default=False,
        description="Si es true los nombres vienen de RENIEC y la app debe mostrarlos bloqueados"
    )
    estado_validacion: str = Field(description="validado · pendiente · rechazado")
    motivo_validacion: str = Field(
        default="",
        description="Vacío si validado; si no: documento_no_encontrado · servicio_no_disponible · tipo_sin_validacion"
    )
    canales_disponibles: List[str]
    canal_sugerido: str
    expira_en: int = Field(description="Segundos de vida del borrador de registro")


class OtpEnviarIn(BaseModel):
    registro_id: str
    canal: Optional[str] = Field(default=None, description="sms · whatsapp · correo")


class OtpEnviarOut(BaseModel):
    enviado: bool
    canal: str
    destino_enmascarado: str
    reintento_en: int = Field(description="Segundos hasta poder reenviar")
    expira_en: int = Field(description="Segundos de vida del código")


class OtpVerificarIn(BaseModel):
    registro_id: str
    codigo: str

    @field_validator("codigo")
    @classmethod
    def _codigo_valido(cls, v: str) -> str:
        v = re.sub(r"\D", "", v or "")
        if not re.fullmatch(r"\d{4,10}", v):
            raise ValueError("El código debe ser numérico.")
        return v


class OtpVerificarOut(BaseModel):
    verificado: bool
    token_verificacion: str
    expira_en: int


class RegistroCompletarIn(BaseModel):
    token_verificacion: str
    contrasena: str


class LoginIn(BaseModel):
    correo: str
    contrasena: str

    @field_validator("correo")
    @classmethod
    def _correo_normalizado(cls, v: str) -> str:
        return (v or "").strip().lower()


class RenovarIn(BaseModel):
    token_renovacion: str


class SesionOut(BaseModel):
    """Session pair of RF-051: short signed access token, rotating refresh token."""
    token_acceso: str
    tipo_token: str = "Bearer"
    expira_en: int = 900
    token_renovacion: Optional[str] = None
    cliente_id: Optional[int] = None
    estado_validacion: Optional[str] = None
    datos_fiscales_completos: Optional[bool] = None
    correo_verificado: Optional[bool] = Field(
        default=None,
        description="Si el correo está verificado. La compra exige correo y teléfono verificados",
    )
    requiere_codigo: bool = Field(
        default=False,
        description="Siempre false en v1. Reservado para pedir un código en el login sin abrir /v2",
    )


class PasswordResetIn(BaseModel):
    correo: str

    @field_validator("correo")
    @classmethod
    def _correo_normalizado(cls, v: str) -> str:
        return (v or "").strip().lower()


class PasswordResetOut(BaseModel):
    registro_id: str
    canales_disponibles: List[str]
    canal_sugerido: str
    expira_en: int


class PasswordResetCompletarIn(BaseModel):
    token_verificacion: str
    contrasena: str

