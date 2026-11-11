import structlog

from issue_analyst_api.config import Settings
from issue_analyst_api.logging import configure_logging


def main() -> None:
    settings = Settings()
    configure_logging(settings)
    structlog.get_logger().info("service started", log_level=settings.LOG_LEVEL)


if __name__ == "__main__":
    main()
