"""Testes do filtro e da normalização do evento (RF02, RF03, RF04, RF05, RF28)."""

import copy
from datetime import UTC, datetime
from typing import Any

import pytest
import structlog

from issue_analyst_api.domain.events import (
    IgnoredReason,
    NormalizedIssueEvent,
    ValidationErrorCode,
    ValidationFailure,
    classify_event,
    normalize_issue_event,
)

MAX_TITLE = 256
MAX_DESCRIPTION = 500


def _payload(**issue_overrides: Any) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "number": 42,
        "title": "Erro ao salvar",
        "body": "Passos para reproduzir",
        "created_at": "2026-11-10T09:00:00-03:00",
        "updated_at": "2026-11-10T10:30:00-03:00",
    }
    issue.update(issue_overrides)
    return {
        "action": "opened",
        "issue": issue,
        "repository": {"full_name": "acme/app"},
        "sender": {"type": "User"},
    }


def _normalize(
    payload: dict[str, Any],
    *,
    delivery_id: str | None = "evt-001",
    event_type: str = "issue.created",
) -> NormalizedIssueEvent | ValidationFailure:
    return normalize_issue_event(
        delivery_id=delivery_id,
        event_type=event_type,  # type: ignore[arg-type]
        payload=payload,
        max_title_chars=MAX_TITLE,
        max_description_chars=MAX_DESCRIPTION,
    )


# --- Filtro ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("action", "expected"), [("opened", "issue.created"), ("edited", "issue.edited")]
)
def test_issues_opened_and_edited_pass_the_filter(action: str, expected: str) -> None:
    payload = _payload()
    payload["action"] = action

    assert classify_event("issues", payload) == expected


@pytest.mark.parametrize(
    ("github_event", "action", "sender_type", "reason"),
    [
        ("ping", None, None, IgnoredReason.PING),
        ("push", "opened", "User", IgnoredReason.UNSUPPORTED_EVENT),
        ("issue_comment", "created", "User", IgnoredReason.UNSUPPORTED_EVENT),
        (None, "opened", "User", IgnoredReason.UNSUPPORTED_EVENT),
        ("issues", "closed", "User", IgnoredReason.UNSUPPORTED_ACTION),
        ("issues", "labeled", "User", IgnoredReason.UNSUPPORTED_ACTION),
        ("issues", None, "User", IgnoredReason.UNSUPPORTED_ACTION),
        ("issues", "opened", "Bot", IgnoredReason.BOT_SENDER),
        ("issues", "edited", "Bot", IgnoredReason.BOT_SENDER),
    ],
)
def test_filter_ignores_with_reason(
    github_event: str | None, action: str | None, sender_type: str | None, reason: IgnoredReason
) -> None:
    payload: dict[str, Any] = {"sender": {"type": sender_type}}
    if action is not None:
        payload["action"] = action

    assert classify_event(github_event, payload) == reason


# --- Normalização ---------------------------------------------------------------------


def test_created_event_is_normalized() -> None:
    result = _normalize(_payload())

    assert isinstance(result, NormalizedIssueEvent)
    assert result.model_dump(by_alias=True) == {
        "eventId": "evt-001",
        "type": "issue.created",
        "occurredAt": datetime(2026, 11, 10, 12, 0, tzinfo=UTC),
        "repository": "acme/app",
        "issueNumber": 42,
        "title": "Erro ao salvar",
        "description": "Passos para reproduzir",
        "occurredAtFallback": None,
        "missingDescription": False,
    }


def test_edited_event_uses_updated_at_in_utc() -> None:
    result = _normalize(_payload(), event_type="issue.edited")

    assert isinstance(result, NormalizedIssueEvent)
    assert result.type == "issue.edited"
    assert result.occurred_at == datetime(2026, 11, 10, 13, 30, tzinfo=UTC)
    assert result.occurred_at.tzinfo == UTC


@pytest.mark.parametrize("body", [None, "", "   ", "\n\t "])
def test_empty_description_is_accepted_with_missing_description(body: str | None) -> None:
    result = _normalize(_payload(body=body))

    assert isinstance(result, NormalizedIssueEvent)
    assert result.description == ""
    assert result.missing_description is True


def test_missing_body_key_is_accepted_with_missing_description() -> None:
    payload = _payload()
    del payload["issue"]["body"]

    result = _normalize(payload)

    assert isinstance(result, NormalizedIssueEvent)
    assert result.missing_description is True


def test_repository_outside_catalog_is_accepted() -> None:
    payload = _payload()
    payload["repository"]["full_name"] = "someone-else/unknown-repo"

    result = _normalize(payload)

    assert isinstance(result, NormalizedIssueEvent)
    assert result.repository == "someone-else/unknown-repo"


def test_limits_are_inclusive() -> None:
    result = _normalize(_payload(title="t" * MAX_TITLE, body="d" * MAX_DESCRIPTION))

    assert isinstance(result, NormalizedIssueEvent)


def test_limits_count_characters_not_bytes() -> None:
    # "ç" e "ã" ocupam 2 bytes em UTF-8: em bytes os textos passariam dos limites.
    title = "ç" * MAX_TITLE
    description = "ã" * MAX_DESCRIPTION
    assert len(title.encode()) > MAX_TITLE
    assert len(description.encode()) > MAX_DESCRIPTION

    result = _normalize(_payload(title=title, body=description))

    assert isinstance(result, NormalizedIssueEvent)
    assert result.title == title
    assert result.description == description


def test_accented_text_above_character_limit_is_invalid() -> None:
    result = _normalize(_payload(title="é" * (MAX_TITLE + 1), body="õ" * (MAX_DESCRIPTION + 1)))

    assert result == ValidationFailure(ValidationErrorCode.INVALID_FIELDS, ["title", "description"])


def test_description_not_string_is_invalid() -> None:
    assert _normalize(_payload(body=["not", "text"])) == ValidationFailure(
        ValidationErrorCode.INVALID_FIELDS, ["description"]
    )


# --- Validação ------------------------------------------------------------------------


@pytest.mark.parametrize("delivery_id", [None, "", "   "])
def test_missing_delivery_id(delivery_id: str | None) -> None:
    assert _normalize(_payload(), delivery_id=delivery_id) == ValidationFailure(
        ValidationErrorCode.MISSING_EVENT_ID, ["eventId"]
    )


def _without(payload: dict[str, Any], *paths: tuple[str, str]) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    for parent, key in paths:
        del result[parent][key]
    return result


@pytest.mark.parametrize(
    ("payload", "fields"),
    [
        (_without(_payload(), ("repository", "full_name")), ["repository"]),
        (_without(_payload(), ("issue", "number")), ["issueNumber"]),
        (_without(_payload(), ("issue", "title")), ["title"]),
        (_payload(title=None), ["title"]),
        (_payload(title="   "), ["title"]),
        (_without(_payload(), ("issue", "number"), ("issue", "title")), ["issueNumber", "title"]),
        (
            _without(
                _payload(), ("repository", "full_name"), ("issue", "number"), ("issue", "title")
            ),
            ["repository", "issueNumber", "title"],
        ),
    ],
)
def test_missing_fields_lists_all_missing(payload: dict[str, Any], fields: list[str]) -> None:
    assert _normalize(payload) == ValidationFailure(ValidationErrorCode.MISSING_FIELDS, fields)


def test_missing_issue_and_repository_objects_list_all_missing() -> None:
    payload = {"action": "opened", "sender": {"type": "User"}}

    result = _normalize(payload)

    assert isinstance(result, ValidationFailure)
    assert result.error is ValidationErrorCode.MISSING_FIELDS
    assert result.fields[:3] == ["repository", "issueNumber", "title"]


@pytest.mark.parametrize("full_name", ["acme", "acme/", "/app", "acme/app/extra", "ac me/app", ""])
def test_repository_outside_owner_name_format(full_name: str) -> None:
    payload = _payload()
    payload["repository"]["full_name"] = full_name

    assert _normalize(payload) == ValidationFailure(
        ValidationErrorCode.INVALID_FIELDS, ["repository"]
    )


@pytest.mark.parametrize("number", [0, -1, 1.5, 7.0, "42", True, False])
def test_issue_number_not_integer_or_below_one(number: Any) -> None:
    assert _normalize(_payload(number=number)) == ValidationFailure(
        ValidationErrorCode.INVALID_FIELDS, ["issueNumber"]
    )


def test_title_above_limit() -> None:
    assert _normalize(_payload(title="t" * (MAX_TITLE + 1))) == ValidationFailure(
        ValidationErrorCode.INVALID_FIELDS, ["title"]
    )


def test_description_above_limit() -> None:
    assert _normalize(_payload(body="d" * (MAX_DESCRIPTION + 1))) == ValidationFailure(
        ValidationErrorCode.INVALID_FIELDS, ["description"]
    )


def test_invalid_fields_lists_all_invalid() -> None:
    payload = _payload(number=0, title="t" * (MAX_TITLE + 1), body="d" * (MAX_DESCRIPTION + 1))
    payload["repository"]["full_name"] = "acme"

    assert _normalize(payload) == ValidationFailure(
        ValidationErrorCode.INVALID_FIELDS, ["repository", "issueNumber", "title", "description"]
    )


def test_precedence_missing_event_id_over_missing_fields() -> None:
    payload = _without(_payload(), ("issue", "title"))

    result = _normalize(payload, delivery_id=None)

    assert isinstance(result, ValidationFailure)
    assert result.error is ValidationErrorCode.MISSING_EVENT_ID


def test_precedence_missing_fields_over_invalid_fields() -> None:
    payload = _without(_payload(number=0), ("issue", "title"))

    assert _normalize(payload) == ValidationFailure(ValidationErrorCode.MISSING_FIELDS, ["title"])


# --- Fallback de occurredAt (D12) -----------------------------------------------------


@pytest.mark.parametrize(
    "occurred_at", ["not-a-date", "2026-11-10T09:00:00", "2026-11-10", 12345, ["x"]]
)
def test_invalid_occurred_at_is_accepted_with_fallback(occurred_at: Any) -> None:
    with structlog.testing.capture_logs() as logs:
        result = _normalize(_payload(created_at=occurred_at))

    assert isinstance(result, NormalizedIssueEvent)
    assert result.occurred_at is None
    assert result.occurred_at_fallback == "invalid"
    assert logs == [
        {
            "event": "occurred_at_fallback",
            "log_level": "warning",
            "eventId": "evt-001",
            "reason": "invalid",
            "source": "created_at",
        }
    ]


@pytest.mark.parametrize(
    ("event_type", "source"), [("issue.created", "created_at"), ("issue.edited", "updated_at")]
)
def test_missing_occurred_at_is_accepted_with_fallback(event_type: str, source: str) -> None:
    payload = _without(_payload(), ("issue", source))

    with structlog.testing.capture_logs() as logs:
        result = _normalize(payload, event_type=event_type)

    assert isinstance(result, NormalizedIssueEvent)
    assert result.occurred_at is None
    assert result.occurred_at_fallback == "missing"
    assert [(log["event"], log["reason"], log["source"]) for log in logs] == [
        ("occurred_at_fallback", "missing", source)
    ]


def test_valid_occurred_at_has_no_fallback_and_no_warning() -> None:
    with structlog.testing.capture_logs() as logs:
        result = _normalize(_payload())

    assert isinstance(result, NormalizedIssueEvent)
    assert result.occurred_at_fallback is None
    assert logs == []


def test_occurred_at_never_appears_in_error_fields() -> None:
    payload = _without(_payload(number=0), ("issue", "title"), ("issue", "created_at"))

    assert _normalize(payload) == ValidationFailure(ValidationErrorCode.MISSING_FIELDS, ["title"])


def test_rejected_event_does_not_log_fallback() -> None:
    payload = _without(_payload(), ("issue", "title"), ("issue", "created_at"))

    with structlog.testing.capture_logs() as logs:
        _normalize(payload)

    assert logs == []
