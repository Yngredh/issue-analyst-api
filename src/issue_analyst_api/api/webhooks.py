"""Endpoint do webhook do GitHub (RF01, RF02, RF03, RF04, RF05, RF28, RNF14)."""

import json
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import structlog
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from issue_analyst_api.adapters.issue_events import (
    DatabaseUnavailableError,
    store_issue_event,
)
from issue_analyst_api.config import Settings
from issue_analyst_api.domain.events import (
    IgnoredReason,
    ValidationErrorCode,
    ValidationFailure,
    classify_event,
    normalize_issue_event,
)
from issue_analyst_api.domain.signature import is_valid_signature

router = APIRouter()

# Chave do `eventId` no contexto do structlog (id de correlação da entrega).
EVENT_ID_KEY = "eventId"


class PayloadTooLargeError(Exception):
    """Corpo da requisição acima do limite configurado (`WEBHOOK_MAX_BODY_BYTES`)."""


class Decision(StrEnum):
    """Decisão registrada no log de cada resposta do webhook (RNF14)."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    IGNORED = "ignored"
    REJECTED = "rejected"


@dataclass(frozen=True)
class _Outcome:
    """Resposta do webhook junto com o que vai para o log de decisão."""

    response: Response
    decision: Decision
    reason: str | None = None
    log_fields: dict[str, Any] = field(default_factory=dict)


async def read_body_limited(request: Request, limit: int) -> bytes:
    """Lê o corpo bruto em streaming e interrompe assim que passa do limite."""
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise PayloadTooLargeError
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/webhooks/github")
async def receive_github_webhook(request: Request) -> Response:
    """Recebe a entrega do GitHub: limite de tamanho → assinatura → JSON → filtro → normalização
    → transação (evento + outbox).

    `issue.created` ou `issue.edited` novo → `202 accepted`; já recebido → `200 duplicate`;
    banco fora → `503`.

    Cada resposta gera um único log `webhook_response` com o código HTTP, a decisão e o motivo.
    O `eventId` fica no contexto do structlog durante a requisição e sai dele no fim.
    """
    try:
        outcome = await _process(request)
        _log_outcome(outcome)
        return outcome.response
    finally:
        structlog.contextvars.unbind_contextvars(EVENT_ID_KEY)


def _log_outcome(outcome: _Outcome) -> None:
    """Registra a decisão da resposta, sem segredo, corpo bruto, título ou descrição."""
    log = structlog.get_logger()
    level = log.warning if outcome.decision is Decision.REJECTED else log.info
    level(
        "webhook_response",
        httpStatus=outcome.response.status_code,
        decision=outcome.decision.value,
        reason=outcome.reason,
        **outcome.log_fields,
    )


def _rejected(status_code: int, reason: str, content: dict[str, Any] | None = None) -> _Outcome:
    response = (
        Response(status_code=status_code)
        if content is None
        else JSONResponse(status_code=status_code, content=content)
    )
    return _Outcome(response, Decision.REJECTED, reason)


async def _process(request: Request) -> _Outcome:
    settings: Settings = request.app.state.settings
    try:
        body = await read_body_limited(request, settings.WEBHOOK_MAX_BODY_BYTES)
    except PayloadTooLargeError:
        return _rejected(413, "payload_too_large", {"error": "payload_too_large"})

    secret = settings.GITHUB_WEBHOOK_SECRET.get_secret_value().encode()
    if not is_valid_signature(secret, body, request.headers.get("X-Hub-Signature-256")):
        return _rejected(401, "invalid_signature")

    # Só depois da assinatura conferida: o `eventId` não vem de quem não tem o segredo.
    delivery_id = request.headers.get("X-GitHub-Delivery")
    if delivery_id:
        structlog.contextvars.bind_contextvars(**{EVENT_ID_KEY: delivery_id})

    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        return _validation_rejected(ValidationFailure(ValidationErrorCode.INVALID_PAYLOAD))

    classification = classify_event(request.headers.get("X-GitHub-Event"), payload)
    if isinstance(classification, IgnoredReason):
        return _Outcome(
            JSONResponse(
                status_code=200, content={"status": "ignored", "reason": classification.value}
            ),
            Decision.IGNORED,
            classification.value,
        )

    result = normalize_issue_event(
        delivery_id=delivery_id,
        event_type=classification,
        payload=payload,
        max_title_chars=settings.MAX_TITLE_CHARS,
        max_description_chars=settings.MAX_DESCRIPTION_CHARS,
    )
    if isinstance(result, ValidationFailure):
        return _validation_rejected(result)

    try:
        duplicate = await store_issue_event(request.app.state.db_pool, result, payload, body)
    except DatabaseUnavailableError:
        return _rejected(503, "unavailable", {"error": "unavailable"})

    if duplicate is not None:
        # `eventId` é o da entrega recebida; `state` vem da linha já gravada (D13).
        return _Outcome(
            JSONResponse(
                status_code=200,
                content={
                    "status": "duplicate",
                    "reason": duplicate.reason.value,
                    "eventId": result.event_id,
                    "state": duplicate.state,
                },
            ),
            Decision.DUPLICATE,
            duplicate.reason.value,
            {"state": duplicate.state},
        )

    # Resposta só depois do commit: 202 significa "aceito e guardado", não "analisado".
    return _Outcome(
        JSONResponse(
            status_code=202,
            content={
                "status": "accepted",
                "eventId": result.event_id,
                "links": {"result": f"/issues/{result.repository}/{result.issue_number}"},
            },
        ),
        Decision.ACCEPTED,
        log_fields={
            "type": result.type,
            "repository": result.repository,
            "issueNumber": result.issue_number,
            "missingDescription": result.description == "",
        },
    )


def _validation_rejected(failure: ValidationFailure) -> _Outcome:
    """400 da validação; o log leva o código e os nomes dos campos, nunca os valores."""
    body = failure.to_body()
    return _Outcome(
        JSONResponse(status_code=400, content=body),
        Decision.REJECTED,
        body["error"],
        {"fields": body["fields"]},
    )
