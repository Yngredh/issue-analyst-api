"""Filtro de eventos do GitHub e normalização da issue (RF02, RF03, RF04, RF05, RF28).

A validação recusa só o que impede identificar a issue. Descrição vazia não é erro:
o evento é aceito com `missingDescription = true`. A entrada não consulta o catálogo.
`occurredAt` ausente ou inválido também não recusa o evento (decisão D12): fica `None`, com o
motivo em `occurredAtFallback`, e a persistência grava o mesmo instante de `received_at`.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

import structlog
from pydantic import BaseModel, ConfigDict, computed_field
from pydantic.alias_generators import to_camel

EventType = Literal["issue.created", "issue.edited"]
OccurredAtFallback = Literal["missing", "invalid"]

_ACTION_TO_TYPE: dict[str, EventType] = {
    "opened": "issue.created",
    "edited": "issue.edited",
}

# `owner/name`: exatamente uma barra, as duas partes não vazias e sem espaços.
_REPOSITORY_FORMAT = re.compile(r"[^/\s]+/[^/\s]+")


class IgnoredReason(StrEnum):
    """Motivos de `200 ignored` (decisão D7)."""

    PING = "ping"
    UNSUPPORTED_EVENT = "unsupported_event"
    UNSUPPORTED_ACTION = "unsupported_action"
    BOT_SENDER = "bot_sender"


class ValidationErrorCode(StrEnum):
    """Códigos de `400`, em ordem de precedência (decisão D8)."""

    INVALID_PAYLOAD = "invalid_payload"
    MISSING_EVENT_ID = "missing_event_id"
    MISSING_FIELDS = "missing_fields"
    INVALID_FIELDS = "invalid_fields"


@dataclass(frozen=True)
class ValidationFailure:
    """Recusa da entrada: código e campos (no nome normalizado) com problema."""

    error: ValidationErrorCode
    fields: list[str] = field(default_factory=list)

    def to_body(self) -> dict[str, Any]:
        """Corpo da resposta `400` (decisão D8)."""
        return {"error": self.error.value, "fields": list(self.fields)}


class NormalizedIssueEvent(BaseModel):
    """Evento de issue normalizado, conforme "Contratos > Normalização" do plano."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, frozen=True)

    event_id: str
    type: EventType
    occurred_at: datetime | None
    occurred_at_fallback: OccurredAtFallback | None = None
    repository: str
    issue_number: int
    title: str
    description: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def missing_description(self) -> bool:
        """Descrição vazia: derivada de `description == ""` (ponto em aberto P3)."""
        return self.description == ""


def classify_event(github_event: str | None, payload: dict[str, Any]) -> EventType | IgnoredReason:
    """Filtra a entrega: devolve o tipo normalizado ou o motivo para ignorá-la.

    Só `issues` com `opened` ou `edited` segue; os motivos são avaliados na ordem de D7.
    """
    if github_event == "ping":
        return IgnoredReason.PING
    if github_event != "issues":
        return IgnoredReason.UNSUPPORTED_EVENT
    event_type = _ACTION_TO_TYPE.get(payload.get("action"))  # type: ignore[arg-type]
    if event_type is None:
        return IgnoredReason.UNSUPPORTED_ACTION
    sender = payload.get("sender")
    if isinstance(sender, dict) and sender.get("type") == "Bot":
        return IgnoredReason.BOT_SENDER
    return event_type


def _parse_timestamp(value: Any) -> datetime | None:
    """Interpreta um timestamp ISO 8601 com fuso e converte para UTC; senão `None`.

    Data sem fuso conta como inválida: não se assume UTC (D12).
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def normalize_issue_event(
    *,
    delivery_id: str | None,
    event_type: EventType,
    payload: dict[str, Any],
    max_title_chars: int,
    max_description_chars: int,
) -> NormalizedIssueEvent | ValidationFailure:
    """Mapeia o payload do GitHub para o evento normalizado e valida os campos.

    Precedência (D8): `missing_event_id` > `missing_fields` > `invalid_fields`.
    `missing_fields` e `invalid_fields` listam todos os campos da categoria, na ordem
    repository, issueNumber, title, description (D12). Limites são inclusivos e contam caracteres.
    """
    if delivery_id is None or delivery_id.strip() == "":
        return ValidationFailure(ValidationErrorCode.MISSING_EVENT_ID, ["eventId"])

    missing: list[str] = []
    invalid: list[str] = []

    repository_obj = payload.get("repository")
    issue_obj = payload.get("issue")
    repository_data = repository_obj if isinstance(repository_obj, dict) else {}
    issue = issue_obj if isinstance(issue_obj, dict) else {}

    repository = repository_data.get("full_name")
    if repository is None:
        missing.append("repository")
    elif not isinstance(repository, str) or _REPOSITORY_FORMAT.fullmatch(repository) is None:
        invalid.append("repository")

    issue_number = issue.get("number")
    if issue_number is None:
        missing.append("issueNumber")
    elif type(issue_number) is not int or issue_number < 1:
        invalid.append("issueNumber")

    title = issue.get("title")
    if title is None or (isinstance(title, str) and title.strip() == ""):
        missing.append("title")
    elif not isinstance(title, str) or len(title) > max_title_chars:
        invalid.append("title")

    raw_description = issue.get("body")
    description = ""
    if raw_description is not None:
        if not isinstance(raw_description, str) or len(raw_description) > max_description_chars:
            invalid.append("description")
        elif raw_description.strip() != "":
            description = raw_description

    occurred_source = "created_at" if event_type == "issue.created" else "updated_at"
    raw_occurred_at = issue.get(occurred_source)
    occurred_at = _parse_timestamp(raw_occurred_at)
    fallback: OccurredAtFallback | None = None
    if raw_occurred_at is None:
        fallback = "missing"
    elif occurred_at is None:
        fallback = "invalid"

    if missing:
        return ValidationFailure(ValidationErrorCode.MISSING_FIELDS, missing)
    if invalid:
        return ValidationFailure(ValidationErrorCode.INVALID_FIELDS, invalid)

    if fallback is not None:
        structlog.get_logger().warning(
            "occurred_at_fallback", eventId=delivery_id, reason=fallback, source=occurred_source
        )

    return NormalizedIssueEvent(
        event_id=delivery_id,
        type=event_type,
        occurred_at=occurred_at,
        occurred_at_fallback=fallback,
        repository=repository,
        issue_number=issue_number,
        title=title,
        description=description,
    )
