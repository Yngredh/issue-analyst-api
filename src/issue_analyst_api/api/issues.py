"""Consulta do resultado da issue por repositório e número (RF27, D14).

Somente leitura e sem autenticação; é o endereço de `links.result` do 202 do webhook (D9).
"""

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Path, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from issue_analyst_api.adapters.issue_events import DatabaseUnavailableError
from issue_analyst_api.adapters.issue_presentation import (
    IssuePresentation,
    fetch_issue_presentation,
)

router = APIRouter()


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ProposalResponse(_CamelModel):
    """Proposta válida mais recente do caso."""

    specialist: str | None
    summary: str | None
    next_step: str | None
    conclusion: str | None
    conclusion_reason: str | None
    answer: str | None


class PublicationResponse(_CamelModel):
    """Publicação mais recente da issue."""

    state: str
    comment_url: str | None
    published_at: datetime | None


class IssueResultResponse(_CamelModel):
    """Corpo de "Contratos > Consulta"; `status` e `statusReason` já vêm combinados (D16)."""

    repository: str
    issue_number: int
    event_id: str
    status: str
    status_reason: str
    category: str | None
    attempt: int | None
    manual_attempts: int | None
    proposal: ProposalResponse | None
    publication: PublicationResponse | None

    @classmethod
    def from_presentation(cls, item: IssuePresentation) -> "IssueResultResponse":
        proposal = item.proposal
        publication = item.publication
        return cls(
            repository=item.repository,
            issue_number=item.issue_number,
            event_id=item.event_id,
            status=item.status,
            status_reason=item.status_reason,
            category=item.category,
            attempt=item.attempt,
            manual_attempts=item.manual_attempts,
            proposal=(
                None
                if proposal is None
                else ProposalResponse(
                    specialist=proposal.specialist,
                    summary=proposal.summary,
                    next_step=proposal.next_step,
                    conclusion=proposal.conclusion,
                    conclusion_reason=proposal.conclusion_reason,
                    answer=proposal.answer,
                )
            ),
            publication=(
                None
                if publication is None
                else PublicationResponse(
                    state=publication.state,
                    comment_url=publication.comment_url,
                    published_at=publication.published_at,
                )
            ),
        )


@router.get(
    "/issues/{owner}/{repo}/{number}",
    response_model=IssueResultResponse,
    responses={404: {"description": "not_found"}, 503: {"description": "unavailable"}},
)
async def get_issue_result(
    request: Request,
    owner: str,
    repo: str,
    number: Annotated[int, Path(gt=0)],
) -> IssueResultResponse | JSONResponse:
    """Devolve o resultado da issue lido de `inbound.issue_presentation`, sem escrever nada.

    Sem linha na view → `404 not_found`; banco indisponível → `503 unavailable`.
    """
    try:
        item = await fetch_issue_presentation(request.app.state.db_pool, f"{owner}/{repo}", number)
    except DatabaseUnavailableError:
        return JSONResponse(status_code=503, content={"error": "unavailable"})
    if item is None:
        return JSONResponse(status_code=404, content={"error": "not_found"})
    return IssueResultResponse.from_presentation(item)
