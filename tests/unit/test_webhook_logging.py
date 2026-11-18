"""Log de decisão do webhook (RNF14): um log por resposta, `eventId` no contexto e nenhum dado
sensível."""

import hashlib
import hmac
import json
import logging
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import httpx
import pytest
import structlog
from fastapi.testclient import TestClient
from pydantic import SecretStr

from issue_analyst_api.adapters.issue_events import (
    DatabaseUnavailableError,
    Duplicate,
    DuplicateReason,
)
from issue_analyst_api.api import webhooks
from issue_analyst_api.api.app import create_app
from issue_analyst_api.config import Settings

SECRET = "logging-test-secret-value"
URL = "/webhooks/github"
TITLE = "Titulo-sensivel-que-nao-vai-para-o-log"
DESCRIPTION = "Descricao-completa-que-nunca-deve-aparecer-no-log"
EVENT_ID = "evt-log-001"
AUTO = "auto"


def _body(**issue_overrides: Any) -> bytes:
    issue = {
        "number": 7,
        "title": TITLE,
        "body": DESCRIPTION,
        "created_at": "2026-11-10T12:00:00Z",
    }
    issue.update(issue_overrides)
    return json.dumps(
        {
            "action": "opened",
            "issue": issue,
            "repository": {"full_name": "acme/app"},
            "sender": {"type": "User"},
        }
    ).encode()


def _sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _settings(**overrides: object) -> Settings:
    # Banco e broker em portas fechadas: a gravação é substituída nestes testes.
    return Settings(
        GITHUB_WEBHOOK_SECRET=SecretStr(SECRET),
        DATABASE_URL="postgresql://nobody:nothing@127.0.0.1:1/none",
        RABBITMQ_URL="amqp://nobody:nothing@127.0.0.1:1/",
        **overrides,
    )


def _returning(value: object) -> Callable[..., Awaitable[object]]:
    async def store(*_args: object, **_kwargs: object) -> object:
        return value

    return store


@pytest.fixture(autouse=True)
def _fake_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Substitui a gravação no banco: estes testes cobrem só o log do caminho HTTP."""
    monkeypatch.setattr(webhooks, "store_issue_created", _returning(None))


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(create_app(_settings())) as test_client:
        yield test_client


def _post(
    client: TestClient,
    body: bytes,
    *,
    signature: str | None = AUTO,
    event: str = "issues",
    delivery_id: str | None = EVENT_ID,
) -> httpx.Response:
    headers = {"Content-Type": "application/json", "X-GitHub-Event": event}
    if delivery_id is not None:
        headers["X-GitHub-Delivery"] = delivery_id
    if signature == AUTO:
        signature = _sign(body)
    if signature is not None:
        headers["X-Hub-Signature-256"] = signature
    return client.post(URL, content=body, headers=headers)


def _post_and_capture(
    client: TestClient, body: bytes, **kwargs: Any
) -> tuple[int, list[dict[str, Any]]]:
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        response = _post(client, body, **kwargs)
    return response.status_code, logs


def _responses(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry["event"] == "webhook_response"]


def test_accepted_logs_one_decision_with_event_id(client: TestClient) -> None:
    status, logs = _post_and_capture(client, _body())

    assert status == 202
    assert _responses(logs) == [
        {
            "event": "webhook_response",
            "log_level": "info",
            "eventId": EVENT_ID,
            "httpStatus": 202,
            "decision": "accepted",
            "reason": None,
            "type": "issue.created",
            "repository": "acme/app",
            "issueNumber": 7,
            "missingDescription": False,
        }
    ]


@pytest.mark.parametrize("reason", list(DuplicateReason))
def test_duplicate_logs_decision_and_reason(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, reason: DuplicateReason
) -> None:
    monkeypatch.setattr(webhooks, "store_issue_created", _returning(Duplicate(reason, "ACCEPTED")))

    status, logs = _post_and_capture(client, _body())

    assert status == 200
    assert _responses(logs) == [
        {
            "event": "webhook_response",
            "log_level": "info",
            "eventId": EVENT_ID,
            "httpStatus": 200,
            "decision": "duplicate",
            "reason": reason.value,
            "state": "ACCEPTED",
        }
    ]


@pytest.mark.parametrize(
    ("event", "body", "reason"),
    [
        ("ping", b'{"zen": "x"}', "ping"),
        ("pull_request", _body(), "unsupported_event"),
        ("issues", _body().replace(b'"opened"', b'"closed"'), "unsupported_action"),
        ("issues", _body().replace(b'"User"', b'"Bot"'), "bot_sender"),
    ],
)
def test_ignored_logs_decision_and_reason(
    client: TestClient, event: str, body: bytes, reason: str
) -> None:
    status, logs = _post_and_capture(client, body, event=event)

    assert status == 200
    assert _responses(logs) == [
        {
            "event": "webhook_response",
            "log_level": "info",
            "eventId": EVENT_ID,
            "httpStatus": 200,
            "decision": "ignored",
            "reason": reason,
        }
    ]


def test_validation_rejection_logs_code_and_field_names(client: TestClient) -> None:
    status, logs = _post_and_capture(client, _body(title="   "))

    assert status == 400
    assert _responses(logs) == [
        {
            "event": "webhook_response",
            "log_level": "warning",
            "eventId": EVENT_ID,
            "httpStatus": 400,
            "decision": "rejected",
            "reason": "missing_fields",
            "fields": ["title"],
        }
    ]


def test_missing_event_id_rejection_has_no_event_id(client: TestClient) -> None:
    status, logs = _post_and_capture(client, _body(), delivery_id=None)

    assert status == 400
    [entry] = _responses(logs)
    assert (entry["decision"], entry["reason"]) == ("rejected", "missing_event_id")
    assert "eventId" not in entry


@pytest.mark.parametrize(
    ("signature", "body", "http_status", "reason"),
    [
        (None, _body(), 401, "invalid_signature"),
        (_sign(b"other", "wrong-secret"), _body(), 401, "invalid_signature"),
        (AUTO, b"{not json", 400, "invalid_payload"),
    ],
)
def test_other_rejections_log_reason(
    client: TestClient, signature: str | None, body: bytes, http_status: int, reason: str
) -> None:
    status, logs = _post_and_capture(client, body, signature=signature)

    assert status == http_status
    [entry] = _responses(logs)
    assert (entry["httpStatus"], entry["decision"], entry["reason"]) == (
        http_status,
        "rejected",
        reason,
    )


def test_invalid_signature_does_not_bind_event_id(client: TestClient) -> None:
    _, logs = _post_and_capture(client, _body(), signature=None)

    [entry] = _responses(logs)
    assert "eventId" not in entry


def test_payload_too_large_logs_rejection() -> None:
    with TestClient(create_app(_settings(WEBHOOK_MAX_BODY_BYTES=10))) as small_client:
        status, logs = _post_and_capture(small_client, _body())

    assert status == 413
    [entry] = _responses(logs)
    assert (entry["httpStatus"], entry["decision"], entry["reason"]) == (
        413,
        "rejected",
        "payload_too_large",
    )


def test_database_unavailable_logs_rejection(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def unavailable(*_args: object, **_kwargs: object) -> None:
        raise DatabaseUnavailableError

    monkeypatch.setattr(webhooks, "store_issue_created", unavailable)

    status, logs = _post_and_capture(client, _body())

    assert status == 503
    [entry] = _responses(logs)
    assert (entry["eventId"], entry["httpStatus"], entry["decision"], entry["reason"]) == (
        EVENT_ID,
        503,
        "rejected",
        "unavailable",
    )


def test_event_id_is_bound_for_logs_inside_the_request(client: TestClient) -> None:
    # `occurred_at_fallback` é registrado durante a normalização e recebe o `eventId` do contexto.
    _, logs = _post_and_capture(client, _body(created_at="ontem"))

    fallback = [entry for entry in logs if entry["event"] == "occurred_at_fallback"]
    assert [entry["eventId"] for entry in fallback] == [EVENT_ID]


def test_event_id_does_not_leak_between_requests(client: TestClient) -> None:
    _post(client, _body())
    _, logs = _post_and_capture(client, _body(), delivery_id=None)

    [entry] = _responses(logs)
    assert "eventId" not in entry


def test_logs_never_contain_secret_raw_body_title_or_description(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    caplog.set_level(logging.DEBUG)
    body = _body()
    with structlog.testing.capture_logs(
        processors=[structlog.contextvars.merge_contextvars]
    ) as logs:
        _post(client, body)  # accepted
        _post(client, body, event="pull_request")  # ignored
        _post(client, _body(number=0))  # rejected (invalid_fields)
        _post(client, body, signature=_sign(body, "wrong-secret"))  # rejected (401)
        _post(client, _body(created_at="ontem"))  # occurred_at_fallback + accepted
        monkeypatch.setattr(
            webhooks,
            "store_issue_created",
            _returning(Duplicate(DuplicateReason.DUPLICATE_EVENT, "ACCEPTED")),
        )
        _post(client, body)  # duplicate

    assert {entry["decision"] for entry in _responses(logs)} == {
        "accepted",
        "duplicate",
        "ignored",
        "rejected",
    }
    out, err = capsys.readouterr()
    for text in (repr(logs), caplog.text, out, err):
        assert SECRET not in text
        assert TITLE not in text
        assert DESCRIPTION not in text
        assert body.decode() not in text
