"""Runner das migrações SQL do schema `inbound` (decisão D2).

Aplica, em ordem, os arquivos `migrations/NNNN_*.sql` ainda não registrados em
`inbound.schema_migrations`. Cada arquivo roda em sua própria transação junto com o registro
da versão, então rodar o runner de novo não reaplica nem duplica nada.

Uso: `python -m issue_analyst_api.migrate` (lê `DATABASE_URL` do ambiente).
"""

import re
import sys
from pathlib import Path

import psycopg
import structlog

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"
_FILE_PATTERN = re.compile(r"^(\d{4})_.+\.sql$")
# Chave arbitrária do advisory lock que serializa execuções concorrentes do runner.
_LOCK_KEY = 7_311_204_001

_BOOTSTRAP_SQL = """
CREATE SCHEMA IF NOT EXISTS inbound;
CREATE TABLE IF NOT EXISTS inbound.schema_migrations (
  version     text PRIMARY KEY,
  applied_at  timestamptz NOT NULL DEFAULT now()
);
"""


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, Path]]:
    """Lista `(versão, arquivo)` das migrações do diretório, em ordem de versão."""
    found: list[tuple[str, Path]] = []
    for path in directory.iterdir():
        match = _FILE_PATTERN.match(path.name)
        if match:
            found.append((match.group(1), path))
    found.sort()
    versions = [version for version, _ in found]
    if len(versions) != len(set(versions)):
        raise ValueError(f"duplicate migration versions in {directory}")
    return found


def apply_migrations(database_url: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Aplica as migrações pendentes e devolve as versões aplicadas nesta execução."""
    log = structlog.get_logger()
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            with conn.transaction():
                conn.execute(_BOOTSTRAP_SQL)
            done = {row[0] for row in conn.execute("SELECT version FROM inbound.schema_migrations")}
            for version, path in discover_migrations(directory):
                if version in done:
                    continue
                with conn.transaction():
                    conn.execute(path.read_text(encoding="utf-8"))
                    conn.execute(
                        "INSERT INTO inbound.schema_migrations (version) VALUES (%s)", (version,)
                    )
                log.info("migration applied", version=version, file=path.name)
                applied.append(version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    log.info("migrations up to date", applied=applied)
    return applied


def main() -> None:
    from issue_analyst_api.config import Settings
    from issue_analyst_api.logging import configure_logging

    settings = Settings()
    configure_logging(settings)
    try:
        apply_migrations(settings.DATABASE_URL)
    except Exception:
        structlog.get_logger().exception("migration failed")
        sys.exit(1)


if __name__ == "__main__":
    main()
