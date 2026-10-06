import re

from pydantic import BaseModel, field_validator


class CorreoVerificarIn(BaseModel):
    codigo: str

    @field_validator("codigo")
    @classmethod
    def _codigo_valido(cls, v: str) -> str:
        v = re.sub(r"\D", "", v or "")
        if not re.fullmatch(r"\d{4,10}", v):
            raise ValueError("El código debe ser numérico.")
        return v


class CorreoVerificadoOut(BaseModel):
    correo_verificado: bool
