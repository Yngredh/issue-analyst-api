"""Relay da outbox: publica na fila `analysis` as mensagens gravadas pela entrada (RF07, RNF02,
RNF04, RNF05).

Roda em segundo plano, fora do caminho da requisição: o webhook só grava a outbox e responde; o
relay lê as linhas pendentes em ordem de `id`, publica cada uma com publisher confirms e marca
`published_at` na mesma transação da leitura, só depois da confirmação do broker. Se algo falhar
entre publicar e marcar, a transação é desfeita e a linha é publicada de novo na próxima volta
(entrega at-least-once; o `issue-triage` descarta repetições).
"""

import asyncio
import contextlib
import json

import aio_pika
import psycopg
from aio_pika.abc import AbstractChannel, AbstractConnection
from psycopg_pool import AsyncConnectionPool

ANALYSIS_QUEUE = "analysis"

# Backoff de reconexão ao broker (decisão D6): começa em 1 s, dobra a cada falha e para em 30 s.
RECONNECT_BACKOFF_INITIAL_S = 1.0
RECONNECT_BACKOFF_MAX_S = 30.0

_SELECT_PENDING = """
SELECT id, topic, dedup_key, payload
FROM inbound.inbound_outbox
WHERE published_at IS NULL
ORDER BY id
LIMIT %(batch_size)s
FOR UPDATE SKIP LOCKED
"""

_MARK_PUBLISHED = """
UPDATE inbound.inbound_outbox SET published_at = now() WHERE id = ANY(%(ids)s)
"""


async def declare_analysis_queue(channel: AbstractChannel) -> None:
    """Declara a fila `analysis` só como durável, sem argumentos `x-*` (decisão do P4).

    A DLQ fica por policy do RabbitMQ, fora deste serviço; o `issue-triage` declara a fila igual.
    """
    await channel.declare_queue(ANALYSIS_QUEUE, durable=True)


class OutboxRelay:
    """Tarefa em segundo plano que publica a outbox no RabbitMQ."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        rabbitmq_url: str,
        *,
        poll_interval_s: float,
        batch_size: int,
        backoff_initial_s: float = RECONNECT_BACKOFF_INITIAL_S,
        backoff_max_s: float = RECONNECT_BACKOFF_MAX_S,
    ) -> None:
        self._pool = pool
        self._rabbitmq_url = rabbitmq_url
        self._poll_interval_s = poll_interval_s
        self._batch_size = batch_size
        self._backoff_initial_s = backoff_initial_s
        self._backoff_max_s = backoff_max_s
        self._connection: AbstractConnection | None = None
        self._channel: AbstractChannel | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Inicia o relay como tarefa do loop atual, sem esperar o broker."""
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="outbox-relay")

    async def stop(self) -> None:
        """Para o relay e fecha a conexão com o broker.

        A tarefa é cancelada; se estiver no meio de um lote, a transação é desfeita e as linhas
        continuam pendentes (nenhuma linha é perdida: no pior caso, é publicada de novo).
        """
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._close_broker()

    async def _run(self) -> None:
        backoff_s = self._backoff_initial_s
        while True:
            if self._channel is None or self._channel.is_closed:
                try:
                    await self._connect()
                except Exception:
                    await self._close_broker()
                    await asyncio.sleep(backoff_s)
                    backoff_s = min(backoff_s * 2, self._backoff_max_s)
                    continue
                backoff_s = self._backoff_initial_s

            try:
                published = await self.publish_pending()
            except psycopg.Error:
                # Banco indisponível: as linhas continuam pendentes; tenta na próxima volta.
                await asyncio.sleep(self._poll_interval_s)
                continue
            except Exception:
                # Falha no broker durante a publicação: a transação foi desfeita e a conexão é
                # refeita com backoff.
                await self._close_broker()
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2, self._backoff_max_s)
                continue

            if published < self._batch_size:
                await asyncio.sleep(self._poll_interval_s)

    async def _connect(self) -> None:
        self._connection = await aio_pika.connect(self._rabbitmq_url)
        self._channel = await self._connection.channel(publisher_confirms=True)
        await declare_analysis_queue(self._channel)

    async def _close_broker(self) -> None:
        connection, self._connection, self._channel = self._connection, None, None
        if connection is not None and not connection.is_closed:
            with contextlib.suppress(Exception):
                await connection.close()

    async def publish_pending(self) -> int:
        """Publica um lote de linhas pendentes e marca `published_at`; devolve quantas publicou.

        Leitura com `FOR UPDATE SKIP LOCKED`, publicação e marcação na mesma transação: a
        marcação só acontece depois da confirmação do broker para todas as mensagens do lote.
        """
        if self._channel is None:
            raise RuntimeError("broker channel is not open")
        exchange = self._channel.default_exchange
        async with self._pool.connection() as conn, conn.transaction():
            cursor = await conn.execute(_SELECT_PENDING, {"batch_size": self._batch_size})
            rows = await cursor.fetchall()
            if not rows:
                return 0
            for _, topic, dedup_key, payload in rows:
                message = aio_pika.Message(
                    body=json.dumps(payload).encode(),
                    content_type="application/json",
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    message_id=dedup_key,
                )
                # Com publisher confirms, o `publish` só retorna depois do ack do broker.
                await exchange.publish(message, routing_key=topic)
            await self._mark_published(conn, [row[0] for row in rows])
            return len(rows)

    async def _mark_published(self, conn: psycopg.AsyncConnection, ids: list[int]) -> None:
        await conn.execute(_MARK_PUBLISHED, {"ids": ids})
