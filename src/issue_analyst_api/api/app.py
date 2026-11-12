from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from issue_analyst_api.adapters.database import close_pool, create_pool, open_pool
from issue_analyst_api.adapters.outbox_relay import OutboxRelay
from issue_analyst_api.api import webhooks
from issue_analyst_api.config import Settings


def create_app(settings: Settings | None = None) -> FastAPI:
    """Cria a aplicação HTTP do serviço.

    O lifespan abre o pool do banco e inicia o relay da outbox em segundo plano na subida; no
    encerramento, para o relay e fecha o pool.
    """
    app_settings = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = app_settings
        pool = create_pool(app_settings.DATABASE_URL)
        await open_pool(pool)
        app.state.db_pool = pool
        relay = OutboxRelay(
            pool,
            app_settings.RABBITMQ_URL,
            poll_interval_s=app_settings.RELAY_POLL_INTERVAL_S,
            batch_size=app_settings.RELAY_BATCH_SIZE,
        )
        relay.start()
        app.state.outbox_relay = relay
        try:
            yield
        finally:
            await relay.stop()
            await close_pool(pool)

    app = FastAPI(title=app_settings.SERVICE_NAME, lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Responde se o processo está de pé, sem tocar banco nem broker."""
        return {"status": "ok"}

    app.include_router(webhooks.router)

    return app
