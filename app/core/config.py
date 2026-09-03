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


settings = Settings()
