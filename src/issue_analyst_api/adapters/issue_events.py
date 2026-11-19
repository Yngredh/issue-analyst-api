"""Persistência transacional do evento aceito com a mensagem da outbox (RF07, RF28, RNF05, RNF07).

O evento e a mensagem da fila `analysis` são gravados na mesma transação: ou os dois ficam
gravados, ou nenhum. A publicação no broker é feita depois, pelo relay da outbox.
A entrada não lê o schema `triage`: quem decide se uma edição reabre a análise é o
`issue-triage`.
"""

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import psycopg
import structlog
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from issue_analyst_api.domain.events import NormalizedIssueEvent

ANALYSIS_TOPIC = "analysis"
INITIAL_ATTEMPT = 1
EDIT_TRIGGER = "author_edit"

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


# Consulta feita depois do conflito (D13): o `event_id` é verificado primeiro e só depois o
# `issue.created` da mesma issue. Com `ON CONFLICT DO NOTHING` o PostgreSQL espera a transação
# concorrente terminar e, em READ COMMITTED, cada comando vê o que já foi commitado: a consulta
# encontra a linha vencedora.
_SELECT_BY_EVENT_ID = """
SELECT status, body_hash FROM inbound.issue_events WHERE event_id = %(event_id)s
"""

_SELECT_CREATED_BY_ISSUE = """
SELECT status FROM inbound.issue_events
WHERE repository = %(repository)s AND issue_number = %(issue_number)s AND type = 'issue.created'
"""


# `issue.created` da mesma issue, lido na transação da edição para achar o `origin_event_id`.
_SELECT_ORIGIN_EVENT_ID = """
SELECT event_id FROM inbound.issue_events
WHERE repository = %(repository)s AND issue_number = %(issue_number)s AND type = 'issue.created'
"""


class DuplicateReason(StrEnum):
    """Qual chave de deduplicação conflitou (RF06)."""

    DUPLICATE_EVENT = "duplicate_event"
    DUPLICATE_ISSUE = "duplicate_issue"


@dataclass(frozen=True)
class Duplicate:
    """Nada foi inserido: chave que conflitou e estado atual da linha já gravada (D10, D13)."""

    reason: DuplicateReason
    state: str


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


def edit_message(origin_event_id: str, delivery_id: str) -> dict[str, Any]:
    """Payload da mensagem de edição da fila `analysis` (P1, "Contratos > Mensagem da fila")."""
    return {
        "eventId": origin_event_id,
        "attempt": None,
        "trigger": EDIT_TRIGGER,
        "triggerRef": delivery_id,
    }


async def _insert_outbox(
    conn: psycopg.AsyncConnection, event_id: str, dedup_key: str, payload: dict[str, Any]
) -> None:
    await conn.execute(
        _INSERT_OUTBOX,
        {
            "event_id": event_id,
            "topic": ANALYSIS_TOPIC,
            "dedup_key": dedup_key,
            "payload": Jsonb(payload),
        },
    )


async def _insert_created_outbox(conn: psycopg.AsyncConnection, event_id: str) -> None:
    await _insert_outbox(
        conn, event_id, f"{event_id}:{INITIAL_ATTEMPT}", analysis_message(event_id)
    )


async def _insert_edited_outbox(conn: psycopg.AsyncConnection, event: NormalizedIssueEvent) -> None:
    """Grava a mensagem de edição só se a issue já tem `issue.created`; senão, nada (P1)."""
    cursor = await conn.execute(
        _SELECT_ORIGIN_EVENT_ID,
        {"repository": event.repository, "issue_number": event.issue_number},
    )
    row = await cursor.fetchone()
    if row is None:
        return
    origin_event_id = row[0]
    await _insert_outbox(
        conn,
        origin_event_id,
        f"{origin_event_id}:edit:{event.event_id}",
        edit_message(origin_event_id, event.event_id),
    )


async def _find_conflict(
    conn: psycopg.AsyncConnection, event: NormalizedIssueEvent, received_body_hash: str
) -> Duplicate:
    """Descobre qual chave conflitou: `event_id` primeiro, depois a issue (D13)."""
    cursor = await conn.execute(_SELECT_BY_EVENT_ID, {"event_id": event.event_id})
    row = await cursor.fetchone()
    if row is not None:
        state, stored_body_hash = row
        if stored_body_hash != received_body_hash:
            # Mesmo `eventId` com conteúdo diferente: ganha o primeiro (D13).
            structlog.get_logger().warning(
                "duplicate_mismatch",
                event_id=event.event_id,
                storedBodyHash=stored_body_hash,
                receivedBodyHash=received_body_hash,
            )
        return Duplicate(DuplicateReason.DUPLICATE_EVENT, state)

    cursor = await conn.execute(
        _SELECT_CREATED_BY_ISSUE,
        {"repository": event.repository, "issue_number": event.issue_number},
    )
    row = await cursor.fetchone()
    if row is None:
        # Inalcançável: o insert só deixa de acontecer se uma das duas chaves conflitou.
        raise RuntimeError("conflicting issue event not found")
    return Duplicate(DuplicateReason.DUPLICATE_ISSUE, row[0])


async def store_issue_event(
    pool: AsyncConnectionPool,
    event: NormalizedIssueEvent,
    raw_payload: dict[str, Any],
    raw_body: bytes,
) -> Duplicate | None:
    """Grava o evento (`issue.created` ou `issue.edited`) e a mensagem da outbox numa única
    transação.

    `INSERT ... ON CONFLICT DO NOTHING` sem alvo: qualquer chave em conflito (mesmo `eventId` ou
    mesma issue) faz nada ser inserido, e decide sozinho quem é o primeiro em entregas
    simultâneas. A outbox só é gravada se o evento foi inserido: `issue.created` grava a
    mensagem inicial; `issue.edited` grava a mensagem de edição só se a issue já tem
    `issue.created` (sem ele, a edição é só registrada).
    Devolve `None` se o evento foi inserido; se nada foi inserido, devolve o `Duplicate` com a
    chave que conflitou e o estado da linha gravada, sem gravar outbox (RF06).
    Levanta `DatabaseUnavailableError` se o banco não estiver acessível.
    """
    received_body_hash = body_hash(raw_body)
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
                    "body_hash": received_body_hash,
                },
            )
            if await cursor.fetchone() is None:
                return await _find_conflict(conn, event, received_body_hash)
            if event.type == "issue.edited":
                await _insert_edited_outbox(conn, event)
            else:
                await _insert_created_outbox(conn, event.event_id)
            return None
    except psycopg.OperationalError as exc:  # inclui o PoolTimeout do psycopg_pool
        raise DatabaseUnavailableError from exc
