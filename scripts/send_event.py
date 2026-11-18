"""Envia ao serviço um evento `issues` no formato do webhook do GitHub, assinado com HMAC.

Monta o payload de `opened` ou `edited`, assina o corpo com `GITHUB_WEBHOOK_SECRET` (lido do
ambiente) e faz `POST` em `/webhooks/github`. Só usa a biblioteca padrão e nunca chama o GitHub.

Exemplo:
    uv run python scripts/send_event.py --action opened --repository equipe/projeto-teste \
        --number 42 --title "Erro ao salvar" --description "Passos..." --delivery-id evt-001
"""

import argparse
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import UTC, datetime

DEFAULT_URL = "http://localhost:8000/webhooks/github"
SECRET_ENV = "GITHUB_WEBHOOK_SECRET"


def build_payload(
    action: str,
    repository: str,
    number: int,
    title: str,
    description: str | None,
    *,
    now: datetime | None = None,
) -> dict:
    """Monta o corpo de um evento `issues` do GitHub com os campos que o serviço lê."""
    timestamp = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
    owner, _, name = repository.partition("/")
    return {
        "action": action,
        "issue": {
            "number": number,
            "title": title,
            "body": description,
            "state": "open",
            "created_at": timestamp,
            "updated_at": timestamp,
            "user": {"login": "send-event-script", "type": "User"},
        },
        "repository": {"full_name": repository, "name": name, "owner": {"login": owner}},
        "sender": {"login": "send-event-script", "type": "User"},
    }


def sign(secret: str, body: bytes) -> str:
    """Valor do header `X-Hub-Signature-256` (`sha256=<hex>`) para o corpo."""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def build_request(url: str, secret: str, delivery_id: str, payload: dict) -> urllib.request.Request:
    """Requisição `POST` com os headers que o GitHub envia em um evento `issues`."""
    body = json.dumps(payload).encode()
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "send-event-script",
        "X-GitHub-Event": "issues",
        "X-GitHub-Delivery": delivery_id,
        "X-Hub-Signature-256": sign(secret, body),
    }
    return urllib.request.Request(url, data=body, headers=headers, method="POST")


def send(request: urllib.request.Request, timeout_s: float = 30.0) -> tuple[int, str]:
    """Envia a requisição e devolve o código HTTP e o corpo da resposta, mesmo em 4xx e 5xx."""
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--action", choices=["opened", "edited"], required=True)
    parser.add_argument("--repository", required=True, help="no formato owner/name")
    parser.add_argument("--number", type=int, required=True, help="número da issue")
    parser.add_argument("--title", required=True)
    parser.add_argument(
        "--description", default=None, help="corpo da issue; omitido envia body nulo"
    )
    parser.add_argument("--delivery-id", required=True, help="valor de X-GitHub-Delivery")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"padrão: {DEFAULT_URL}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    secret = os.environ.get(SECRET_ENV)
    if not secret:
        print(f"defina a variável de ambiente {SECRET_ENV}", file=sys.stderr)
        return 2
    payload = build_payload(args.action, args.repository, args.number, args.title, args.description)
    try:
        status, body = send(build_request(args.url, secret, args.delivery_id, payload))
    except urllib.error.URLError as error:
        print(f"falha ao conectar em {args.url}: {error.reason}", file=sys.stderr)
        return 2
    print(status)
    print(body)
    return 0 if status < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
