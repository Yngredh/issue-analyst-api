"""Dados de teste da view `inbound.issue_presentation` (RF27, D16).

Grava linhas nas tabelas do stub de `triage` e `publisher` (D15), fazendo o papel do
`issue-triage` e do `issue-publisher`. Só os testes escrevem nesses schemas; o serviço só lê.
"""

import random
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

import psycopg

PUBLISHED_AT = datetime(2026, 10, 22, 12, 0, 9, tzinfo=UTC)


def unique_issue() -> tuple[str, int]:
    """Repositório e número únicos: o banco é compartilhado pela sessão."""
    return f"equipe/repo-{uuid.uuid4().hex[:8]}", random.randint(1, 1_000_000)


def insert_created(conn: psycopg.Connection, repository: str, number: int, event_id: str) -> None:
    conn.execute(
        """
        INSERT INTO inbound.issue_events
          (event_id, type, occurred_at, repository, issue_number, title, raw_payload, body_hash)
        VALUES (%s, 'issue.created', now(), %s, %s, 'Erro ao salvar', '{}'::jsonb, 'hash')
        """,
        (event_id, repository, number),
    )


def insert_case(
    conn: psycopg.Connection,
    repository: str,
    number: int,
    origin_event_id: str,
    status: str,
    status_reason: str,
    *,
    category: str | None = None,
    current_attempt: int = 1,
    manual_attempts: int = 0,
) -> int:
    row = conn.execute(
        """
        INSERT INTO triage.issue_cases
          (repository, issue_number, origin_event_id, status, status_reason, category,
           current_attempt, manual_attempts)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            repository,
            number,
            origin_event_id,
            status,
            status_reason,
            category,
            current_attempt,
            manual_attempts,
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def insert_attempt(
    conn: psycopg.Connection, case_id: int, event_id: str, attempt: int, state: str = "DONE"
) -> int:
    row = conn.execute(
        """
        INSERT INTO triage.analysis_attempts (case_id, event_id, attempt, trigger, state)
        VALUES (%s, %s, %s, 'initial', %s)
        RETURNING id
        """,
        (case_id, event_id, attempt, state),
    ).fetchone()
    assert row is not None
    return row[0]


def insert_proposal(
    conn: psycopg.Connection,
    attempt_id: int,
    *,
    valid: bool = True,
    summary: str = "Erro 500 no envio do login.",
) -> int:
    row = conn.execute(
        """
        INSERT INTO triage.proposals
          (attempt_id, kind, specialist, category, summary, next_step, conclusion,
           conclusion_reason, validation_status, validation_error, comment_body)
        VALUES (%s, 'triage', 'bug', 'bug', %s, 'Coletar o log do erro.', 'procede',
                'O relato descreve um erro reproduzível.', %s, %s, %s)
        RETURNING id
        """,
        (
            attempt_id,
            summary,
            "VALID" if valid else "INVALID",
            None if valid else "missing next_step",
            "comentário" if valid else None,
        ),
    ).fetchone()
    assert row is not None
    return row[0]


def insert_publication(
    conn: psycopg.Connection,
    repository: str,
    number: int,
    origin_event_id: str,
    revision: int,
    proposal_id: int,
    state: str,
) -> str | None:
    """Grava a publicação; devolve a URL do comentário quando `PUBLISHED`."""
    published = state == "PUBLISHED"
    comment_url = (
        f"https://github.com/{repository}/issues/{number}#issuecomment-1" if published else None
    )
    conn.execute(
        """
        INSERT INTO publisher.publications
          (repository, issue_number, revision, proposal_id, origin_event_id, kind, state,
           failure_kind, github_comment_id, github_comment_url, published_at)
        VALUES (%s, %s, %s, %s, %s, 'triage', %s, %s, %s, %s, %s)
        """,
        (
            repository,
            number,
            revision,
            proposal_id,
            origin_event_id,
            state,
            "permanent" if state == "FAILED" else None,
            1 if published else None,
            comment_url,
            PUBLISHED_AT if published else None,
        ),
    )
    return comment_url


# Cada cenário grava um estado da view para `(repository, number, event_id)` já com o
# `issue.created` gravado.
Scenario = Callable[[psycopg.Connection, str, int, str], None]


def accepted(conn: psycopg.Connection, repository: str, number: int, event_id: str) -> None:
    """Issue aceita, sem caso no `triage` (análise ainda não feita)."""


def analyzing(conn: psycopg.Connection, repository: str, number: int, event_id: str) -> None:
    case_id = insert_case(conn, repository, number, event_id, "ANALYZING", "initial")
    insert_attempt(conn, case_id, event_id, 1, state="IN_PROGRESS")


def published(conn: psycopg.Connection, repository: str, number: int, event_id: str) -> None:
    case_id = insert_case(
        conn,
        repository,
        number,
        event_id,
        "READY_TO_PUBLISH",
        "proposal_validated",
        category="bug",
    )
    proposal_id = insert_proposal(conn, insert_attempt(conn, case_id, event_id, 1))
    insert_publication(conn, repository, number, event_id, 1, proposal_id, "PUBLISHED")


def publication_failed(
    conn: psycopg.Connection, repository: str, number: int, event_id: str
) -> None:
    """Falha de publicação da revisão atual: vira `NEEDS_REVIEW (technical_failure)` (D16)."""
    case_id = insert_case(
        conn,
        repository,
        number,
        event_id,
        "READY_TO_PUBLISH",
        "proposal_validated",
        category="bug",
    )
    proposal_id = insert_proposal(conn, insert_attempt(conn, case_id, event_id, 1))
    insert_publication(conn, repository, number, event_id, 1, proposal_id, "FAILED")


def old_publication_failed(
    conn: psycopg.Connection, repository: str, number: int, event_id: str
) -> None:
    """Falha de publicação de uma revisão antiga: o status do caso não muda (D16)."""
    case_id = insert_case(
        conn,
        repository,
        number,
        event_id,
        "READY_TO_PUBLISH",
        "proposal_validated",
        category="bug",
        current_attempt=2,
    )
    old_proposal_id = insert_proposal(conn, insert_attempt(conn, case_id, event_id, 1))
    insert_publication(conn, repository, number, event_id, 1, old_proposal_id, "FAILED")
    insert_proposal(conn, insert_attempt(conn, case_id, event_id, 2), summary="Revisão 2.")


def insufficient_content(
    conn: psycopg.Connection, repository: str, number: int, event_id: str
) -> None:
    """`NEEDS_REVIEW (insufficient_content)` sem proposta válida."""
    case_id = insert_case(
        conn,
        repository,
        number,
        event_id,
        "NEEDS_REVIEW",
        "insufficient_content",
        category="indeterminado",
    )
    insert_attempt(conn, case_id, event_id, 1)


def max_retries(conn: psycopg.Connection, repository: str, number: int, event_id: str) -> None:
    case_id = insert_case(
        conn,
        repository,
        number,
        event_id,
        "MAX_RETRIES_REACHED",
        "manual_attempts_exhausted",
        category="indeterminado",
        current_attempt=4,
        manual_attempts=3,
    )
    for attempt in range(1, 5):
        insert_attempt(conn, case_id, event_id, attempt)


def latest_valid_proposal(
    conn: psycopg.Connection, repository: str, number: int, event_id: str
) -> None:
    """Tentativas 1 e 2 com proposta válida e 3 com inválida: aparece só a da tentativa 2."""
    case_id = insert_case(
        conn,
        repository,
        number,
        event_id,
        "NEEDS_REVIEW",
        "invalid_proposal",
        category="bug",
        current_attempt=3,
    )
    insert_proposal(conn, insert_attempt(conn, case_id, event_id, 1), summary="Tentativa 1.")
    insert_proposal(conn, insert_attempt(conn, case_id, event_id, 2), summary="Tentativa 2.")
    insert_proposal(
        conn, insert_attempt(conn, case_id, event_id, 3), valid=False, summary="Tentativa 3."
    )
