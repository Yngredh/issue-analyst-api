"""Logs do relay da outbox (RNF14): falha de conexão, retomada e nenhum segredo no log.

Broker e banco são simulados: o relay real roda com `_connect` e `publish_pending` roteirizados.
A publicação com `eventId` no contexto é coberta em `tests/integration/test_outbox_relay.py`.
"""

import asyncio
import time
from typing import Any

import psycopg
import structlog

from issue_analyst_api.adapters.outbox_relay import OutboxRelay

BROKER_PASSWORD = "relay-broker-password"
BROKER_URL = f"amqp://user:{BROKER_PASSWORD}@127.0.0.1:1/"


class _OpenChannel:
    is_closed = False


class _ScriptedRelay(OutboxRelay):
    """Relay com conexão e publicação roteirizadas; o resto do laço é o real."""

    def __init__(self, connect_failures: int, publish_script: list[Any]) -> None:
        super().__init__(
            None,  # type: ignore[arg-type]  # o pool não é usado: `publish_pending` é simulado
            BROKER_URL,
            poll_interval_s=0.01,
            batch_size=100,
            backoff_initial_s=0.01,
            backoff_max_s=0.02,
        )
        self.connect_failures = connect_failures
        self.publish_script = publish_script

    async def _connect(self) -> None:
        if self.connect_failures > 0:
            self.connect_failures -= 1
            raise ConnectionError(f"cannot connect to {BROKER_URL}")
        self._channel = _OpenChannel()  # type: ignore[assignment]

    async def _close_broker(self) -> None:
        self._channel = None

    async def publish_pending(self) -> int:
        if not self.publish_script:
            return 0
        step = self.publish_script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


async def _run(relay: _ScriptedRelay) -> list[dict[str, Any]]:
    with structlog.testing.capture_logs() as logs:
        relay.start()
        deadline = time.monotonic() + 5
        while relay.publish_script or relay.connect_failures:
            assert time.monotonic() < deadline, "scripted relay did not finish"
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.05)
        await relay.stop()
    return [entry for entry in logs if entry["event"].startswith("relay_")]


async def test_first_connection_is_logged_as_connected() -> None:
    logs = await _run(_ScriptedRelay(connect_failures=0, publish_script=[0]))

    assert [entry["event"] for entry in logs] == ["relay_broker_connected"]


async def test_connection_failures_and_resumption_are_logged() -> None:
    logs = await _run(
        _ScriptedRelay(
            connect_failures=2,
            publish_script=[
                psycopg.OperationalError("db down"),
                psycopg.OperationalError("db down"),
                0,
                RuntimeError("channel closed while publishing"),
                0,
            ],
        )
    )

    assert [entry["event"] for entry in logs] == [
        "relay_broker_connection_failed",
        "relay_broker_connection_failed",
        "relay_broker_connection_resumed",
        "relay_database_unavailable",  # uma vez só, mesmo com duas falhas seguidas
        "relay_database_resumed",
        "relay_broker_connection_failed",  # falha do broker durante a publicação
        "relay_broker_connection_resumed",
    ]
    failures = [entry for entry in logs if entry["event"] == "relay_broker_connection_failed"]
    assert [entry["log_level"] for entry in failures] == ["warning"] * 3
    assert [entry["error"] for entry in failures] == [
        "ConnectionError",
        "ConnectionError",
        "RuntimeError",
    ]
    assert all("retryInS" in entry for entry in failures)
    [database_down] = [e for e in logs if e["event"] == "relay_database_unavailable"]
    assert database_down["error"] == "OperationalError"


async def test_relay_logs_never_contain_broker_credentials() -> None:
    logs = await _run(_ScriptedRelay(connect_failures=3, publish_script=[]))

    assert BROKER_PASSWORD not in repr(logs)
    assert BROKER_URL not in repr(logs)
