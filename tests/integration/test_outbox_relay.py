"""Relay da outbox com PostgreSQL e RabbitMQ do testcontainers (RF07, RNF02, RNF04, RNF05)."""

import asyncio
import hashlib
import hmac
import json
import random
import socket
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable

import aio_pika
import psycopg
import pytest
from aio_pika.abc import AbstractIncomingMessage
from aio_pika.exceptions import ChannelPreconditionFailed
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr
from testcontainers.community.rabbitmq import RabbitMqContainer

from issue_analyst_api.adapters.database import close_pool, create_pool, open_pool
from issue_analyst_api.adapters.outbox_relay import ANALYSIS_QUEUE, OutboxRelay
from issue_analyst_api.api.app import create_app
from issue_analyst_api.config import Settings
from tests.conftest import rabbitmq_url_of

SECRET = "relay-test-secret"
TIMEOUT_S = 30.0


@pytest.fixture
async def pool(database_url: str) -> AsyncIterator[AsyncConnectionPool]:
    db_pool = create_pool(database_url)
    await open_pool(db_pool)
    try:
        yield db_pool
    finally:
        await close_pool(db_pool)


def _relay(pool: AsyncConnectionPool, rabbitmq_url: str, **kwargs: float) -> OutboxRelay:
    params: dict = {
        "poll_interval_s": 0.1,
        "batch_size": 100,
        "backoff_initial_s": 0.2,
        "backoff_max_s": 1.0,
    }
    params.update(kwargs)
    return OutboxRelay(pool, rabbitmq_url, **params)


def _insert_outbox(database_url: str, count: int) -> list[str]:
    """Grava `count` mensagens pendentes, em ordem de `id`, e devolve as `dedup_key`."""
    keys: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        for _ in range(count):
            event_id = f"relay-{uuid.uuid4()}"
            dedup_key = f"{event_id}:1"
            payload = {"eventId": event_id, "attempt": 1, "trigger": "initial", "triggerRef": None}
            conn.execute(
                """
                INSERT INTO inbound.inbound_outbox (event_id, topic, dedup_key, payload)
                VALUES (%s, 'analysis', %s, %s)
                """,
                (event_id, dedup_key, Jsonb(payload)),
            )
            keys.append(dedup_key)
    return keys


async def _run_until_all_published(
    relay: OutboxRelay, database_url: str, keys: list[str]
) -> None:
    """Roda o relay até todas as `keys` estarem marcadas como publicadas e o para."""

    def all_published() -> bool:
        return _published(database_url, keys) == set(keys)

    await _with_relay(relay, lambda: _wait_until(all_published))


def _published(database_url: str, keys: list[str]) -> set[str]:
    with psycopg.connect(database_url) as conn:
        rows = conn.execute(
            """
            SELECT dedup_key FROM inbound.inbound_outbox
            WHERE dedup_key = ANY(%s) AND published_at IS NOT NULL
            """,
            (keys,),
        ).fetchall()
    return {row[0] for row in rows}


async def _wait_until(condition: Callable[[], bool], timeout_s: float = TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not reached before timeout")
        await asyncio.sleep(0.1)


async def _drain(rabbitmq_url: str) -> list[AbstractIncomingMessage]:
    """Consome tudo o que está na fila `analysis`, em ordem."""
    messages: list[AbstractIncomingMessage] = []
    connection = await aio_pika.connect(rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(ANALYSIS_QUEUE, durable=True)
        while (message := await queue.get(no_ack=True, fail=False)) is not None:
            messages.append(message)
    return messages


async def _with_relay(relay: OutboxRelay, body: Callable[[], Awaitable[None]]) -> None:
    relay.start()
    try:
        await body()
    finally:
        await relay.stop()


async def test_pending_messages_are_published_in_order_and_marked(
    pool: AsyncConnectionPool, database_url: str, rabbitmq_url: str
) -> None:
    await _drain(rabbitmq_url)
    keys = _insert_outbox(database_url, 5)

    # Lote menor que o total: a ordem tem de se manter entre lotes.
    relay = _relay(pool, rabbitmq_url, batch_size=2)
    await _run_until_all_published(relay, database_url, keys)

    messages = [m for m in await _drain(rabbitmq_url) if m.message_id in keys]
    assert [m.message_id for m in messages] == keys
    for message, key in zip(messages, keys, strict=True):
        event_id = key.removesuffix(":1")
        assert json.loads(message.body) == {
            "eventId": event_id,
            "attempt": 1,
            "trigger": "initial",
            "triggerRef": None,
        }
        assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
        assert message.content_type == "application/json"


async def test_analysis_queue_is_declared_durable_without_arguments(
    pool: AsyncConnectionPool, database_url: str, rabbitmq_url: str
) -> None:
    keys = _insert_outbox(database_url, 1)
    relay = _relay(pool, rabbitmq_url)
    await _run_until_all_published(relay, database_url, keys)

    connection = await aio_pika.connect(rabbitmq_url)
    async with connection:
        # Mesma declaração (durável, sem argumentos) é aceita: os parâmetros batem (P4).
        channel = await connection.channel()
        await channel.declare_queue(ANALYSIS_QUEUE, durable=True)
        with pytest.raises(ChannelPreconditionFailed):
            await channel.declare_queue(ANALYSIS_QUEUE, durable=False)
        channel = await connection.channel()
        with pytest.raises(ChannelPreconditionFailed):
            await channel.declare_queue(
                ANALYSIS_QUEUE, durable=True, arguments={"x-dead-letter-exchange": "analysis.dlx"}
            )


class _FailOnceBeforeMarking(OutboxRelay):
    """Simula a queda entre a confirmação do broker e a marcação de `published_at`."""

    failed = False

    async def _mark_published(self, conn: psycopg.AsyncConnection, ids: list[int]) -> None:
        if not self.failed:
            self.failed = True
            raise ConnectionError("simulated crash after publishing")
        await super()._mark_published(conn, ids)


async def test_crash_between_publish_and_mark_republishes(
    pool: AsyncConnectionPool, database_url: str, rabbitmq_url: str
) -> None:
    await _drain(rabbitmq_url)
    keys = _insert_outbox(database_url, 1)
    relay = _FailOnceBeforeMarking(
        pool, rabbitmq_url, poll_interval_s=0.1, batch_size=100, backoff_initial_s=0.2
    )

    await _run_until_all_published(relay, database_url, keys)

    assert relay.failed
    delivered = [m.message_id for m in await _drain(rabbitmq_url) if m.message_id in keys]
    # At-least-once: a mensagem confirmada e não marcada é publicada de novo.
    assert delivered == [keys[0], keys[0]]


async def test_stop_does_not_lose_rows(
    pool: AsyncConnectionPool, database_url: str, rabbitmq_url: str
) -> None:
    await _drain(rabbitmq_url)
    keys = _insert_outbox(database_url, 60)

    relay = _relay(pool, rabbitmq_url, batch_size=5)
    await _with_relay(relay, lambda: _wait_until(lambda: len(_published(database_url, keys)) > 0))
    marked_at_stop = _published(database_url, keys)
    delivered = {m.message_id for m in await _drain(rabbitmq_url)}
    # Toda linha marcada foi confirmada pelo broker antes do encerramento.
    assert marked_at_stop <= delivered

    # O restante continua pendente e é publicado por um novo relay.
    relay = _relay(pool, rabbitmq_url, batch_size=5)
    await _run_until_all_published(relay, database_url, keys)
    delivered |= {m.message_id for m in await _drain(rabbitmq_url)}
    assert set(keys) <= delivered


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_broker_down_accumulates_and_publishes_when_back(
    pool: AsyncConnectionPool, database_url: str
) -> None:
    # Porta fixa no host: o broker volta no mesmo endereço depois de parado.
    broker = RabbitMqContainer("rabbitmq:3-management").with_bind_ports(5672, _free_port())
    with broker:
        url = rabbitmq_url_of(broker)
        relay = _relay(pool, url)
        relay.start()
        try:
            first = _insert_outbox(database_url, 1)
            await _wait_until(lambda: _published(database_url, first) == set(first))

            await asyncio.to_thread(broker.get_wrapped_container().stop)
            pending = _insert_outbox(database_url, 3)
            await asyncio.sleep(2)
            assert _published(database_url, pending) == set()

            await asyncio.to_thread(broker.get_wrapped_container().start)
            await _wait_until(lambda: _published(database_url, pending) == set(pending), 90)
        finally:
            await relay.stop()

        messages = await _drain(url)
        delivered = [m.message_id for m in messages if m.message_id in pending]
        assert delivered == pending


def _signed_issue_opened() -> tuple[bytes, dict[str, str], str]:
    event_id = f"relay-webhook-{uuid.uuid4()}"
    payload = {
        "action": "opened",
        "issue": {
            "number": random.randint(1, 1_000_000),
            "title": "Erro ao salvar",
            "body": "Passos para reproduzir",
            "created_at": "2026-11-10T12:00:00Z",
        },
        "repository": {"full_name": f"acme/relay-{uuid.uuid4().hex[:8]}"},
        "sender": {"type": "User", "login": "alguem"},
    }
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": "sha256="
        + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest(),
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": event_id,
    }
    return body, headers, f"{event_id}:1"


def _settings(database_url: str, rabbitmq_url: str) -> Settings:
    return Settings(
        GITHUB_WEBHOOK_SECRET=SecretStr(SECRET),
        DATABASE_URL=database_url,
        RABBITMQ_URL=rabbitmq_url,
        RELAY_POLL_INTERVAL_S=0.1,
    )


def test_service_relay_publishes_accepted_event(database_url: str, rabbitmq_url: str) -> None:
    body, headers, dedup_key = _signed_issue_opened()

    with TestClient(create_app(_settings(database_url, rabbitmq_url))) as client:
        response = client.post("/webhooks/github", content=body, headers=headers)
        assert response.status_code == 202
        deadline = time.monotonic() + TIMEOUT_S
        while _published(database_url, [dedup_key]) != {dedup_key}:
            assert time.monotonic() < deadline, "relay did not publish the accepted event"
            time.sleep(0.1)


def test_webhook_still_accepts_with_broker_down(database_url: str) -> None:
    body, headers, dedup_key = _signed_issue_opened()
    broker_down = "amqp://guest:guest@127.0.0.1:1/"

    with TestClient(create_app(_settings(database_url, broker_down))) as client:
        response = client.post("/webhooks/github", content=body, headers=headers)
        time.sleep(1)

    assert response.status_code == 202
    # A mensagem fica na outbox, pendente, até o broker voltar.
    assert _published(database_url, [dedup_key]) == set()
    with psycopg.connect(database_url) as conn:
        count = conn.execute(
            "SELECT count(*) FROM inbound.inbound_outbox WHERE dedup_key = %s", (dedup_key,)
        ).fetchone()
    assert count == (1,)
