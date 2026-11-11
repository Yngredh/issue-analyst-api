from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from issue_analyst_api.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Cria a aplicação HTTP do serviço.

    O lifespan ainda é vazio; as próximas etapas abrem nele o pool do banco e o relay.
    """
    app_settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = app_settings
        yield

    app = FastAPI(title=app_settings.SERVICE_NAME, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Responde se o processo está de pé, sem tocar banco nem broker."""
        return {"status": "ok"}

    return app
