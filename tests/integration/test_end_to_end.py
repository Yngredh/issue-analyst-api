"""Ponta a ponta da entrada (RNF01, RNF02, RNF16).

Real: o serviço (uvicorn numa porta local, com lifespan e relay), o PostgreSQL e o RabbitMQ do
testcontainers, e o envio pelo `scripts/send_event.py` (HTTP com HMAC). Simulado: o GitHub (o
script faz o papel dele). Não há consumidor da fila `analysis`: o `issue-triage` está parado.
"""

import asyncio
import json
import random
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import aio_pika
import psycopg
import uvicorn
from aio_pika.abc import AbstractIncomingMessage
from pydantic import SecretStr
from testcontainers.community.rabbitmq import RabbitMqContainer
from testcontainers.postgres import PostgresContainer

from issue_analyst_api.adapters.outbox_relay import ANALYSIS_QUEUE
from issue_analyst_api.api.app import create_app
from issue_analyst_api.config import Settings
from tests.conftest import load_send_event_script, migrate_with_stub, rabbitmq_url_of

SECRET = "e2e-test-secret"
TIMEOUT_S = 30.0

send_event = load_send_event_script()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def _running_service(database_url: str, rabbitmq_url: str) -> Iterator[str]:
    """Sobe o serviço com uvicorn numa thread e devolve a URL do webhook."""
    settings = Settings(
        GITHUB_WEBHOOK_SECRET=SecretStr(SECRET),
        DATABASE_URL=database_url,
        RABBITMQ_URL=rabbitmq_url,
        RELAY_POLL_INTERVAL_S=0.1,
    )
    port = _free_port()
    config = uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    def serve() -> None:
        # Loop seletor também no Windows, como em `main.py` (psycopg assíncrono).
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    deadline = time.monotonic() + TIMEOUT_S
    while not server.started:
        assert thread.is_alive(), "service failed to start"
        assert time.monotonic() < deadline, "service did not start in time"
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}/webhooks/github"
    finally:
        server.should_exit = True
        thread.join(TIMEOUT_S)


def _unique_issue() -> tuple[str, int]:
    return f"equipe/e2e-{uuid.uuid4().hex[:8]}", random.randint(1, 1_000_000)


def _send(url: str, delivery_id: str, repository: str, number: int) -> tuple[int, dict]:
    """Envia um `issues.opened` assinado pelo script de envio."""
    payload = send_event.build_payload(
        "opened", repository, number, "Erro ao salvar", "Passos para reproduzir"
    )
    status, body = send_event.send(send_event.build_request(url, SECRET, delivery_id, payload))
    return status, json.loads(body)


def _published(database_url: str, dedup_key: str) -> bool:
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT published_at IS NOT NULL FROM inbound.inbound_outbox WHERE dedup_key = %s",
            (dedup_key,),
        ).fetchone()
    return bool(row and row[0])


def _outbox_count(database_url: str, event_id: str) -> int:
    with psycopg.connect(database_url) as conn:
        row = conn.execute(
            "SELECT count(*) FROM inbound.inbound_outbox WHERE event_id = %s", (event_id,)
        ).fetchone()
    assert row is not None
    return row[0]


def _wait_until(condition: Callable[[], bool], timeout_s: float = TIMEOUT_S) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, "condition not reached before timeout"
        time.sleep(0.1)


async def _queue_depth(rabbitmq_url: str) -> int:
    """Mensagens prontas na fila `analysis`, sem consumi-las."""
    connection = await aio_pika.connect(rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(ANALYSIS_QUEUE, durable=True, passive=True)
        return queue.declaration_result.message_count or 0


async def _drain(rabbitmq_url: str) -> list[AbstractIncomingMessage]:
    """Consome tudo o que está na fila `analysis` (só no fim do teste, para conferir)."""
    messages: list[AbstractIncomingMessage] = []
    connection = await aio_pika.connect(rabbitmq_url)
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(ANALYSIS_QUEUE, durable=True)
        while (message := await queue.get(no_ack=True, fail=False)) is not None:
            messages.append(message)
    return messages


def _messages_for(rabbitmq_url: str, dedup_key: str) -> list[AbstractIncomingMessage]:
    return [m for m in asyncio.run(_drain(rabbitmq_url)) if m.message_id == dedup_key]


def test_without_triage_message_stays_pending_in_analysis_queue(
    database_url: str, rabbitmq_url: str
) -> None:
    repository, number = _unique_issue()
    event_id = f"e2e-{uuid.uuid4()}"
    dedup_key = f"{event_id}:1"

    with _running_service(database_url, rabbitmq_url) as url:
        status, body = _send(url, event_id, repository, number)
        assert status == 202
        assert body == {
            "status": "accepted",
            "eventId": event_id,
            "links": {"result": f"/issues/{repository}/{number}"},
        }
        _wait_until(lambda: _published(database_url, dedup_key))

    # Sem consumidor, a mensagem continua na fila depois que o serviço para.
    assert asyncio.run(_queue_depth(rabbitmq_url)) >= 1
    [message] = _messages_for(rabbitmq_url, dedup_key)
    assert json.loads(message.body) == {
        "eventId": event_id,
        "attempt": 1,
        "trigger": "initial",
        "triggerRef": None,
    }
    assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT


def test_resend_same_delivery_is_duplicate_with_single_message(
    database_url: str, rabbitmq_url: str
) -> None:
    repository, number = _unique_issue()
    event_id = f"e2e-{uuid.uuid4()}"
    dedup_key = f"{event_id}:1"

    with _running_service(database_url, rabbitmq_url) as url:
        first_status, _ = _send(url, event_id, repository, number)
        _wait_until(lambda: _published(database_url, dedup_key))
        second_status, second_body = _send(url, event_id, repository, number)
        # Dá tempo ao relay de publicar algo a mais, se houvesse.
        time.sleep(1)

    assert first_status == 202
    assert second_status == 200
    assert second_body == {
        "status": "duplicate",
        "reason": "duplicate_event",
        "eventId": event_id,
        "state": "ACCEPTED",
    }
    assert _outbox_count(database_url, event_id) == 1
    assert len(_messages_for(rabbitmq_url, dedup_key)) == 1


def test_broker_down_accepts_and_outbox_publishes_when_back(database_url: str) -> None:
    repository, number = _unique_issue()
    event_id = f"e2e-{uuid.uuid4()}"
    dedup_key = f"{event_id}:1"
    # Porta fixa no host: o broker volta no mesmo endereço depois de parado.
    broker = RabbitMqContainer("rabbitmq:3-management").with_bind_ports(5672, _free_port())

    with broker:
        broker_url = rabbitmq_url_of(broker)
        with _running_service(database_url, broker_url) as url:
            broker.get_wrapped_container().stop()

            status, body = _send(url, event_id, repository, number)
            assert status == 202
            assert body["eventId"] == event_id
            time.sleep(2)
            assert not _published(database_url, dedup_key)
            assert _outbox_count(database_url, event_id) == 1

            broker.get_wrapped_container().start()
            _wait_until(lambda: _published(database_url, dedup_key), 90)

        assert len(_messages_for(broker_url, dedup_key)) == 1


def test_database_down_returns_503_and_stores_nothing(rabbitmq_url: str) -> None:
    repository, number = _unique_issue()
    event_id = f"e2e-{uuid.uuid4()}"
    # Porta fixa no host: o banco volta no mesmo endereço para conferir que nada foi gravado.
    database = PostgresContainer("postgres:16", driver=None).with_bind_ports(5432, _free_port())

    with database:
        database_url = database.get_connection_url()
        migrate_with_stub(database_url)
        with _running_service(database_url, rabbitmq_url) as url:
            database.get_wrapped_container().stop()
            status, body = _send(url, event_id, repository, number)

        assert status == 503
        assert body == {"error": "unavailable"}

        database.get_wrapped_container().start()

        def stored_rows() -> int | None:
            try:
                with psycopg.connect(database_url) as conn:
                    row = conn.execute(
                        "SELECT (SELECT count(*) FROM inbound.issue_events)"
                        " + (SELECT count(*) FROM inbound.inbound_outbox)"
                    ).fetchone()
            except psycopg.OperationalError:
                return None
            assert row is not None
            return row[0]

        _wait_until(lambda: stored_rows() is not None, 60)
        assert stored_rows() == 0
