from fastapi.testclient import TestClient
from pydantic import SecretStr

from issue_analyst_api.api.app import create_app
from issue_analyst_api.config import Settings


def test_health_returns_ok_without_database_or_broker() -> None:
    # URLs apontam para portas fechadas: o /health não pode depender delas.
    settings = Settings(
        GITHUB_WEBHOOK_SECRET=SecretStr("test-secret"),
        DATABASE_URL="postgresql://nobody:nothing@127.0.0.1:1/none",
        RABBITMQ_URL="amqp://nobody:nothing@127.0.0.1:1/",
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
