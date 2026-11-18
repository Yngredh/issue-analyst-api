"""View `inbound.issue_presentation` (RF27, RNF07, D16) sobre o stub de `triage` e `publisher`."""

import uuid
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from tests.integration import presentation_data as data


def _view_rows(conn: psycopg.Connection, repository: str, number: int) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT * FROM inbound.issue_presentation WHERE repository = %s AND issue_number = %s",
            (repository, number),
        )
        return cursor.fetchall()


def _single_row(
    conn: psycopg.Connection, scenario: data.Scenario
) -> tuple[dict[str, Any], str, int, str]:
    repository, number = data.unique_issue()
    event_id = f"evt-{uuid.uuid4()}"
    data.insert_created(conn, repository, number, event_id)
    scenario(conn, repository, number, event_id)
    rows = _view_rows(conn, repository, number)
    assert len(rows) == 1
    return rows[0], repository, number, event_id


@pytest.mark.parametrize(
    ("scenario", "status", "status_reason"),
    [
        (data.accepted, "ACCEPTED", "awaiting_analysis"),
        (data.analyzing, "ANALYZING", "initial"),
        (data.published, "READY_TO_PUBLISH", "proposal_validated"),
        (data.publication_failed, "NEEDS_REVIEW", "technical_failure"),
        (data.old_publication_failed, "READY_TO_PUBLISH", "proposal_validated"),
        (data.insufficient_content, "NEEDS_REVIEW", "insufficient_content"),
        (data.max_retries, "MAX_RETRIES_REACHED", "manual_attempts_exhausted"),
        (data.latest_valid_proposal, "NEEDS_REVIEW", "invalid_proposal"),
    ],
)
def test_combined_status(
    db_conn: psycopg.Connection, scenario: data.Scenario, status: str, status_reason: str
) -> None:
    row, _, _, _ = _single_row(db_conn, scenario)

    assert (row["status"], row["status_reason"]) == (status, status_reason)


def test_accepted_issue_without_case(db_conn: psycopg.Connection) -> None:
    row, repository, number, event_id = _single_row(db_conn, data.accepted)

    assert row == {
        "repository": repository,
        "issue_number": number,
        "event_id": event_id,
        "status": "ACCEPTED",
        "status_reason": "awaiting_analysis",
        "category": None,
        "current_attempt": None,
        "manual_attempts": None,
        "specialist": None,
        "summary": None,
        "next_step": None,
        "conclusion": None,
        "conclusion_reason": None,
        "answer": None,
        "publication_state": None,
        "github_comment_url": None,
        "published_at": None,
    }


def test_analyzing_case_without_proposal(db_conn: psycopg.Connection) -> None:
    row, _, _, _ = _single_row(db_conn, data.analyzing)

    assert row["current_attempt"] == 1
    assert row["manual_attempts"] == 0
    assert row["summary"] is None
    assert row["publication_state"] is None


def test_published_proposal_with_url_and_date(db_conn: psycopg.Connection) -> None:
    row, repository, number, _ = _single_row(db_conn, data.published)

    assert row["category"] == "bug"
    assert row["specialist"] == "bug"
    assert row["summary"] == "Erro 500 no envio do login."
    assert row["conclusion"] == "procede"
    assert row["publication_state"] == "PUBLISHED"
    assert (
        row["github_comment_url"]
        == f"https://github.com/{repository}/issues/{number}#issuecomment-1"
    )
    assert row["published_at"] == data.PUBLISHED_AT


def test_failed_publication_of_current_revision_needs_review(db_conn: psycopg.Connection) -> None:
    # D16: `FAILED` com `revision = current_attempt` e caso em READY_TO_PUBLISH.
    row, _, _, _ = _single_row(db_conn, data.publication_failed)

    assert (row["status"], row["status_reason"]) == ("NEEDS_REVIEW", "technical_failure")
    assert row["publication_state"] == "FAILED"
    assert row["summary"] == "Erro 500 no envio do login."
    # Só leitura: o caso continua gravado como READY_TO_PUBLISH (sem escrita cruzada).
    case = db_conn.execute(
        "SELECT status, status_reason FROM triage.issue_cases WHERE origin_event_id = %s",
        (row["event_id"],),
    ).fetchone()
    assert case == ("READY_TO_PUBLISH", "proposal_validated")


def test_failed_publication_of_old_revision_keeps_status(db_conn: psycopg.Connection) -> None:
    row, _, _, _ = _single_row(db_conn, data.old_publication_failed)

    assert (row["status"], row["status_reason"]) == ("READY_TO_PUBLISH", "proposal_validated")
    assert row["current_attempt"] == 2
    assert row["summary"] == "Revisão 2."


def test_failed_publication_outside_ready_to_publish_keeps_status(
    db_conn: psycopg.Connection,
) -> None:
    def scenario(conn: psycopg.Connection, repository: str, number: int, event_id: str) -> None:
        case_id = data.insert_case(conn, repository, number, event_id, "ANALYZING", "reprocess")
        proposal_id = data.insert_proposal(conn, data.insert_attempt(conn, case_id, event_id, 1))
        data.insert_publication(conn, repository, number, event_id, 1, proposal_id, "FAILED")

    row, _, _, _ = _single_row(db_conn, scenario)

    assert (row["status"], row["status_reason"]) == ("ANALYZING", "reprocess")


def test_insufficient_content_without_proposal(db_conn: psycopg.Connection) -> None:
    row, _, _, _ = _single_row(db_conn, data.insufficient_content)

    assert row["category"] == "indeterminado"
    assert all(
        row[column] is None
        for column in (
            "specialist",
            "summary",
            "next_step",
            "conclusion",
            "conclusion_reason",
            "answer",
        )
    )


def test_max_retries_reached(db_conn: psycopg.Connection) -> None:
    row, _, _, _ = _single_row(db_conn, data.max_retries)

    assert row["current_attempt"] == 4
    assert row["manual_attempts"] == 3


def test_only_latest_valid_proposal_is_shown(db_conn: psycopg.Connection) -> None:
    row, _, _, _ = _single_row(db_conn, data.latest_valid_proposal)

    assert row["summary"] == "Tentativa 2."


def test_repeated_created_and_edits_do_not_duplicate_row(db_conn: psycopg.Connection) -> None:
    repository, number = data.unique_issue()
    first_event_id = f"evt-{uuid.uuid4()}"
    data.insert_created(db_conn, repository, number, first_event_id)
    # Segundo `issue.created` da mesma issue: barrado pela entrada (ON CONFLICT DO NOTHING).
    db_conn.execute(
        """
        INSERT INTO inbound.issue_events
          (event_id, type, occurred_at, repository, issue_number, title, raw_payload, body_hash)
        VALUES (%s, 'issue.created', now(), %s, %s, 'De novo', '{}'::jsonb, 'hash')
        ON CONFLICT DO NOTHING
        """,
        (f"evt-{uuid.uuid4()}", repository, number),
    )
    for _ in range(2):
        db_conn.execute(
            """
            INSERT INTO inbound.issue_events
              (event_id, type, occurred_at, repository, issue_number, title, raw_payload,
               body_hash)
            VALUES (%s, 'issue.edited', now(), %s, %s, 'Editada', '{}'::jsonb, 'hash')
            """,
            (f"evt-{uuid.uuid4()}", repository, number),
        )

    rows = _view_rows(db_conn, repository, number)

    assert len(rows) == 1
    assert rows[0]["event_id"] == first_event_id


def test_edit_without_created_is_not_shown(db_conn: psycopg.Connection) -> None:
    repository, number = data.unique_issue()
    db_conn.execute(
        """
        INSERT INTO inbound.issue_events
          (event_id, type, occurred_at, repository, issue_number, title, raw_payload, body_hash)
        VALUES (%s, 'issue.edited', now(), %s, %s, 'Editada', '{}'::jsonb, 'hash')
        """,
        (f"evt-{uuid.uuid4()}", repository, number),
    )

    assert _view_rows(db_conn, repository, number) == []
