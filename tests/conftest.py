"""Fixtures compartilhadas dos testes."""

import asyncio
import importlib.util
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import psycopg
import pytest
from testcontainers.community.rabbitmq import RabbitMqContainer
from testcontainers.postgres import PostgresContainer

from issue_analyst_api.migrate import apply_migrations

# Cópia literal do DDL de `triage` e `publisher` lido pela view (D15); nunca vai para `migrations/`.
TRIAGE_PUBLISHER_STUB = (
    Path(__file__).resolve().parent / "fixtures" / "sql" / "triage_publisher_stub.sql"
)

if sys.platform == "win32":
    # O psycopg assíncrono não funciona com o ProactorEventLoop, padrão no Windows.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def apply_triage_publisher_stub(database_url: str) -> None:
    """Cria as tabelas de `triage` e `publisher` lidas pela view, antes das migrações (D15)."""
    with psycopg.connect(database_url) as conn:
        conn.execute(TRIAGE_PUBLISHER_STUB.read_text(encoding="utf-8"))


def migrate_with_stub(database_url: str) -> list[str]:
    """Aplica o stub de `triage` e `publisher` e depois as migrações do serviço."""
    apply_triage_publisher_stub(database_url)
    return apply_migrations(database_url)


@pytest.fixture(scope="session")
def postgres_container() -> Iterator[PostgresContainer]:
    """PostgreSQL 16 do testcontainers, um por sessão de testes."""
    with PostgresContainer("postgres:16", driver=None) as container:
        yield container


@pytest.fixture(scope="session")
def database_url(postgres_container: PostgresContainer) -> str:
    """URL do PostgreSQL da sessão, com o stub de `triage` e `publisher` e as migrações."""
    url = postgres_container.get_connection_url()
    migrate_with_stub(url)
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


def load_send_event_script() -> ModuleType:
    """Carrega `scripts/send_event.py` como módulo (a pasta `scripts/` não é um pacote)."""
    path = Path(__file__).resolve().parent.parent / "scripts" / "send_event.py"
    spec = importlib.util.spec_from_file_location("send_event", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
