"""Mensagem de `issue.edited` (RF28, entrada; P1): a edição é gravada e, se a issue já tem
`issue.created`, vai para a outbox com `attempt` nulo, `trigger = author_edit` e `triggerRef`."""

import hashlib
import hmac
import json
import random
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from issue_analyst_api.api.app import create_app
from issue_analyst_api.config import Settings

SECRET = "edited-test-secret"
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


def _body(repository: str, number: int, action: str, description: str = "") -> bytes:
    return json.dumps(
        {
            "action": action,
            "issue": {
                "number": number,
                "title": "Erro ao salvar",
                "body": description,
                "created_at": "2026-11-10T12:00:00Z",
                "updated_at": "2026-11-10T13:00:00Z",
            },
            "repository": {"full_name": repository},
            "sender": {"type": "User"},
        }
    ).encode()


def _post(client: TestClient, body: bytes, delivery_id: str) -> httpx.Response:
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": signature,
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": delivery_id,
    }
    return client.post(URL, content=body, headers=headers)


def _events(conn: psycopg.Connection, repository: str, number: int) -> list[tuple]:
    return conn.execute(
        """
        SELECT event_id, type FROM inbound.issue_events
        WHERE repository = %s AND issue_number = %s ORDER BY received_at, event_id
        """,
        (repository, number),
    ).fetchall()


def _outbox(conn: psycopg.Connection, origin_event_id: str) -> list[tuple[str, str, Any]]:
    return conn.execute(
        """
        SELECT event_id, dedup_key, payload FROM inbound.inbound_outbox
        WHERE event_id = %s ORDER BY id
        """,
        (origin_event_id,),
    ).fetchall()


def _edit_outbox(conn: psycopg.Connection, origin_event_id: str) -> list[tuple[str, str, Any]]:
    return [row for row in _outbox(conn, origin_event_id) if ":edit:" in row[1]]


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(database_url))) as test_client:
        yield test_client


def _create_issue(client: TestClient, repository: str, number: int) -> str:
    origin_event_id = f"evt-{uuid.uuid4()}"
    response = _post(client, _body(repository, number, "opened"), origin_event_id)
    assert response.status_code == 202
    return origin_event_id


def test_edit_of_created_issue_writes_event_and_edit_message(
    client: TestClient, database_url: str
) -> None:
    repository, number = _unique_issue()
    origin_event_id = _create_issue(client, repository, number)
    edit_id = f"evt-{uuid.uuid4()}"

    response = _post(client, _body(repository, number, "edited", "Passos novos"), edit_id)

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "eventId": edit_id,
        "links": {"result": f"/issues/{repository}/{number}"},
    }
    with psycopg.connect(database_url) as conn:
        edit_rows = [row for row in _events(conn, repository, number) if row[0] == edit_id]
        edit_messages = _edit_outbox(conn, origin_event_id)
        edit_keyed = conn.execute(
            "SELECT count(*) FROM inbound.inbound_outbox WHERE event_id = %s", (edit_id,)
        ).fetchone()
    assert edit_rows == [(edit_id, "issue.edited")]
    assert edit_messages == [
        (
            origin_event_id,
            f"{origin_event_id}:edit:{edit_id}",
            {
                "eventId": origin_event_id,
                "attempt": None,
                "trigger": "author_edit",
                "triggerRef": edit_id,
            },
        )
    ]
    assert edit_keyed == (0,)  # a outbox é indexada pelo `origin_event_id`, não pela edição


def test_edit_without_created_issue_only_stores_event(
    client: TestClient, database_url: str
) -> None:
    repository, number = _unique_issue()
    edit_id = f"evt-{uuid.uuid4()}"

    response = _post(client, _body(repository, number, "edited"), edit_id)

    assert response.status_code == 202
    assert response.json()["eventId"] == edit_id
    with psycopg.connect(database_url) as conn:
        assert _events(conn, repository, number) == [(edit_id, "issue.edited")]
        outbox = conn.execute(
            "SELECT count(*) FROM inbound.inbound_outbox WHERE event_id = %s OR dedup_key LIKE %s",
            (edit_id, f"%:edit:{edit_id}"),
        ).fetchone()
    assert outbox == (0,)


def test_redelivered_edit_is_duplicate_event_without_new_message(
    client: TestClient, database_url: str
) -> None:
    repository, number = _unique_issue()
    origin_event_id = _create_issue(client, repository, number)
    edit_id = f"evt-{uuid.uuid4()}"
    body = _body(repository, number, "edited", "Passos novos")
    assert _post(client, body, edit_id).status_code == 202

    response = _post(client, body, edit_id)

    assert response.status_code == 200
    assert response.json() == {
        "status": "duplicate",
        "reason": "duplicate_event",
        "eventId": edit_id,
        "state": "ACCEPTED",
    }
    with psycopg.connect(database_url) as conn:
        assert len(_edit_outbox(conn, origin_event_id)) == 1
        assert len(_events(conn, repository, number)) == 2


def test_two_different_edits_of_same_issue_write_two_messages(
    client: TestClient, database_url: str
) -> None:
    repository, number = _unique_issue()
    origin_event_id = _create_issue(client, repository, number)
    first_edit, second_edit = f"evt-{uuid.uuid4()}", f"evt-{uuid.uuid4()}"

    assert _post(client, _body(repository, number, "edited", "v1"), first_edit).status_code == 202
    assert _post(client, _body(repository, number, "edited", "v2"), second_edit).status_code == 202

    with psycopg.connect(database_url) as conn:
        messages = _edit_outbox(conn, origin_event_id)
        all_messages = _outbox(conn, origin_event_id)
    assert [(key, payload["triggerRef"]) for _, key, payload in messages] == [
        (f"{origin_event_id}:edit:{first_edit}", first_edit),
        (f"{origin_event_id}:edit:{second_edit}", second_edit),
    ]
    # A mensagem inicial do `issue.created` continua sendo a única com `attempt = 1`.
    assert [
        key for _, key, _ in all_messages if not key.startswith(f"{origin_event_id}:edit:")
    ] == [f"{origin_event_id}:1"]
