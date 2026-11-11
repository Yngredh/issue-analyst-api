import hashlib
import hmac
import logging
from collections.abc import Iterator

import httpx
import pytest
import structlog
from fastapi.testclient import TestClient
from pydantic import SecretStr

from issue_analyst_api.api.app import create_app
from issue_analyst_api.api.webhooks import MAX_BODY_BYTES
from issue_analyst_api.config import Settings

SECRET = "endpoint-test-secret-value"
URL = "/webhooks/github"
VALID_BODY = b'{"action": "opened", "issue": {"number": 1}}'


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client() -> Iterator[TestClient]:
    # Banco e broker em portas fechadas: estas verificações não podem depender deles.
    settings = Settings(
        GITHUB_WEBHOOK_SECRET=SecretStr(SECRET),
        DATABASE_URL="postgresql://nobody:nothing@127.0.0.1:1/none",
        RABBITMQ_URL="amqp://nobody:nothing@127.0.0.1:1/",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _post(client: TestClient, body: bytes, signature: str | None) -> httpx.Response:
    headers = {"Content-Type": "application/json"}
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post(URL, content=body, headers=headers)


def test_valid_signature_passes_checks_and_returns_provisional_501(client: TestClient) -> None:
    response = _post(client, VALID_BODY, _sign(VALID_BODY))

    assert response.status_code == 501


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

    assert response.status_code == 501


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
