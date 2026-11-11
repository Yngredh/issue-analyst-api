"""Cada linha da tabela de Validação da Entrada de issues, pelo endpoint (RF02 a RF05, RF28).

A gravação no banco é substituída por um falso; a persistência real é coberta pelos testes de
integração. `issue.edited` válido ainda responde o `501` provisório (até a Etapa 9).
"""

import hashlib
import hmac
import json
from collections.abc import Iterator
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from issue_analyst_api.api import webhooks
from issue_analyst_api.api.app import create_app
from issue_analyst_api.config import Settings
from issue_analyst_api.domain.events import NormalizedIssueEvent

SECRET = "validation-table-secret"
URL = "/webhooks/github"


def _payload(action: str = "opened", sender_type: str = "User", **issue: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "number": 7,
        "title": "Erro ao salvar",
        "body": "Passos para reproduzir",
        "created_at": "2026-11-10T12:00:00Z",
        "updated_at": "2026-11-10T13:00:00Z",
    }
    base.update(issue)
    return {
        "action": action,
        "issue": base,
        "repository": {"full_name": "acme/app"},
        "sender": {"type": sender_type},
    }


@pytest.fixture(autouse=True)
def _fake_store(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_store(*_args: object, **_kwargs: object) -> bool:
        return True

    monkeypatch.setattr(webhooks, "store_issue_created", fake_store)


@pytest.fixture
def client() -> Iterator[TestClient]:
    settings = Settings(
        GITHUB_WEBHOOK_SECRET=SecretStr(SECRET),
        DATABASE_URL="postgresql://nobody:nothing@127.0.0.1:1/none",
        RABBITMQ_URL="amqp://nobody:nothing@127.0.0.1:1/",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _post(
    client: TestClient,
    payload: Any,
    *,
    event: str | None = "issues",
    delivery: str | None = "evt-001",
    signed: bool = True,
    raw: bytes | None = None,
) -> httpx.Response:
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signed:
        digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
        headers["X-Hub-Signature-256"] = "sha256=" + digest
    if event is not None:
        headers["X-GitHub-Event"] = event
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    return client.post(URL, content=body, headers=headers)


def test_signature_missing_or_invalid_returns_401(client: TestClient) -> None:
    assert _post(client, _payload(), signed=False).status_code == 401


def test_body_not_valid_json_returns_400_invalid_payload(client: TestClient) -> None:
    response = _post(client, None, raw=b"{not json")

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_payload", "fields": []}


@pytest.mark.parametrize(
    ("event", "payload", "reason"),
    [
        ("ping", {"zen": "Keep it simple."}, "ping"),
        ("push", {"ref": "refs/heads/main"}, "unsupported_event"),
        ("issues", _payload(action="closed"), "unsupported_action"),
        ("issues", _payload(action="opened", sender_type="Bot"), "bot_sender"),
        ("issues", _payload(action="edited", sender_type="Bot"), "bot_sender"),
    ],
)
def test_unsupported_event_ping_or_bot_returns_200_ignored(
    client: TestClient, event: str, payload: dict[str, Any], reason: str
) -> None:
    response = _post(client, payload, event=event)

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": reason}


def test_ping_without_delivery_is_still_ignored(client: TestClient) -> None:
    # Consequência da ordem da D7/D12: o filtro roda antes da exigência de X-GitHub-Delivery.
    response = _post(client, {"zen": "x"}, event="ping", delivery=None)

    assert response.status_code == 200
    assert response.json() == {"status": "ignored", "reason": "ping"}


def test_non_object_json_returns_400_invalid_payload_before_filter(client: TestClient) -> None:
    response = _post(client, ["not", "an", "object"], event="ping")

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_payload", "fields": []}


def test_missing_delivery_returns_400_missing_event_id(client: TestClient) -> None:
    response = _post(client, _payload(), delivery=None)

    assert response.status_code == 400
    assert response.json() == {"error": "missing_event_id", "fields": ["eventId"]}


def test_missing_repository_number_and_title_lists_all(client: TestClient) -> None:
    payload = _payload()
    del payload["repository"]["full_name"]
    del payload["issue"]["number"]
    del payload["issue"]["title"]

    response = _post(client, payload)

    assert response.status_code == 400
    assert response.json() == {
        "error": "missing_fields",
        "fields": ["repository", "issueNumber", "title"],
    }


def test_repository_outside_owner_name_returns_400_invalid_fields(client: TestClient) -> None:
    payload = _payload()
    payload["repository"]["full_name"] = "only-owner"

    response = _post(client, payload)

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_fields", "fields": ["repository"]}


@pytest.mark.parametrize("number", [0, -3, "7", 7.5, 7.0, True])
def test_issue_number_not_integer_or_below_one_returns_400_invalid_fields(
    client: TestClient, number: Any
) -> None:
    response = _post(client, _payload(number=number))

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_fields", "fields": ["issueNumber"]}


def test_title_only_spaces_returns_400_missing_fields(client: TestClient) -> None:
    response = _post(client, _payload(title="    "))

    assert response.status_code == 400
    assert response.json() == {"error": "missing_fields", "fields": ["title"]}


def _accepted_event(client: TestClient, payload: dict[str, Any], **kwargs: Any) -> Any:
    """Envia e devolve o evento normalizado que chegou ao fim da validação."""
    captured: list[Any] = []
    original = webhooks.normalize_issue_event

    def spy(**call_kwargs: Any) -> Any:
        result = original(**call_kwargs)
        captured.append(result)
        return result

    with patch.object(webhooks, "normalize_issue_event", spy):
        response = _post(client, payload, **kwargs)

    assert len(captured) == 1
    assert isinstance(captured[0], NormalizedIssueEvent)
    expected_status = 202 if captured[0].type == "issue.created" else 501  # 501 até a Etapa 9
    assert response.status_code == expected_status
    return captured[0]


@pytest.mark.parametrize("body", [None, "", "   "])
def test_description_empty_null_or_spaces_is_accepted_with_missing_description(
    client: TestClient, body: str | None
) -> None:
    event = _accepted_event(client, _payload(body=body))

    assert event.description == ""
    assert event.missing_description is True


def test_repository_outside_catalog_is_accepted_without_catalog_lookup(
    client: TestClient,
) -> None:
    payload = _payload()
    payload["repository"]["full_name"] = "not-in-catalog/whatever"

    event = _accepted_event(client, payload)

    assert event.repository == "not-in-catalog/whatever"


def test_edited_issue_is_accepted_as_issue_edited(client: TestClient) -> None:
    event = _accepted_event(client, _payload(action="edited"))

    assert event.type == "issue.edited"


@pytest.mark.parametrize("created_at", [None, "2026-11-10T12:00:00", "ontem"])
def test_occurred_at_missing_or_invalid_is_accepted_with_fallback(
    client: TestClient, created_at: str | None
) -> None:
    payload = _payload(created_at=created_at)
    if created_at is None:
        del payload["issue"]["created_at"]

    event = _accepted_event(client, payload)

    assert event.occurred_at is None
    assert event.occurred_at_fallback == ("missing" if created_at is None else "invalid")


def test_title_above_256_returns_400_invalid_fields(client: TestClient) -> None:
    response = _post(client, _payload(title="t" * 257))

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_fields", "fields": ["title"]}


def test_description_above_500_returns_400_invalid_fields(client: TestClient) -> None:
    response = _post(client, _payload(body="d" * 501))

    assert response.status_code == 400
    assert response.json() == {"error": "invalid_fields", "fields": ["description"]}


def test_body_above_1mb_returns_413(client: TestClient) -> None:
    response = _post(client, None, raw=b"x" * (1_048_576 + 1), signed=False)

    assert response.status_code == 413
    assert response.json() == {"error": "payload_too_large"}
