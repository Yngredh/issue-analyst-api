"""Script de envio de eventos (`scripts/send_event.py`), sem rede."""

import json
from datetime import UTC, datetime

import pytest

from issue_analyst_api.domain.events import (
    EventType,
    NormalizedIssueEvent,
    classify_event,
    normalize_issue_event,
)
from issue_analyst_api.domain.signature import is_valid_signature
from tests.conftest import load_send_event_script

send_event = load_send_event_script()
NOW = datetime(2026, 11, 18, 12, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("action", "expected_type"),
    [("opened", "issue.created"), ("edited", "issue.edited")],
)
def test_payload_is_accepted_by_the_service_rules(action: str, expected_type: EventType) -> None:
    payload = send_event.build_payload(
        action, "equipe/projeto-teste", 42, "Erro ao salvar", "Passos", now=NOW
    )

    assert classify_event("issues", payload) == expected_type
    normalized = normalize_issue_event(
        delivery_id="evt-001",
        event_type=expected_type,
        payload=payload,
        max_title_chars=256,
        max_description_chars=500,
    )
    assert isinstance(normalized, NormalizedIssueEvent)
    assert normalized.repository == "equipe/projeto-teste"
    assert normalized.issue_number == 42
    assert normalized.title == "Erro ao salvar"
    assert normalized.description == "Passos"
    assert normalized.occurred_at == NOW


def test_omitted_description_is_sent_as_null() -> None:
    payload = send_event.build_payload("opened", "equipe/projeto-teste", 1, "Titulo", None)

    assert payload["issue"]["body"] is None
    assert payload["sender"]["type"] == "User"


def test_request_has_github_headers_and_valid_signature() -> None:
    payload = send_event.build_payload("opened", "equipe/projeto-teste", 42, "T", "D", now=NOW)

    request = send_event.build_request("http://svc/webhooks/github", "s3cr3t", "evt-9", payload)

    assert request.get_method() == "POST"
    assert request.full_url == "http://svc/webhooks/github"
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers["x-github-event"] == "issues"
    assert headers["x-github-delivery"] == "evt-9"
    assert headers["content-type"] == "application/json"
    assert json.loads(request.data) == payload
    assert is_valid_signature(b"s3cr3t", request.data, headers["x-hub-signature-256"])


def test_missing_secret_aborts_without_sending(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET", raising=False)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("must not send")

    monkeypatch.setattr(send_event, "send", fail)
    argv = ["--action", "opened", "--repository", "a/b", "--number", "1", "--title", "T"]

    assert send_event.main([*argv, "--delivery-id", "evt-1"]) == 2
    assert "GITHUB_WEBHOOK_SECRET" in capsys.readouterr().err
