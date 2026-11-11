"""Adaptador de conexão com o PostgreSQL: pool assíncrono do psycopg (decisão D1)."""

from psycopg_pool import AsyncConnectionPool

# Espera máxima por uma conexão do pool antes de responder `503 unavailable`. Fica abaixo do
# tempo limite de entrega do webhook do GitHub (10 s).
CONNECTION_TIMEOUT_S = 5.0


def create_pool(database_url: str) -> AsyncConnectionPool:
    """Cria o pool sem abri-lo; quem abre e fecha é o lifespan da aplicação."""
    return AsyncConnectionPool(conninfo=database_url, open=False, timeout=CONNECTION_TIMEOUT_S)


async def open_pool(pool: AsyncConnectionPool) -> None:
    """Abre o pool sem esperar conexões prontas.

    O serviço sobe mesmo com o banco fora do ar (o `/health` não depende dele); o pool continua
    tentando conectar em segundo plano.
    """
    await pool.open(wait=False)


async def close_pool(pool: AsyncConnectionPool) -> None:
    """Fecha o pool e as conexões abertas."""
    await pool.close()
