"""Endpoint do webhook do GitHub (RF01, RF02)."""

import json

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from issue_analyst_api.config import Settings
from issue_analyst_api.domain.signature import is_valid_signature

# Limite do corpo em bytes, aplicado na leitura e antes da assinatura (1 MB).
MAX_BODY_BYTES = 1_048_576

router = APIRouter()


class PayloadTooLargeError(Exception):
    """Corpo da requisição acima de `MAX_BODY_BYTES`."""


async def read_body_limited(request: Request, limit: int = MAX_BODY_BYTES) -> bytes:
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
    """Recebe a entrega do GitHub: limite de tamanho → assinatura → JSON.

    Até a Etapa 5, uma entrega que passa por essas verificações responde `501` (provisório).
    """
    try:
        body = await read_body_limited(request)
    except PayloadTooLargeError:
        return JSONResponse(status_code=413, content={"error": "payload_too_large"})

    settings: Settings = request.app.state.settings
    secret = settings.GITHUB_WEBHOOK_SECRET.get_secret_value().encode()
    if not is_valid_signature(secret, body, request.headers.get("X-Hub-Signature-256")):
        return Response(status_code=401)

    try:
        json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse(status_code=400, content={"error": "invalid_payload", "fields": []})

    return Response(status_code=501)
