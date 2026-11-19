"""Leitura do resultado da issue na view `inbound.issue_presentation` (RF27, D14, RNF07).

Só leitura: a view combina `inbound`, `triage` e `publisher` (status combinado da D16) e este
adaptador nunca escreve em nenhuma tabela.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from issue_analyst_api.adapters.issue_events import DatabaseUnavailableError

_SELECT_PRESENTATION = """
SELECT repository, issue_number, event_id, status, status_reason, category, current_attempt,
       manual_attempts, specialist, summary, next_step, conclusion, conclusion_reason, answer,
       publication_state, github_comment_url, published_at
FROM inbound.issue_presentation
WHERE repository = %(repository)s AND issue_number = %(issue_number)s
"""


@dataclass(frozen=True)
class ProposalView:
    """Proposta válida mais recente do caso."""

    specialist: str | None
    summary: str | None
    next_step: str | None
    conclusion: str | None
    conclusion_reason: str | None
    answer: str | None


@dataclass(frozen=True)
class PublicationView:
    """Publicação mais recente da issue."""

    state: str
    comment_url: str | None
    published_at: datetime | None


@dataclass(frozen=True)
class IssuePresentation:
    """Linha da view `inbound.issue_presentation`."""

    repository: str
    issue_number: int
    event_id: str
    status: str
    status_reason: str
    category: str | None
    attempt: int | None
    manual_attempts: int | None
    proposal: ProposalView | None
    publication: PublicationView | None


def _to_utc(value: datetime | None) -> datetime | None:
    return None if value is None else value.astimezone(UTC)


def _from_row(row: dict) -> IssuePresentation:
    proposal_fields = (
        row["specialist"],
        row["summary"],
        row["next_step"],
        row["conclusion"],
        row["conclusion_reason"],
        row["answer"],
    )
    # A view não expõe o id da proposta: sem proposta válida, todas as colunas vêm nulas.
    proposal = (
        None if all(value is None for value in proposal_fields) else ProposalView(*proposal_fields)
    )
    # `publications.state` é NOT NULL: nulo na view significa que não há publicação.
    publication = (
        None
        if row["publication_state"] is None
        else PublicationView(
            state=row["publication_state"],
            comment_url=row["github_comment_url"],
            published_at=_to_utc(row["published_at"]),
        )
    )
    return IssuePresentation(
        repository=row["repository"],
        issue_number=row["issue_number"],
        event_id=row["event_id"],
        status=row["status"],
        status_reason=row["status_reason"],
        category=row["category"],
        attempt=row["current_attempt"],
        manual_attempts=row["manual_attempts"],
        proposal=proposal,
        publication=publication,
    )


async def fetch_issue_presentation(
    pool: AsyncConnectionPool, repository: str, issue_number: int
) -> IssuePresentation | None:
    """Lê o resultado da issue; `None` se não há `issue.created` aceito para ela.

    Levanta `DatabaseUnavailableError` se o banco não estiver acessível (inclui a espera de
    `CONNECTION_TIMEOUT_S` por uma conexão do pool).
    """
    try:
        async with pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    _SELECT_PRESENTATION,
                    {"repository": repository, "issue_number": issue_number},
                )
                row = await cursor.fetchone()
    except psycopg.OperationalError as exc:  # inclui o PoolTimeout do psycopg_pool
        raise DatabaseUnavailableError from exc
    return None if row is None else _from_row(row)
