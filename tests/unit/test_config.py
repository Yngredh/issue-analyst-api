import pytest
from pydantic import ValidationError

from issue_analyst_api.config import Settings

ENV_VARS = (
    "LOG_LEVEL",
    "SERVICE_NAME",
    "HTTP_PORT",
    "DATABASE_URL",
    "RABBITMQ_URL",
    "GITHUB_WEBHOOK_SECRET",
    "MAX_TITLE_CHARS",
    "MAX_DESCRIPTION_CHARS",
    "RELAY_POLL_INTERVAL_S",
    "RELAY_BATCH_SIZE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir("/")  # evita ler um .env local
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost:5432/db")
    monkeypatch.setenv("RABBITMQ_URL", "amqp://user:pass@localhost:5672/")


def test_settings_load_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")

    settings = Settings()

    assert settings.HTTP_PORT == 8000

    assert settings.LOG_LEVEL == "INFO"
    assert settings.SERVICE_NAME == "issue-analyst-api"
    assert settings.MAX_TITLE_CHARS == 256
    assert settings.MAX_DESCRIPTION_CHARS == 500
    assert settings.RELAY_POLL_INTERVAL_S == 1
    assert settings.RELAY_BATCH_SIZE == 100


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("HTTP_PORT", "9000")
    monkeypatch.setenv("MAX_DESCRIPTION_CHARS", "800")

    settings = Settings()

    assert settings.LOG_LEVEL == "DEBUG"
    assert settings.HTTP_PORT == 9000
    assert settings.MAX_DESCRIPTION_CHARS == 800


def test_settings_fail_without_webhook_secret() -> None:
    with pytest.raises(ValidationError, match="GITHUB_WEBHOOK_SECRET"):
        Settings()


def test_webhook_secret_is_not_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", "test-secret")

    settings = Settings()

    assert settings.GITHUB_WEBHOOK_SECRET.get_secret_value() == "test-secret"
    assert "test-secret" not in repr(settings)
