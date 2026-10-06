import re
from .config import settings

_UPPER = re.compile(r"[A-ZÁÉÍÓÚÑ]")
_LOWER = re.compile(r"[a-záéíóúñ]")
_DIGIT = re.compile(r"[0-9]")


class PasswordPolicyError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def validate_password_policy(password: str) -> None:
    """
    The rules the app shows live while the customer types (M1.9). They are
    checked here too because a mobile client is not a trust boundary.
    """
    if not password or len(password) < settings.PASSWORD_MIN_LENGTH:
        raise PasswordPolicyError(
            f"La contraseña debe tener al menos {settings.PASSWORD_MIN_LENGTH} caracteres."
        )
    if len(password) > 128:
        raise PasswordPolicyError("La contraseña no puede exceder 128 caracteres.")
    if not _UPPER.search(password):
        raise PasswordPolicyError("La contraseña debe incluir al menos una letra mayúscula.")
    if not _LOWER.search(password):
        raise PasswordPolicyError("La contraseña debe incluir al menos una letra minúscula.")
    if not _DIGIT.search(password):
        raise PasswordPolicyError("La contraseña debe incluir al menos un número.")


def mask_destination(destination: str, channel: str) -> str:
    """Echoes back where the code went without printing it in full."""
    if not destination:
        return ""
    if channel == "correo" or "@" in destination:
        user, _, domain = destination.partition("@")
        visible = user[:2] if len(user) > 2 else user[:1]
        return f"{visible}{'*' * max(len(user) - len(visible), 1)}@{domain}"
    digits = re.sub(r"\D", "", destination)
    return f"{'*' * max(len(digits) - 3, 0)}{digits[-3:]}" if digits else ""
