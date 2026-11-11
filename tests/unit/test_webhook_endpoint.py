import hashlib
import hmac
import json
import logging
from collections.abc import Iterator

import httpx
import pytest
import structlog
from fastapi.testclient import TestClient
from pydantic import SecretStr

from issue_analyst_api.adapters.issue_events import DatabaseUnavailableError
from issue_analyst_api.api import webhooks
from issue_analyst_api.api.app import create_app
from issue_analyst_api.config import Settings

SECRET = "endpoint-test-secret-value"
URL = "/webhooks/github"
MAX_BODY_BYTES = 1_048_576  # padrão de WEBHOOK_MAX_BODY_BYTES
VALID_BODY = json.dumps(
    {
        "action": "opened",
        "issue": {
            "number": 1,
            "title": "Erro ao salvar",
            "body": "Passos para reproduzir",
            "created_at": "2026-11-10T12:00:00Z",
            "updated_at": "2026-11-10T12:00:00Z",
        },
        "repository": {"full_name": "acme/app"},
        "sender": {"type": "User"},
    }
).encode()
ISSUE_HEADERS = {"X-GitHub-Event": "issues", "X-GitHub-Delivery": "evt-001"}


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _settings(**overrides: object) -> Settings:
    # Banco e broker em portas fechadas: estas verificações não podem depender deles.
    return Settings(
        GITHUB_WEBHOOK_SECRET=SecretStr(SECRET),
        DATABASE_URL="postgresql://nobody:nothing@127.0.0.1:1/none",
        RABBITMQ_URL="amqp://nobody:nothing@127.0.0.1:1/",
        **overrides,
    )


@pytest.fixture(autouse=True)
def _fake_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Substitui a gravação no banco: estes testes cobrem só o caminho HTTP."""

    async def fake_store(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(webhooks, "store_issue_created", fake_store)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(_settings())) as test_client:
        yield test_client


def _post(client: TestClient, body: bytes, signature: str | None) -> httpx.Response:
    headers = {"Content-Type": "application/json", **ISSUE_HEADERS}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post(URL, content=body, headers=headers)


def test_valid_signature_passes_checks_and_returns_202(client: TestClient) -> None:
    response = _post(client, VALID_BODY, _sign(VALID_BODY))

    assert response.status_code == 202


def test_missing_signature_returns_401_without_body(client: TestClient) -> None:
    response = _post(client, VALID_BODY, None)

    assert response.status_code == 401
    assert response.content == b""


def test_wrong_signature_returns_401_without_body(client: TestClient) -> None:
    response = _post(client, VALID_BODY, _sign(VALID_BODY, secret="wrong-secret"))

    assert response.status_code == 401
    assert response.content == b""


@pytest.mark.parametrize("signature", ["sha256=not-hex", "md5=abc", "garbage", ""])
def test_malformed_signature_returns_401_without_body(client: TestClient, signature: str) -> None:
    response = _post(client, VALID_BODY, signature)

    assert response.status_code == 401
    assert response.content == b""


def test_body_above_limit_returns_413_before_checking_signature(client: TestClient) -> None:
    body = b"x" * (MAX_BODY_BYTES + 1)

    response = _post(client, body, "sha256=" + "0" * 64)

    assert response.status_code == 413
    assert response.json() == {"error": "payload_too_large"}


def test_body_exactly_at_limit_is_not_rejected_by_size(client: TestClient) -> None:
    body = b" " * (MAX_BODY_BYTES - len(VALID_BODY)) + VALID_BODY

    response = _post(client, body, _sign(body))

    assert response.status_code == 202


@pytest.mark.parametrize("body", [b"{not json", b"", b"\xff\xfe\x00garbage"])
def test_invalid_json_with_valid_signature_returns_400(client: TestClient, body: bytes) -> None:
    response = _post(client, body, _sign(body))

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_payload", "fields": []}


def test_invalid_json_with_invalid_signature_returns_401(client: TestClient) -> None:
    response = _post(client, b"{not json", _sign(b"{not json", secret="wrong-secret"))

    assert response.status_code == 401


def test_secret_never_appears_in_logs(
    client: TestClient, caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    caplog.set_level(logging.DEBUG)
    with structlog.testing.capture_logs() as captured:
        _post(client, VALID_BODY, _sign(VALID_BODY))
        _post(client, VALID_BODY, None)
        _post(client, VALID_BODY, _sign(VALID_BODY, secret="wrong-secret"))
        _post(client, b"{not json", _sign(b"{not json"))
        _post(client, b"x" * (MAX_BODY_BYTES + 1), None)

    out, err = capsys.readouterr()
    assert SECRET not in repr(captured)
    assert SECRET not in caplog.text
    assert SECRET not in out
    assert SECRET not in err


def test_accepted_body_has_event_id_and_result_link(client: TestClient) -> None:
    response = _post(client, VALID_BODY, _sign(VALID_BODY))

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "eventId": "evt-001",
        "links": {"result": "/issues/acme/app/1"},
    }


def test_not_inserted_returns_200_duplicate_with_event_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def already_stored(*_args: object, **_kwargs: object) -> bool:
        return False

    monkeypatch.setattr(webhooks, "store_issue_created", already_stored)

    response = _post(client, VALID_BODY, _sign(VALID_BODY))

    assert response.status_code == 200
    assert response.json() == {"status": "duplicate", "eventId": "evt-001"}


def test_database_unavailable_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(*_args: object, **_kwargs: object) -> bool:
        raise DatabaseUnavailableError

    monkeypatch.setattr(webhooks, "store_issue_created", unavailable)

    response = _post(client, VALID_BODY, _sign(VALID_BODY))

    assert response.status_code == 503
    assert response.json() == {"error": "unavailable"}


def test_edited_issue_returns_provisional_501_without_storing(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []

    async def spy(*args: object, **_kwargs: object) -> bool:
        calls.append(args)
        return True

    monkeypatch.setattr(webhooks, "store_issue_created", spy)
    body = VALID_BODY.replace(b'"opened"', b'"edited"')

    response = _post(client, body, _sign(body))

    assert response.status_code == 501
    assert calls == []
