import uuid

import psycopg
import pytest
from psycopg import errors
from pydantic import SecretStr
from testcontainers.postgres import PostgresContainer

from issue_analyst_api.api.app import create_app
from issue_analyst_api.config import Settings
from issue_analyst_api.migrate import apply_migrations

INSERT_EVENT = """
INSERT INTO inbound.issue_events
  (event_id, type, occurred_at, repository, issue_number, title, raw_payload, body_hash)
VALUES (%s, %s, now(), %s, %s, %s, '{}'::jsonb, 'hash')
"""


def _insert_event(
    conn: psycopg.Connection,
    *,
    type_: str = "issue.created",
    repository: str = "acme/shop",
    issue_number: int = 1,
    title: str = "Checkout quebra",
) -> None:
    conn.execute(INSERT_EVENT, (str(uuid.uuid4()), type_, repository, issue_number, title))


def test_tables_and_indexes_created(db_conn: psycopg.Connection) -> None:
    tables = {
        row[0]
        for row in db_conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'inbound'"
        )
    }
    assert {"issue_events", "inbound_outbox", "schema_migrations"} <= tables

    indexes = {
        row[0]: row[1]
        for row in db_conn.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'inbound'"
        )
    }
    assert "issue_events_one_created_per_issue" in indexes
    assert "UNIQUE" in indexes["issue_events_one_created_per_issue"]
    assert "WHERE (type = 'issue.created'::text)" in indexes["issue_events_one_created_per_issue"]
    assert "inbound_outbox_pending" in indexes
    assert "WHERE (published_at IS NULL)" in indexes["inbound_outbox_pending"]
    assert any(
        "(repository, issue_number, received_at)" in definition
        for definition in indexes.values()
    )
    assert any(
        "inbound_outbox" in definition and "(topic, dedup_key)" in definition
        for definition in indexes.values()
    )


def test_partial_index_accepts_edited_and_blocks_second_created(
    db_conn: psycopg.Connection,
) -> None:
    _insert_event(db_conn, type_="issue.created", issue_number=10)
    _insert_event(db_conn, type_="issue.edited", issue_number=10)
    _insert_event(db_conn, type_="issue.edited", issue_number=10)

    with pytest.raises(errors.UniqueViolation):
        _insert_event(db_conn, type_="issue.created", issue_number=10)


def test_check_rejects_blank_title(db_conn: psycopg.Connection) -> None:
    with pytest.raises(errors.CheckViolation):
        _insert_event(db_conn, issue_number=20, title="   ")


def test_migration_runner_is_idempotent(postgres_container: PostgresContainer) -> None:
    # Banco novo e vazio, para ver a primeira aplicação e a repetição.
    admin_url = postgres_container.get_connection_url()
    db_name = f"migrate_{uuid.uuid4().hex[:8]}"
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(f"CREATE DATABASE {db_name}")
    url = admin_url.rsplit("/", 1)[0] + f"/{db_name}"

    assert apply_migrations(url) == ["0001"]
    assert apply_migrations(url) == []

    with psycopg.connect(url) as conn:
        versions = conn.execute("SELECT version FROM inbound.schema_migrations").fetchall()
        tables = conn.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'inbound'"
        ).fetchone()
    assert versions == [("0001",)]
    assert tables == (3,)


async def test_lifespan_opens_and_closes_pool(database_url: str) -> None:
    settings = Settings(
        GITHUB_WEBHOOK_SECRET=SecretStr("test-secret"),
        DATABASE_URL=database_url,
        RABBITMQ_URL="amqp://nobody:nothing@127.0.0.1:1/",
    )
    app = create_app(settings)

    async with app.router.lifespan_context(app):
        pool = app.state.db_pool
        async with pool.connection() as conn:
            cursor = await conn.execute("SELECT 1")
            assert await cursor.fetchone() == (1,)

    assert pool.closed
