"""`GET /issues/{owner}/{repo}/{number}` (RF27, D14) com PostgreSQL real e o stub de `triage` e
`publisher` (D15)."""

import hashlib
import hmac
import json
import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from issue_analyst_api.api.app import create_app
from issue_analyst_api.config import Settings
from tests.integration import presentation_data as data

SECRET = "issue-result-test-secret"

_PROPOSAL = {
    "specialist": "bug",
    "summary": "Erro 500 no envio do login.",
    "nextStep": "Coletar o log do erro.",
    "conclusion": "procede",
    "conclusionReason": "O relato descreve um erro reproduzível.",
    "answer": None,
}


def _settings(database_url: str) -> Settings:
    return Settings(
        GITHUB_WEBHOOK_SECRET=SecretStr(SECRET),
        DATABASE_URL=database_url,
        RABBITMQ_URL="amqp://nobody:nothing@127.0.0.1:1/",
    )


@pytest.fixture
def client(database_url: str) -> Iterator[TestClient]:
    with TestClient(create_app(_settings(database_url))) as test_client:
        yield test_client


def _expected(
    repository: str, number: int, event_id: str, scenario: data.Scenario
) -> dict[str, Any]:
    """Corpo exato de "Contratos > Consulta" para cada cenário."""
    base: dict[str, Any] = {"repository": repository, "issueNumber": number, "eventId": event_id}
    comment_url = f"https://github.com/{repository}/issues/{number}#issuecomment-1"
    bodies: dict[data.Scenario, dict[str, Any]] = {
        data.accepted: {
            "status": "ACCEPTED",
            "statusReason": "awaiting_analysis",
            "category": None,
            "attempt": None,
            "manualAttempts": None,
            "proposal": None,
            "publication": None,
        },
        data.analyzing: {
            "status": "ANALYZING",
            "statusReason": "initial",
            "category": None,
            "attempt": 1,
            "manualAttempts": 0,
            "proposal": None,
            "publication": None,
        },
        data.published: {
            "status": "READY_TO_PUBLISH",
            "statusReason": "proposal_validated",
            "category": "bug",
            "attempt": 1,
            "manualAttempts": 0,
            "proposal": _PROPOSAL,
            "publication": {
                "state": "PUBLISHED",
                "commentUrl": comment_url,
                "publishedAt": "2026-10-22T12:00:09Z",
            },
        },
        data.publication_failed: {
            "status": "NEEDS_REVIEW",
            "statusReason": "technical_failure",
            "category": "bug",
            "attempt": 1,
            "manualAttempts": 0,
            "proposal": _PROPOSAL,
            "publication": {"state": "FAILED", "commentUrl": None, "publishedAt": None},
        },
        data.old_publication_failed: {
            "status": "READY_TO_PUBLISH",
            "statusReason": "proposal_validated",
            "category": "bug",
            "attempt": 2,
            "manualAttempts": 0,
            "proposal": {**_PROPOSAL, "summary": "Revisão 2."},
            "publication": {"state": "FAILED", "commentUrl": None, "publishedAt": None},
        },
        data.insufficient_content: {
            "status": "NEEDS_REVIEW",
            "statusReason": "insufficient_content",
            "category": "indeterminado",
            "attempt": 1,
            "manualAttempts": 0,
            "proposal": None,
            "publication": None,
        },
        data.max_retries: {
            "status": "MAX_RETRIES_REACHED",
            "statusReason": "manual_attempts_exhausted",
            "category": "indeterminado",
            "attempt": 4,
            "manualAttempts": 3,
            "proposal": None,
            "publication": None,
        },
        data.latest_valid_proposal: {
            "status": "NEEDS_REVIEW",
            "statusReason": "invalid_proposal",
            "category": "bug",
            "attempt": 3,
            "manualAttempts": 0,
            "proposal": {**_PROPOSAL, "summary": "Tentativa 2."},
            "publication": None,
        },
    }
    return {**base, **bodies[scenario]}


@pytest.mark.parametrize(
    "scenario",
    [
        data.accepted,
        data.analyzing,
        data.published,
        data.publication_failed,
        data.old_publication_failed,
        data.insufficient_content,
        data.max_retries,
        data.latest_valid_proposal,
    ],
    ids=lambda scenario: scenario.__name__,
)
def test_exact_body_for_each_state(
    client: TestClient, database_url: str, scenario: data.Scenario
) -> None:
    repository, number = data.unique_issue()
    event_id = f"evt-{uuid.uuid4()}"
    # O app lê por outra conexão: os dados precisam estar commitados.
    with psycopg.connect(database_url) as conn:
        data.insert_created(conn, repository, number, event_id)
        scenario(conn, repository, number, event_id)

    response = client.get(f"/issues/{repository}/{number}")

    assert response.status_code == 200
    assert response.json() == _expected(repository, number, event_id, scenario)


def test_unknown_issue_returns_404(client: TestClient) -> None:
    repository, number = data.unique_issue()

    response = client.get(f"/issues/{repository}/{number}")

    assert response.status_code == 404
    assert response.json() == {"error": "not_found"}


@pytest.mark.parametrize("number", ["0", "-1", "abc", "1.5"])
def test_invalid_number_returns_422(client: TestClient, number: str) -> None:
    response = client.get(f"/issues/equipe/projeto-teste/{number}")

    assert response.status_code == 422


def test_database_down_returns_503() -> None:
    settings = _settings("postgresql://nobody:nothing@127.0.0.1:1/none")

    with TestClient(create_app(settings)) as down_client:
        response = down_client.get("/issues/equipe/projeto-teste/42")

    assert response.status_code == 503
    assert response.json() == {"error": "unavailable"}


def test_result_link_from_webhook_leads_to_accepted(client: TestClient) -> None:
    repository, number = data.unique_issue()
    event_id = f"evt-{uuid.uuid4()}"
    body = json.dumps(
        {
            "action": "opened",
            "issue": {
                "number": number,
                "title": "Erro ao salvar",
                "body": "Passos para reproduzir",
                "created_at": "2026-11-10T12:00:00Z",
            },
            "repository": {"full_name": repository},
            "sender": {"type": "User", "login": "alguem"},
        }
    ).encode()
    signature = "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()

    accepted = client.post(
        "/webhooks/github",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": signature,
            "X-GitHub-Event": "issues",
            "X-GitHub-Delivery": event_id,
        },
    )
    assert accepted.status_code == 202

    response = client.get(accepted.json()["links"]["result"])

    assert response.status_code == 200
    assert response.json() == _expected(repository, number, event_id, data.accepted)
