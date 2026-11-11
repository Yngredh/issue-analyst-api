"""Persistência transacional do evento com a outbox (RF07, RNF05, RNF07)."""

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
from fastapi.testclient import TestClient
from psycopg import errors
from psycopg.types.json import Jsonb
from pydantic import SecretStr

from issue_analyst_api.adapters.database import close_pool, create_pool, open_pool
from issue_analyst_api.adapters.issue_events import store_issue_created
from issue_analyst_api.api.app import create_app
from issue_analyst_api.config import Settings
from issue_analyst_api.domain.events import NormalizedIssueEvent

SECRET = "persistence-test-secret"
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


def _payload(repository: str, number: int, *, action: str = "opened", **issue: Any) -> dict:
    data: dict[str, Any] = {
        "number": number,
        "title": "Erro ao salvar",
        "body": "Passos para reproduzir",
        "created_at": "2026-11-10T12:00:00Z",
        "updated_at": "2026-11-10T13:00:00Z",
        "labels": [{"name": "bug"}],
    }
    data.update(issue)
    return {
        "action": action,
        "issue": data,
        "repository": {"full_name": repository},
        "sender": {"type": "User", "login": "alguem"},
    }


def _post(client: TestClient, body: bytes, event_id: str) -> httpx.Response:
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": event_id,
    }
    return client.post(URL, content=body, headers=headers)


def _event_rows(conn: psycopg.Connection, event_id: str) -> list[tuple]:
    return conn.execute(
        """
        SELECT type, occurred_at, received_at, repository, issue_number, title, description,
               raw_payload, body_hash, status
        FROM inbound.issue_events WHERE event_id = %s
        """,
        (event_id,),
    ).fetchall()


def _outbox_rows(conn: psycopg.Connection, event_id: str) -> list[tuple]:
    return conn.execute(
        """
        SELECT topic, dedup_key, payload, published_at
        FROM inbound.inbound_outbox WHERE event_id = %s
        """,
        (event_id,),
    ).fetchall()


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(database_url))) as test_client:
        yield test_client


def test_accepted_event_writes_one_event_and_one_outbox_row(
    client: TestClient, database_url: str
) -> None:
    repository, number = _unique_issue()
    event_id = f"evt-{uuid.uuid4()}"
    payload = _payload(repository, number)
    # Corpo com espaços e ordem de chaves própria: o hash é do corpo bruto.
    body = json.dumps(payload, indent=2).encode()

    response = _post(client, body, event_id)

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "eventId": event_id,
        "links": {"result": f"/issues/{repository}/{number}"},
    }
    with psycopg.connect(database_url) as conn:
        events = _event_rows(conn, event_id)
        outbox = _outbox_rows(conn, event_id)
    assert len(events) == 1
    (type_, occurred_at, _received, repo, issue_number, title, description, raw, hash_, status) = (
        events[0]
    )
    assert type_ == "issue.created"
    assert occurred_at == datetime(2026, 11, 10, 12, 0, tzinfo=UTC)
    assert (repo, issue_number, title, description) == (
        repository,
        number,
        "Erro ao salvar",
        "Passos para reproduzir",
    )
    assert raw == payload  # raw_payload guardado sem alteração
    assert hash_ == hashlib.sha256(body).hexdigest()
    assert status == "ACCEPTED"
    assert outbox == [
        (
            "analysis",
            f"{event_id}:1",
            {"eventId": event_id, "attempt": 1, "trigger": "initial", "triggerRef": None},
            None,
        )
    ]


def test_empty_description_is_stored_as_empty_string(
    client: TestClient, database_url: str
) -> None:
    repository, number = _unique_issue()
    event_id = f"evt-{uuid.uuid4()}"
    body = json.dumps(_payload(repository, number, body="   ")).encode()

    assert _post(client, body, event_id).status_code == 202

    with psycopg.connect(database_url) as conn:
        assert _event_rows(conn, event_id)[0][6] == ""


@pytest.mark.parametrize("created_at", [None, "ontem"])
def test_occurred_at_fallback_uses_received_at(
    client: TestClient, database_url: str, created_at: str | None
) -> None:
    repository, number = _unique_issue()
    event_id = f"evt-{uuid.uuid4()}"
    payload = _payload(repository, number, created_at=created_at)
    if created_at is None:
        del payload["issue"]["created_at"]

    assert _post(client, json.dumps(payload).encode(), event_id).status_code == 202

    with psycopg.connect(database_url) as conn:
        occurred_at, received_at = _event_rows(conn, event_id)[0][1:3]
    assert occurred_at == received_at


def test_same_event_id_again_returns_duplicate_without_new_outbox_row(
    client: TestClient, database_url: str
) -> None:
    repository, number = _unique_issue()
    event_id = f"evt-{uuid.uuid4()}"
    body = json.dumps(_payload(repository, number)).encode()

    assert _post(client, body, event_id).status_code == 202
    response = _post(client, body, event_id)

    assert response.status_code == 200
    assert response.json() == {"status": "duplicate", "eventId": event_id}
    with psycopg.connect(database_url) as conn:
        assert len(_event_rows(conn, event_id)) == 1
        assert len(_outbox_rows(conn, event_id)) == 1


def test_edited_issue_returns_501_and_writes_nothing(
    client: TestClient, database_url: str
) -> None:
    repository, number = _unique_issue()
    event_id = f"evt-{uuid.uuid4()}"
    body = json.dumps(_payload(repository, number, action="edited")).encode()

    assert _post(client, body, event_id).status_code == 501

    with psycopg.connect(database_url) as conn:
        assert _event_rows(conn, event_id) == []
        assert _outbox_rows(conn, event_id) == []


async def test_outbox_failure_rolls_back_event(database_url: str) -> None:
    repository, number = _unique_issue()
    event_id = f"evt-{uuid.uuid4()}"
    # Ocupa a chave da outbox para forçar a falha no segundo insert da transação.
    with psycopg.connect(database_url) as conn:
        conn.execute(
            """
            INSERT INTO inbound.inbound_outbox (event_id, topic, dedup_key, payload)
            VALUES (%s, 'analysis', %s, %s)
            """,
            (event_id, f"{event_id}:1", Jsonb({})),
        )
    event = NormalizedIssueEvent(
        event_id=event_id,
        type="issue.created",
        occurred_at=datetime(2026, 11, 10, 12, 0, tzinfo=UTC),
        repository=repository,
        issue_number=number,
        title="Erro ao salvar",
        description="",
    )
    pool = create_pool(database_url)
    await open_pool(pool)
    try:
        with pytest.raises(errors.UniqueViolation):
            await store_issue_created(pool, event, _payload(repository, number), b"{}")
    finally:
        await close_pool(pool)

    with psycopg.connect(database_url) as conn:
        assert _event_rows(conn, event_id) == []
        assert len(_outbox_rows(conn, event_id)) == 1  # só a linha pré-existente


def test_database_down_returns_503() -> None:
    settings = _settings("postgresql://nobody:nothing@127.0.0.1:1/none")
    repository, number = _unique_issue()
    body = json.dumps(_payload(repository, number)).encode()

    with TestClient(create_app(settings)) as down_client:
        response = _post(down_client, body, f"evt-{uuid.uuid4()}")

    assert response.status_code == 503
    assert response.json() == {"error": "unavailable"}
