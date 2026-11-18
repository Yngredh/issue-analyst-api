# issue-analyst-api

Recebe issues do GitHub, valida e encaminha para o processo de triagem.

## Pré-requisitos

- [uv](https://docs.astral.sh/uv/) (instala o Python 3.12 automaticamente, se necessário)
- Docker com Docker Compose (PostgreSQL e RabbitMQ locais e testes de integração)

## Configuração

Copie `.env.example` para `.env` e preencha os valores. São obrigatórios `DATABASE_URL`,
`RABBITMQ_URL` e `GITHUB_WEBHOOK_SECRET`; os demais têm padrão (`HTTP_PORT=8000`,
`WEBHOOK_MAX_BODY_BYTES=1048576`, `MAX_TITLE_CHARS=256`, `MAX_DESCRIPTION_CHARS=500`, `RELAY_POLL_INTERVAL_S=1`,
`RELAY_BATCH_SIZE=100`). Para o compose local, as URLs são:

```
DATABASE_URL=postgresql://postgres:postgres@localhost:5432/issue_analyst
RABBITMQ_URL=amqp://guest:guest@localhost:5672/
```

Nunca commite o `.env`.

## Subir a infraestrutura local

```bash
docker compose up -d      # PostgreSQL 16 (5432) e RabbitMQ (5672, painel em 15672)
docker compose down       # para os contêineres (use -v para apagar os dados)
```

## Migrar o banco

Com o PostgreSQL de pé, o `DATABASE_URL` no `.env` e as dependências instaladas (`uv sync`), aplique as migrações de `migrations/` (cria o
schema `inbound`; as já aplicadas ficam em `inbound.schema_migrations` e não rodam de novo):

```bash
uv run python -m issue_analyst_api.migrate
```

## Iniciar o serviço

```bash
uv sync                                   # instala as dependências
uv run python -m issue_analyst_api.main   # sobe o uvicorn na porta HTTP_PORT
curl http://localhost:8000/health         # {"status": "ok"}
```

Com a imagem Docker:

```bash
docker build -t issue-analyst-api .
docker run --env-file .env -p 8000:8000 issue-analyst-api
```

## Enviar eventos com o script

`scripts/send_event.py` faz o papel do GitHub: monta um evento `issues` (`opened` ou `edited`),
assina o corpo com HMAC SHA-256 usando `GITHUB_WEBHOOK_SECRET` **lido do ambiente** (o script não
lê o `.env`) e envia ao `POST /webhooks/github`. Imprime o código HTTP e o corpo da resposta. Não
chama o GitHub.

```bash
export GITHUB_WEBHOOK_SECRET=<o mesmo valor do .env>

# issue nova -> 202 accepted e uma mensagem na fila analysis
uv run python scripts/send_event.py --action opened --repository equipe/projeto-teste   --number 42 --title "Erro ao salvar" --description "Passos para reproduzir" --delivery-id evt-001

# mesma entrega de novo -> 200 duplicate (duplicate_event), sem nova mensagem
uv run python scripts/send_event.py --action opened --repository equipe/projeto-teste   --number 42 --title "Erro ao salvar" --description "Passos para reproduzir" --delivery-id evt-001

# edição da issue -> 202 accepted e mensagem com trigger = author_edit
uv run python scripts/send_event.py --action edited --repository equipe/projeto-teste   --number 42 --title "Erro ao salvar" --description "Agora com o log" --delivery-id evt-002
```

Argumentos: `--action` (`opened` ou `edited`), `--repository` (`owner/name`), `--number`,
`--title`, `--description` (omitido envia `body` nulo, ou seja, issue sem descrição) e
`--delivery-id` (valor de `X-GitHub-Delivery`); `--url` muda o destino (padrão
`http://localhost:8000/webhooks/github`). Sem consumidor (`issue-triage` parado), a mensagem fica
pendente na fila `analysis`, visível no painel do RabbitMQ (http://localhost:15672, `guest`/`guest`).

## Testes

```bash
uv run ruff check . && uv run pytest -q
```

O Docker precisa estar rodando: os testes de integração sobem PostgreSQL (`postgres:16`) e
RabbitMQ (`rabbitmq:3-management`) com testcontainers. Nenhum teste chama o GitHub ou a Anthropic.

| Testes | Real | Simulado |
|---|---|---|
| `tests/unit/test_config.py`, `test_events.py`, `test_signature.py` | código de configuração, filtro, normalização e HMAC | nada externo é usado |
| `tests/unit/test_health.py`, `test_webhook_endpoint.py`, `test_webhook_validation_table.py`, `test_webhook_logging.py` | aplicação FastAPI pelo `TestClient`, assinatura e validação | banco e broker apontam para endereços inacessíveis; nos testes do webhook, a gravação no banco é trocada por um falso |
| `tests/unit/test_outbox_relay_logging.py` | relay e seus logs | conexão ao broker e ao banco roteirizadas |
| `tests/unit/test_send_event_script.py` | montagem do payload, headers e assinatura do script | o envio HTTP não acontece |
| `tests/integration/test_inbound_schema.py` | PostgreSQL e migrações | — |
| `tests/integration/test_issue_events_persistence.py`, `test_deduplication.py`, `test_issue_edited.py` | aplicação pelo `TestClient` e PostgreSQL | broker inacessível (o relay não publica); banco parado simulado por URL inacessível |
| `tests/integration/test_outbox_relay.py` | relay, aplicação pelo `TestClient`, PostgreSQL e RabbitMQ (incluindo parar e religar o broker) | um dos testes usa broker inacessível por URL; a queda entre publicar e marcar é forçada por uma subclasse do relay |
| `tests/integration/test_end_to_end.py` | serviço no uvicorn numa porta local, PostgreSQL, RabbitMQ e envio HTTP pelo script; broker e banco parados e religados de verdade | o GitHub (o script faz o papel dele); não há consumidor da fila `analysis` (`issue-triage` parado) |

## Documentação

Veja [doc-reference.md](../docs/doc-reference.md), que aponta para o índice dos documentos do projeto,
e o plano de desenvolvimento em [docs/plano-desenvolvimento-issue-analyst-api.md](docs/plano-desenvolvimento-issue-analyst-api.md).
