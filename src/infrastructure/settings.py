from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class EngineSettings(BaseSettings):
    """Конфиг движка. Все поля читаются из env с префиксом ENGINE_."""

    model_config = SettingsConfigDict(
        env_prefix="ENGINE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        ...,
        description="postgresql+asyncpg://user:pass@host:5432/db (общий с бэком)",
    )
    redis_url: str = Field(
        ...,
        description="redis://host:6379/0 (общий с бэком)",
    )
    encryption_key: str = Field(
        ...,
        description="Fernet-ключ. ОБЯЗАН совпадать с BACKEND_ENCRYPTION_KEY.",
    )
    log_level: str = Field(default="INFO")
    heartbeat_interval_sec: int = Field(default=10, ge=1)
    balance_poll_interval_sec: int = Field(default=5, ge=1)
    state_snapshot_ttl_sec: int = Field(default=30, ge=5)
    command_dedup_ttl_sec: int = Field(default=86400, ge=60)
    command_poll_interval_sec: float = Field(default=1.0, ge=0.1)
    metrics_enabled: bool = Field(default=True)
    metrics_host: str = Field(default="0.0.0.0")
    metrics_port: int = Field(default=9100, ge=1, le=65535)


def load_settings() -> EngineSettings:
    return EngineSettings()  # type: ignore[call-arg]
