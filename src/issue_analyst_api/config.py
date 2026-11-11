from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuração do processo, lida das variáveis de ambiente e de um `.env` opcional."""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    LOG_LEVEL: str = "INFO"
    SERVICE_NAME: str = "issue-analyst-api"
    HTTP_PORT: int = 8000
    DATABASE_URL: str
    RABBITMQ_URL: str
    GITHUB_WEBHOOK_SECRET: SecretStr
    WEBHOOK_MAX_BODY_BYTES: int = 1_048_576
    MAX_TITLE_CHARS: int = 256
    MAX_DESCRIPTION_CHARS: int = 500
    RELAY_POLL_INTERVAL_S: float = 1
    RELAY_BATCH_SIZE: int = 100
