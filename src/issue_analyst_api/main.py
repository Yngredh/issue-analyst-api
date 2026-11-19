import asyncio
import sys

import structlog
import uvicorn

from issue_analyst_api.api.app import create_app
from issue_analyst_api.config import Settings
from issue_analyst_api.logging import configure_logging


def main() -> None:
    settings = Settings()
    configure_logging(settings)
    structlog.get_logger().info(
        "service starting", log_level=settings.LOG_LEVEL, http_port=settings.HTTP_PORT
    )
    config = uvicorn.Config(
        create_app(settings),
        host="0.0.0.0",
        port=settings.HTTP_PORT,
        log_level=settings.LOG_LEVEL.lower(),
    )
    server = uvicorn.Server(config)
    if sys.platform == "win32":
        # O psycopg assíncrono não funciona com o ProactorEventLoop, padrão no Windows.
        asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)
    else:
        server.run()


if __name__ == "__main__":
    main()
