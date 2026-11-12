"""Deduplicação por `eventId` e por issue (RF06, D10, D13)."""

import asyncio
import hashlib
import hmac
import json
import random
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
import pytest
import structlog
from fastapi.testclient import TestClient
from pydantic import SecretStr

from issue_analyst_api.adapters.database import close_pool, create_pool, open_pool
from issue_analyst_api.adapters.issue_events import (
    Duplicate,
    DuplicateReason,
    store_issue_created,
)
from issue_analyst_api.api.app import create_app
from issue_analyst_api.config import Settings
from issue_analyst_api.domain.events import NormalizedIssueEvent

SECRET = "dedup-test-secret"
URL = "/webhooks/github"


def _settings(database_url: str) -> Settings:
    return Settings(
        GITHUB_WEBHOOK_SECRET=SecretStr(SECRET),
        DATABASE_URL=database_url,
        RABBITMQ_URL="amqp://nobody:nothing@127.0.0.1:1/",
    )


def _unique_issue() -> tuple[str, int]:
    """Repositório e número únicos: o banco é compartilhado pela sessão e o app faz commit."""
    return f"acme/repo-{uuid.uuid4().hex[:8]}", random.randint(1, 1_000_000)


def _new_event_id() -> str:
    return f"evt-{uuid.uuid4()}"


def _body(repository: str, number: int, **issue: Any) -> bytes:
    data: dict[str, Any] = {
        "number": number,
        "title": "Erro ao salvar",
        "body": "Passos para reproduzir",
        "created_at": "2026-11-10T12:00:00Z",
    }
    data.update(issue)
    payload = {
        "action": "opened",
        "issue": data,
        "repository": {"full_name": repository},
        "sender": {"type": "User", "login": "alguem"},
    }
    return json.dumps(payload).encode()


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _post(
    client: TestClient, body: bytes, event_id: str, *, signature: str | None = None
) -> httpx.Response:
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": signature if signature is not None else _sign(body),
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": event_id,
    }
    return client.post(URL, content=body, headers=headers)


def _count(conn: psycopg.Connection, table: str, event_id: str) -> int:
    row = conn.execute(
        f"SELECT count(*) FROM inbound.{table} WHERE event_id = %s",  # noqa: S608 - nome fixo
        (event_id,),
    ).fetchone()
    assert row is not None
    return row[0]


def _stored_title(conn: psycopg.Connection, event_id: str) -> str:
    row = conn.execute(
        "SELECT title FROM inbound.issue_events WHERE event_id = %s", (event_id,)
    ).fetchone()
    assert row is not None
    return row[0]


def _mismatch_logs(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in logs if entry["event"] == "duplicate_mismatch"]


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(database_url))) as test_client:
        yield test_client


def test_same_event_id_resent_is_duplicate_event_without_new_outbox_row(
    client: TestClient, database_url: str
) -> None:
    repository, number = _unique_issue()
    event_id = _new_event_id()
    body = _body(repository, number)

    assert _post(client, body, event_id).status_code == 202
    with structlog.testing.capture_logs() as logs:
        response = _post(client, body, event_id)

    assert response.status_code == 200
    assert response.json() == {
        "status": "duplicate",
        "reason": "duplicate_event",
        "eventId": event_id,
        "state": "ACCEPTED",
    }
    assert _mismatch_logs(logs) == []  # mesmo corpo: sem duplicate_mismatch
    with psycopg.connect(database_url) as conn:
        assert _count(conn, "issue_events", event_id) == 1
        assert _count(conn, "inbound_outbox", event_id) == 1


def test_second_issue_created_with_other_event_id_is_duplicate_issue(
    client: TestClient, database_url: str
) -> None:
    repository, number = _unique_issue()
    first_id, second_id = _new_event_id(), _new_event_id()

    assert _post(client, _body(repository, number), first_id).status_code == 202
    response = _post(client, _body(repository, number, title="Outro título"), second_id)

    assert response.status_code == 200
    # `eventId` é o da entrega que acabou de chegar; `state` vem da linha já gravada (D13).
    assert response.json() == {
        "status": "duplicate",
        "reason": "duplicate_issue",
        "eventId": second_id,
        "state": "ACCEPTED",
    }
    with psycopg.connect(database_url) as conn:
        assert _count(conn, "issue_events", second_id) == 0
        assert _count(conn, "inbound_outbox", second_id) == 0
        assert _count(conn, "inbound_outbox", first_id) == 1


def test_same_event_id_and_same_issue_is_duplicate_event(
    client: TestClient, database_url: str
) -> None:
    repository, number = _unique_issue()
    event_id = _new_event_id()

    assert _post(client, _body(repository, number), event_id).status_code == 202
    response = _post(client, _body(repository, number, title="Mudou"), event_id)

    assert response.status_code == 200
    assert response.json()["reason"] == "duplicate_event"


def test_same_event_id_with_different_body_keeps_first_and_logs_mismatch(
    client: TestClient, database_url: str
) -> None:
    first_repo, first_number = _unique_issue()
    other_repo, other_number = _unique_issue()
    event_id = _new_event_id()
    first_body = _body(first_repo, first_number, title="Primeiro")
    second_body = _body(other_repo, other_number, title="Segundo")

    assert _post(client, first_body, event_id).status_code == 202
    with structlog.testing.capture_logs() as logs:
        response = _post(client, second_body, event_id)

    assert response.status_code == 200
    assert response.json()["reason"] == "duplicate_event"
    assert _mismatch_logs(logs) == [
        {
            "event": "duplicate_mismatch",
            "log_level": "warning",
            "eventId": event_id,
            "storedBodyHash": hashlib.sha256(first_body).hexdigest(),
            "receivedBodyHash": hashlib.sha256(second_body).hexdigest(),
        }
    ]
    with psycopg.connect(database_url) as conn:
        assert _stored_title(conn, event_id) == "Primeiro"  # ganha o primeiro
        assert _count(conn, "inbound_outbox", event_id) == 1


def test_duplicate_issue_does_not_log_mismatch(client: TestClient) -> None:
    repository, number = _unique_issue()

    assert _post(client, _body(repository, number), _new_event_id()).status_code == 202
    with structlog.testing.capture_logs() as logs:
        response = _post(client, _body(repository, number, title="Outro"), _new_event_id())

    assert response.json()["reason"] == "duplicate_issue"
    assert _mismatch_logs(logs) == []


def test_event_rejected_with_400_does_not_take_the_key(
    client: TestClient, database_url: str
) -> None:
    repository, number = _unique_issue()
    event_id = _new_event_id()

    rejected = _post(client, _body(repository, number, title="   "), event_id)
    assert rejected.status_code == 400

    accepted = _post(client, _body(repository, number), event_id)
    assert accepted.status_code == 202
    with psycopg.connect(database_url) as conn:
        assert _count(conn, "issue_events", event_id) == 1
        assert _count(conn, "inbound_outbox", event_id) == 1


def test_event_rejected_with_401_does_not_take_the_key(
    client: TestClient, database_url: str
) -> None:
    repository, number = _unique_issue()
    event_id = _new_event_id()
    body = _body(repository, number)

    rejected = _post(client, body, event_id, signature="sha256=" + "0" * 64)
    assert rejected.status_code == 401

    assert _post(client, body, event_id).status_code == 202
    with psycopg.connect(database_url) as conn:
        assert _count(conn, "issue_events", event_id) == 1


async def test_concurrent_deliveries_of_same_event_id_store_one_row_and_one_message(
    database_url: str,
) -> None:
    repository, number = _unique_issue()
    event_id = _new_event_id()
    event = NormalizedIssueEvent(
        event_id=event_id,
        type="issue.created",
        occurred_at=datetime(2026, 11, 10, 12, 0, tzinfo=UTC),
        repository=repository,
        issue_number=number,
        title="Erro ao salvar",
        description="",
    )
    raw_body = _body(repository, number)
    raw_payload = json.loads(raw_body)
    pool = create_pool(database_url)
    await open_pool(pool)
    try:
        results = await asyncio.gather(
            *(store_issue_created(pool, event, raw_payload, raw_body) for _ in range(8))
        )
    finally:
        await close_pool(pool)

    assert results.count(None) == 1
    duplicates = [result for result in results if result is not None]
    assert duplicates == [Duplicate(DuplicateReason.DUPLICATE_EVENT, "ACCEPTED")] * 7
    with psycopg.connect(database_url) as conn:
        assert _count(conn, "issue_events", event_id) == 1
        assert _count(conn, "inbound_outbox", event_id) == 1
