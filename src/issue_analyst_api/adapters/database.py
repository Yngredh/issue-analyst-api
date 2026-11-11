"""Adaptador de conexão com o PostgreSQL: pool assíncrono do psycopg (decisão D1)."""

from psycopg_pool import AsyncConnectionPool


def create_pool(database_url: str) -> AsyncConnectionPool:
    """Cria o pool sem abri-lo; quem abre e fecha é o lifespan da aplicação."""
    return AsyncConnectionPool(conninfo=database_url, open=False)


async def open_pool(pool: AsyncConnectionPool) -> None:
    """Abre o pool sem esperar conexões prontas.

    O serviço sobe mesmo com o banco fora do ar (o `/health` não depende dele); o pool continua
    tentando conectar em segundo plano.
    """
    await pool.open(wait=False)


async def close_pool(pool: AsyncConnectionPool) -> None:
    """Fecha o pool e as conexões abertas."""
    await pool.close()
