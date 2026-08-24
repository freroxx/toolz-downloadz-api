import os
from typing import List, Optional
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Security
    api_secret_key: Optional[str] = Field(default=None, alias="API_SECRET_KEY")
    # Comma-separated list of allowed origins, or "*" for all. Keep "*" default for backward compat
    cors_origins: str = Field(default="*", alias="CORS_ORIGINS")
    # Optional comma-separated list of trusted API keys (for rotation). If set, any matches
    trusted_api_keys: Optional[str] = Field(default=None, alias="TRUSTED_API_KEYS")

    # Extractor tuning
    youtube_cookies: Optional[str] = Field(default=None, alias="YOUTUBE_COOKIES")
    yt_cookies_file: Optional[str] = Field(default=None, alias="YT_COOKIES_FILE")
    pot_provider_url: Optional[str] = Field(default=None, alias="YT_DLP_POT_PROVIDER_URL")
    pot_provider_url_alt: Optional[str] = Field(default=None, alias="POT_PROVIDER_URL")

    # Infra
    redis_url: Optional[str] = Field(default=None, alias="REDIS_URL")
    cache_ttl: int = Field(default=21600, alias="CACHE_TTL")  # 6h default
    enable_cache: bool = Field(default=True, alias="ENABLE_CACHE")

    # Rate limiting: "60/minute" etc
    rate_limit_extract: str = Field(default="30/minute", alias="RATE_LIMIT_EXTRACT")
    rate_limit_default: str = Field(default="60/minute", alias="RATE_LIMIT_DEFAULT")

    # App meta
    app_name: str = "toolz-downloadz-api"
    version: str = "2.0.0"
    environment: str = Field(default="production", alias="ENVIRONMENT")
    log_level: str = Field(default="info", alias="LOG_LEVEL")

    # Sentry
    sentry_dsn: Optional[str] = Field(default=None, alias="SENTRY_DSN")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
        "populate_by_name": True,
    }

    @property
    def resolved_pot_url(self) -> Optional[str]:
        return self.pot_provider_url or self.pot_provider_url_alt

    @property
    def allowed_origins(self) -> List[str]:
        if self.cors_origins.strip() == "*":
            return ["*"]
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def allowed_api_keys(self) -> List[str]:
        keys = []
        if self.api_secret_key:
            keys.append(self.api_secret_key.strip())
        if self.trusted_api_keys:
            for k in self.trusted_api_keys.split(","):
                k = k.strip()
                if k and k not in keys:
                    keys.append(k)
        return keys


# Singleton — loaded once
settings = Settings()

# Helper to reload in tests
def get_settings() -> Settings:
    return settings
