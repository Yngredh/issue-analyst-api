"""Mensagens amigáveis dos logs do fluxo principal (RNF14).

`add_message` é um processador do structlog: preenche `message` com um texto fixo em português a
partir do `event`, sem editar cada chamada de log. O dado variável continua nos campos do JSON; a
mensagem nunca leva segredo, corpo bruto, título nem descrição da issue.
"""

from typing import Any

MESSAGES: dict[str, str] = {
    "webhook_response:accepted": "Webhook recebido: issue aceita e registrada para análise",
    "webhook_response:duplicate": "Webhook recebido: entrega duplicada, nada novo foi registrado",
    "webhook_response:ignored": "Webhook recebido e ignorado: evento não tratado pelo sistema",
    "webhook_response:rejected": "Webhook recusado: assinatura, tamanho ou conteúdo inválido",
    "relay_message_published": "Mensagem enviada para a fila de análise",
    "relay_broker_connected": "Relay conectado ao RabbitMQ",
    "relay_broker_connection_failed": "Falha ao conectar no RabbitMQ: nova tentativa com espera",
    "relay_broker_connection_resumed": "Conexão com o RabbitMQ retomada",
    "relay_database_unavailable": "Banco indisponível para o relay: nova tentativa com espera",
    "relay_database_resumed": "Conexão com o banco retomada pelo relay",
    "duplicate_mismatch": "Mesmo evento reenviado com conteúdo diferente: mantido o primeiro",
    "occurred_at_fallback": "Data do evento ausente ou inválida: usado o horário de recebimento",
}

MAIN_FLOW_EVENTS: tuple[str, ...] = tuple(MESSAGES)


def add_message(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    """Acrescenta `message` ao log, sem sobrescrever um `message` passado na chamada."""
    event = event_dict["event"]
    message = None
    if event == "webhook_response":
        message = MESSAGES.get(f"webhook_response:{event_dict.get('decision')}")
    if message is None:
        message = MESSAGES.get(event, event)
    event_dict.setdefault("message", message)
    return event_dict
