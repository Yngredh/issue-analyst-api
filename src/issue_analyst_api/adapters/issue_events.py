"""Persistência transacional do evento aceito com a mensagem da outbox (RF07, RNF05, RNF07).

O evento e a mensagem da fila `analysis` são gravados na mesma transação: ou os dois ficam
gravados, ou nenhum. A publicação no broker é feita depois, pelo relay da outbox.
"""

import hashlib
from typing import Any

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from issue_analyst_api.domain.events import NormalizedIssueEvent

ANALYSIS_TOPIC = "analysis"
INITIAL_ATTEMPT = 1

# `occurred_at` recebe o mesmo instante de `received_at` quando o normalizado vem sem
# `occurredAt` (fallback da D12): `now()` é fixo durante a transação.
_INSERT_EVENT = """
INSERT INTO inbound.issue_events
  (event_id, type, occurred_at, received_at, repository, issue_number, title, description,
   raw_payload, body_hash)
VALUES
  (%(event_id)s, %(type)s, COALESCE(%(occurred_at)s::timestamptz, now()), now(), %(repository)s,
   %(issue_number)s, %(title)s, %(description)s, %(raw_payload)s, %(body_hash)s)
ON CONFLICT DO NOTHING
RETURNING event_id
"""

_INSERT_OUTBOX = """
INSERT INTO inbound.inbound_outbox (event_id, topic, dedup_key, payload)
VALUES (%(event_id)s, %(topic)s, %(dedup_key)s, %(payload)s)
"""


class DatabaseUnavailableError(Exception):
    """Banco indisponível: nada foi gravado."""


def body_hash(raw_body: bytes) -> str:
    """SHA-256 (hex) do corpo bruto da requisição."""
    return hashlib.sha256(raw_body).hexdigest()


def analysis_message(event_id: str) -> dict[str, Any]:
    """Payload da mensagem inicial da fila `analysis` ("Contratos > Mensagem da fila")."""
    return {
        "eventId": event_id,
        "attempt": INITIAL_ATTEMPT,
        "trigger": "initial",
        "triggerRef": None,
    }


async def _insert_outbox(conn: psycopg.AsyncConnection, event_id: str) -> None:
    await conn.execute(
        _INSERT_OUTBOX,
        {
            "event_id": event_id,
            "topic": ANALYSIS_TOPIC,
            "dedup_key": f"{event_id}:{INITIAL_ATTEMPT}",
            "payload": Jsonb(analysis_message(event_id)),
        },
    )


async def store_issue_created(
    pool: AsyncConnectionPool,
    event: NormalizedIssueEvent,
    raw_payload: dict[str, Any],
    raw_body: bytes,
) -> bool:
    """Grava o `issue.created` e a mensagem da outbox numa única transação.

    `INSERT ... ON CONFLICT DO NOTHING` sem alvo: qualquer chave em conflito (mesmo `eventId` ou
    mesma issue) faz nada ser inserido. A outbox só é gravada se o evento foi inserido.
    Devolve `True` se o evento foi inserido e `False` se já existia.
    Levanta `DatabaseUnavailableError` se o banco não estiver acessível.
    """
    try:
        async with pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(
                _INSERT_EVENT,
                {
                    "event_id": event.event_id,
                    "type": event.type,
                    "occurred_at": (
                        None if event.occurred_at_fallback is not None else event.occurred_at
                    ),
                    "repository": event.repository,
                    "issue_number": event.issue_number,
                    "title": event.title,
                    "description": event.description,
                    "raw_payload": Jsonb(raw_payload),
                    "body_hash": body_hash(raw_body),
                },
            )
            if await cursor.fetchone() is None:
                return False
            await _insert_outbox(conn, event.event_id)
            return True
    except psycopg.OperationalError as exc:  # inclui o PoolTimeout do psycopg_pool
        raise DatabaseUnavailableError from exc
