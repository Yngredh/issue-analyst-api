# issue-analyst-api

Recebe issues do GitHub, valida e encaminha para o processo de triagem.

## Pré-requisitos

- [uv](https://docs.astral.sh/uv/) (instala o Python 3.12 automaticamente, se necessário)
- Docker com Docker Compose (PostgreSQL e RabbitMQ locais e testes de integração)

## Configuração

Copie `.env.example` para `.env` e preencha os valores. São obrigatórios `DATABASE_URL`,
`RABBITMQ_URL` e `GITHUB_WEBHOOK_SECRET`; os demais têm padrão (`HTTP_PORT=8000`,
`MAX_TITLE_CHARS=256`, `MAX_DESCRIPTION_CHARS=500`, `RELAY_POLL_INTERVAL_S=1`,
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

## Testes

```bash
uv run ruff check . && uv run pytest -q
```

## Documentação

Veja [doc-reference.md](../docs/doc-reference.md), que aponta para o índice dos documentos do projeto,
e o plano de desenvolvimento em [docs/plano-desenvolvimento-issue-analyst-api.md](docs/plano-desenvolvimento-issue-analyst-api.md).
