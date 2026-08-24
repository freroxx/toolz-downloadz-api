import os
from typing import List, Optional
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Security
    api_secret_key: Optional[str] = Field(default=None, alias="API_SECRET_KEY")
    cors_origins: str = Field(default="*", alias="CORS_ORIGINS")
    trusted_api_keys: Optional[str] = Field(default=None, alias="TRUSTED_API_KEYS")

    # Extractor tuning — Vercel serverless optimized
    youtube_cookies: Optional[str] = Field(default=None, alias="YOUTUBE_COOKIES")
    yt_cookies_file: Optional[str] = Field(default=None, alias="YT_COOKIES_FILE")
    pot_provider_url: Optional[str] = Field(default=None, alias="YT_DLP_POT_PROVIDER_URL")
    pot_provider_url_alt: Optional[str] = Field(default=None, alias="POT_PROVIDER_URL")
    # Socket timeout tuned for Vercel (10s default, 20s would hit function timeout)
    socket_timeout: int = Field(default=10, alias="YTDL_SOCKET_TIMEOUT")

    # Cache — Vercel uses in-memory by default, Upstash Redis REST for persistence across lambdas
    redis_url: Optional[str] = Field(default=None, alias="REDIS_URL")
    # Upstash Redis REST (Vercel KV) — preferred on Vercel
    upstash_redis_rest_url: Optional[str] = Field(default=None, alias="UPSTASH_REDIS_REST_URL")
    upstash_redis_rest_token: Optional[str] = Field(default=None, alias="UPSTASH_REDIS_REST_TOKEN")
    # Vercel KV aliases
    kv_rest_api_url: Optional[str] = Field(default=None, alias="KV_REST_API_URL")
    kv_rest_api_token: Optional[str] = Field(default=None, alias="KV_REST_API_TOKEN")
    kv_url: Optional[str] = Field(default=None, alias="KV_URL")

    cache_ttl: int = Field(default=21600, alias="CACHE_TTL")  # 6h
    enable_cache: bool = Field(default=True, alias="ENABLE_CACHE")
    # Max extract wall time (seconds) — Vercel hobby caps at 10s, so keep <10. Pro can do 30.
    extract_timeout: int = Field(default=25, alias="EXTRACT_TIMEOUT")

    # Rate limiting: "60/minute" etc — on Vercel this is per-instance memory unless Upstash is configured
    rate_limit_extract: str = Field(default="30/minute", alias="RATE_LIMIT_EXTRACT")
    rate_limit_default: str = Field(default="60/minute", alias="RATE_LIMIT_DEFAULT")

    # App meta
    app_name: str = "toolz-downloadz-api"
    version: str = "2.0.0"
    environment: str = Field(default="production", alias="ENVIRONMENT")
    log_level: str = Field(default="info", alias="LOG_LEVEL")

    # Sentry (optional on Vercel)
    sentry_dsn: Optional[str] = Field(default=None, alias="SENTRY_DSN")

    # Vercel detection
    vercel: bool = Field(default=False, alias="VERCEL")
    vercel_env: Optional[str] = Field(default=None, alias="VERCEL_ENV")

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
    def is_vercel(self) -> bool:
        # Vercel sets VERCEL=1 automatically
        return self.vercel or os.getenv("VERCEL") == "1"

    @property
    def resolved_upstash_url(self) -> Optional[str]:
        return self.upstash_redis_rest_url or self.kv_rest_api_url

    @property
    def resolved_upstash_token(self) -> Optional[str]:
        return self.upstash_redis_rest_token or self.kv_rest_api_token

    @property
    def has_persistent_cache(self) -> bool:
        return bool(self.redis_url or self.resolved_upstash_url)

    @property
    def effective_socket_timeout(self) -> int:
        # On Vercel, cap to 12s to leave headroom for function maxDuration (10s hobby / 30s pro)
        if self.is_vercel:
            return min(self.socket_timeout, 12)
        return self.socket_timeout

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


settings = Settings()

def get_settings() -> Settings:
    return settings
