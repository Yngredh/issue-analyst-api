"""Fixtures compartilhadas dos testes."""

import asyncio
import sys
from collections.abc import Iterator

import psycopg
import pytest
from testcontainers.community.rabbitmq import RabbitMqContainer
from testcontainers.postgres import PostgresContainer

from issue_analyst_api.migrate import apply_migrations

if sys.platform == "win32":
    # O psycopg assíncrono não funciona com o ProactorEventLoop, padrão no Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """PostgreSQL 16 do testcontainers, um por sessão de testes."""
    with PostgresContainer("postgres:16", driver=None) as container:
        yield container


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    """URL do PostgreSQL da sessão, com as migrações já aplicadas."""
    url = postgres_container.get_connection_url()
    apply_migrations(url)
    return url


@pytest.fixture
def db_conn(database_url: str) -> Iterator[psycopg.Connection]:
    """Conexão dentro de uma transação desfeita ao fim do teste."""
    with psycopg.connect(database_url) as conn:
        yield conn
        conn.rollback()


@pytest.fixture(scope="session")
def rabbitmq_container() -> Iterator[RabbitMqContainer]:
    """RabbitMQ do testcontainers (imagem da D5), um por sessão de testes."""
    with RabbitMqContainer("rabbitmq:3-management") as container:
        yield container


def rabbitmq_url_of(container: RabbitMqContainer) -> str:
    """URL AMQP do RabbitMQ do testcontainers."""
    host = container.get_container_host_ip()
    port = container.get_exposed_port(container.port)
    return f"amqp://{container.username}:{container.password}@{host}:{port}/"


@pytest.fixture(scope="session")
def rabbitmq_url(rabbitmq_container: RabbitMqContainer) -> str:
    """URL AMQP do RabbitMQ da sessão."""
    return rabbitmq_url_of(rabbitmq_container)
