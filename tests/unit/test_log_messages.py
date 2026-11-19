"""Mensagens amigáveis e campos de origem dos logs (RNF14)."""

import json

import pytest
import structlog
from pydantic import SecretStr

from issue_analyst_api.api.webhooks import Decision
from issue_analyst_api.config import Settings
from issue_analyst_api.log_messages import MAIN_FLOW_EVENTS, MESSAGES, add_message
from issue_analyst_api.logging import configure_logging


@pytest.mark.parametrize("event", MAIN_FLOW_EVENTS)
def test_every_main_flow_event_has_a_friendly_message(event: str) -> None:
    assert MESSAGES[event].strip()
    assert MESSAGES[event] != event


def test_main_flow_covers_every_webhook_decision() -> None:
    assert {f"webhook_response:{decision.value}" for decision in Decision} <= set(MAIN_FLOW_EVENTS)


def test_add_message_uses_dictionary_unknown_event_name_and_explicit_message() -> None:
    known = add_message(None, "info", {"event": "relay_broker_connected"})
    assert known["message"] == MESSAGES["relay_broker_connected"]
    assert add_message(None, "info", {"event": "algo_novo"})["message"] == "algo_novo"
    explicit = add_message(
        None, "info", {"event": "relay_broker_connected", "message": "meu texto"}
    )
    assert explicit["message"] == "meu texto"


@pytest.mark.parametrize("decision", [decision.value for decision in Decision])
def test_webhook_response_message_follows_the_decision(decision: str) -> None:
    entry = add_message(None, "info", {"event": "webhook_response", "decision": decision})
    assert entry["message"] == MESSAGES[f"webhook_response:{decision}"]


def test_webhook_response_without_known_decision_falls_back_to_event_name() -> None:
    assert add_message(None, "info", {"event": "webhook_response"})["message"] == "webhook_response"


def _emit_relay_log() -> None:
    structlog.get_logger().info("relay_message_published", outboxId=1)


def test_configured_logger_emits_module_func_name_and_message(
    capsys: pytest.CaptureFixture[str],
) -> None:
    try:
        settings = Settings(
            GITHUB_WEBHOOK_SECRET=SecretStr("x"),
            DATABASE_URL="postgresql://nobody:nothing@127.0.0.1:1/none",
            RABBITMQ_URL="amqp://nobody:nothing@127.0.0.1:1/",
        )
        configure_logging(settings)
        _emit_relay_log()
        line = json.loads(capsys.readouterr().out.strip())
    finally:
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()

    assert line["module"] == "test_log_messages"
    assert line["func_name"] == "_emit_relay_log"
    assert line["message"] == MESSAGES["relay_message_published"]
    assert line["event"] == "relay_message_published"
    assert line["service"] == "issue-analyst-api"
    assert line["level"] == "info"
    assert line["outboxId"] == 1
