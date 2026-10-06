from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    ENVIRONMENT: str = "development"
    PORT: int = 8080

    # Redis Cloud (us-central1)
    REDIS_CACHE_URL: str = "redis://localhost:6379/0"
    REDIS_STATE_URL: str = "redis://localhost:6379/1"
    REDIS_MAX_CONNECTIONS: int = 20

    # Odoo 18 Backend
    ODOO_BASE_URL: str = "http://localhost:8081"
    ODOO_HMAC_SECRET: str = ""
    ODOO_MAX_CONCURRENT: int = 20  # Hard semaphore ceiling (Rule 5)
    ODOO_TIMEOUT_SECONDS: float = 10.0

    # Meilisearch
    MEILI_URL: str = "http://localhost:7700"
    MEILI_KEY: str = ""

    # Security & Tokens
    JWT_SECRET_KEY: str = "dev_secret_key_change_in_production"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_EXPIRE_MINUTES: int = 15
    JWT_REFRESH_EXPIRE_DAYS: int = 30

    # Twilio Verify (OTP as a service — ADR-013)
    # The Edge never generates nor stores the code: it asks Verify for a verdict.
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_VERIFY_SERVICE_SID: str = ""
    TWILIO_BASE_URL: str = "https://verify.twilio.com/v2"
    TWILIO_TIMEOUT_SECONDS: float = 8.0
    TWILIO_LOCALE: str = "es"
    # SendGrid dynamic templates (d-...) for the 'correo' channel. Empty falls back
    # to the default template of the Verify email integration.
    TWILIO_EMAIL_TEMPLATE_EMAIL_VERIFICATION: str = ""
    TWILIO_EMAIL_TEMPLATE_PASSWORD_RESET: str = ""

    # Master data for the app selectors. Districts and document types barely
    # ever move; home messages are editorial, so taking one down has to show up
    # in minutes, not tomorrow.
    MASTER_DATA_TTL_SECONDS: int = 86400
    HOME_MESSAGES_TTL_SECONDS: int = 300
    # Delivery zones are operational: shrinking one, or taking a store off
    # the map, has to reach customers in minutes.
    COVERAGE_ZONES_TTL_SECONDS: int = 600

    # RENIEC identity lookup via json.pe (ADR-014). It runs here rather than in
    # Odoo so a slow third party cannot hold an Odoo.sh worker.
    RENIEC_API_URL: str = "https://api.json.pe/api/dni"
    RENIEC_API_TOKEN: str = ""
    RENIEC_TIMEOUT_SECONDS: float = 4.0
    RENIEC_CACHE_TTL_SECONDS: int = 86400  # Each lookup spends a credit

    # Verification channels (RF-062). 'whatsapp' is declared in the frozen contract
    # but stays out of this list until the WABA template is approved.
    OTP_CHANNELS_ENABLED: str = "sms,correo"
    OTP_CHANNEL_DEFAULT: str = "sms"

    # OTP flow limits. These protect the invoice; Twilio's own limits protect Twilio.
    OTP_CODE_LENGTH: int = 6
    OTP_MAX_CHECK_ATTEMPTS: int = 5
    OTP_RESEND_COOLDOWN_SECONDS: int = 60
    OTP_MAX_SENDS_PER_PHONE_HOUR: int = 3
    OTP_MAX_SENDS_PER_PHONE_DAY: int = 10
    OTP_MAX_SENDS_PER_IP_HOUR: int = 20

    # Lifetimes of the registration handshake (holi-estado)
    REGISTRATION_DRAFT_TTL_SECONDS: int = 900   # 15 min
    OTP_SESSION_TTL_SECONDS: int = 600          # 10 min
    VERIFICATION_TOKEN_EXPIRE_MINUTES: int = 10

    # Password policy surfaced in the public contract (M1.9 validates it live)
    PASSWORD_MIN_LENGTH: int = 8

    @property
    def otp_channels(self) -> list[str]:
        return [c.strip() for c in self.OTP_CHANNELS_ENABLED.split(",") if c.strip()]

    @property
    def phone_otp_channels(self) -> list[str]:
        """Channels that prove possession of the phone. Sign-up accepts only these (ADR-017)."""
        return [c for c in self.otp_channels if c in ("sms", "whatsapp")]


settings = Settings()
