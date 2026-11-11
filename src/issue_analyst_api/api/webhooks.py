"""Endpoint do webhook do GitHub (RF01, RF02, RF03, RF04, RF05, RF28)."""

import json

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

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


class PayloadTooLargeError(Exception):
    """Corpo da requisição acima do limite configurado (`WEBHOOK_MAX_BODY_BYTES`)."""


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
    """Recebe a entrega do GitHub: limite de tamanho → assinatura → JSON → filtro → normalização.

    Até a Etapa 5, uma entrega válida responde `501` (provisório).
    """
    settings: Settings = request.app.state.settings
    try:
        body = await read_body_limited(request, settings.WEBHOOK_MAX_BODY_BYTES)
    except PayloadTooLargeError:
        return JSONResponse(status_code=413, content={"error": "payload_too_large"})

    secret = settings.GITHUB_WEBHOOK_SECRET.get_secret_value().encode()
    if not is_valid_signature(secret, body, request.headers.get("X-Hub-Signature-256")):
        return Response(status_code=401)

    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    if not isinstance(payload, dict):
        failure = ValidationFailure(ValidationErrorCode.INVALID_PAYLOAD)
        return JSONResponse(status_code=400, content=failure.to_body())

    classification = classify_event(request.headers.get("X-GitHub-Event"), payload)
    if isinstance(classification, IgnoredReason):
        return JSONResponse(
            status_code=200, content={"status": "ignored", "reason": classification.value}
        )

    result = normalize_issue_event(
        delivery_id=request.headers.get("X-GitHub-Delivery"),
        event_type=classification,
        payload=payload,
        max_title_chars=settings.MAX_TITLE_CHARS,
        max_description_chars=settings.MAX_DESCRIPTION_CHARS,
    )
    if isinstance(result, ValidationFailure):
        return JSONResponse(status_code=400, content=result.to_body())

    return Response(status_code=501)
