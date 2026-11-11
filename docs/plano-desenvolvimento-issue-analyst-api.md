# Plano de desenvolvimento — issue-analyst-api (Entrada de issues)

Plano de implementação da **Entrada de issues (issue-analyst-api) | Spec Geral**, organizado em etapas para execução por agentes orquestrados. Cobre a entrada do sistema: webhook do GitHub → assinatura → filtro → normalização e validação → deduplicação → persistência com outbox → relay para a fila `analysis` → resposta 202 sem esperar a análise.

Requisitos atendidos: RF01 a RF07, entrada do RF28 (`issues.edited`), RNF01, RNF02, RNF04, RNF05, RNF07, RNF14, RNF16 e RNF18.

## Cabeçalho

**Stack.** Python 3.12 (uv), FastAPI + uvicorn, Pydantic v2, pydantic-settings, structlog, PostgreSQL (psycopg 3 async), RabbitMQ (aio-pika), pytest, pytest-asyncio, testcontainers, ruff, Docker Compose. Detalhes em "Decisões técnicas da implementação".

**Comando de validação.** `uv run ruff check . && uv run pytest -q` (o Docker precisa estar rodando: os testes de integração sobem PostgreSQL e RabbitMQ com testcontainers).

**Branch.** `feature/entrada-de-issues`. Uma etapa por commit, com a mensagem `etapa N - <título da etapa>`.

**Referências** (fonte da verdade; em caso de divergência, ver "Pontos em aberto"):
- Entrada de issues (issue-analyst-api) | Spec Geral — `docs/referencias/entrada-de-issues.md`
- Modelagem de dados — `docs/referencias/modelagem-de-dados.md` (schema `inbound`)
- Requisitos — `docs/referencias/requisitos.md`
- High Level Design — `docs/referencias/high-level-design.md` (seções 3.2 Filas e 5 Tecnologias)
- Lista de features — `docs/features-issue-analyst-api.md`

As seções "Contratos" e "Modelo de dados" deste plano resumem o que cada etapa precisa, para que o agente não dependa de ler os documentos inteiros.

**Pré-requisitos (manuais, antes da Etapa 1)**
- [ ] Commitar na `main` o esqueleto gerado pelo `uv init` (pyproject, `src/`, `tests/`, `.env.example`, `.gitignore`, README) e este plano.
- [ ] Exportar os documentos de referência para `docs/referencias/` com os nomes acima.
- [ ] Validar as "Decisões técnicas da implementação" e os "Pontos em aberto" que bloqueiam etapas (indicados em cada etapa).

## Como executar com agentes

- **Orquestrador** (sessão principal do Claude Code): não implementa nada. Atualiza a `main`, cria a branch, lê só os títulos `### Etapa N — ...` da seção "Atividades" e, para cada etapa com checkboxes pendentes, em ordem e uma por vez, chama o subagente `spec-step-implementer` com: caminho deste plano, número da etapa, branch e comando de validação. Depois de cada etapa CONCLUÍDA confere o commit (`git log -1 --oneline`) e faz `git push -u origin feature/entrada-de-issues`. Em BLOQUEADA, para e mostra o relatório.
- **Subagente `spec-step-implementer`**: implementa só a etapa recebida, roda a validação até passar, marca os checkboxes concluídos (itens manuais ficam desmarcados), faz `git add` só dos arquivos que alterou e commita. Nunca faz push.
- **Regras para todas as etapas**
  - Não decidir nada que não esteja neste plano ou nos documentos de referência. Lacuna ou contradição → etapa BLOQUEADA com relatório.
  - Etapa com "Pré-condição" não atendida → BLOQUEADA.
  - Testes de cada item junto da implementação; testes unitários em `tests/unit`, de integração em `tests/integration`.
  - Código, identificadores e logs em inglês; docstrings e textos para humanos em português.
  - Nunca colocar segredos no repositório nem em log; nunca chamar o GitHub ou a Anthropic de verdade.
  - Toda mensagem de commit cita os requisitos da etapa (por exemplo: `etapa 3 - Webhook, limite de tamanho e assinatura HMAC (RF01, RF02)`).

## Decisões técnicas da implementação

Escolhas de implementação que os documentos não fecham. **Status: propostas, validar antes da Etapa 1.**

| # | Ponto | Escolha proposta |
|---|---|---|
| D1 | Driver do PostgreSQL | psycopg 3 assíncrono (`psycopg[binary,pool]`), com `AsyncConnectionPool` aberto no lifespan do FastAPI |
| D2 | Migrações | Arquivos SQL numerados em `migrations/` (`0001_*.sql`), aplicados em ordem por `python -m issue_analyst_api.migrate`, que registra a versão em `inbound.schema_migrations (version, applied_at)` |
| D3 | Cliente do RabbitMQ | aio-pika, com publisher confirms e mensagens persistentes |
| D4 | Servidor HTTP | uvicorn, iniciado por `python -m issue_analyst_api.main` |
| D5 | Imagens locais | `postgres:16` e `rabbitmq:3-management` |
| D6 | Parâmetros do relay | `RELAY_POLL_INTERVAL_S = 1`, `RELAY_BATCH_SIZE = 100`, backoff de reconexão ao broker de 1 s a 30 s |
| D7 | Motivos de `ignored` | `ping`, `unsupported_event`, `unsupported_action`, `bot_sender` |
| D8 | Corpo dos erros 400 | `{"error": "<código>", "fields": ["<campo>", ...]}`; precedência: `invalid_payload` > `missing_event_id` > `missing_fields` > `invalid_fields` |
| D9 | Link de consulta no 202 | campo `statusUrl` com `/issues/{owner}/{repo}/{number}` |
| D10 | "Estado atual" no `duplicate` | `status` lido de `inbound.issue_events` (sempre `ACCEPTED` nesta entrega); a combinação com triage e publisher fica para a spec da consulta |
| D11 | Limite do corpo | constante de 1 MB (1.048.576 bytes) no código; só título e descrição são configuráveis |

## Contratos

### Entrada
- `POST /webhooks/github`, corpo = payload do GitHub. `GET /health` → `200 {"status": "ok"}`, sem tocar banco nem broker.
- Ordem de processamento: limite de 1 MB na leitura → HMAC SHA-256 do corpo bruto com `GITHUB_WEBHOOK_SECRET`, comparado a `X-Hub-Signature-256` (`sha256=<hex>`) em tempo constante → JSON → filtro → normalização e validação → transação.
- Filtro: só `X-GitHub-Event: issues` com `action` `opened` (→ `issue.created`) ou `edited` (→ `issue.edited`). `ping`, outros eventos, outras ações e `sender.type = "Bot"` → `200 ignored`.

### Normalização
| Campo | Origem | Regra |
|---|---|---|
| `eventId` | header `X-GitHub-Delivery` | obrigatório |
| `type` | `X-GitHub-Event` + `action` | `issue.created` ou `issue.edited` |
| `occurredAt` | `issue.created_at` (em `issue.edited`, `issue.updated_at`) | ISO 8601 em UTC |
| `repository` | `repository.full_name` | obrigatório, formato `owner/name` |
| `issueNumber` | `issue.number` | obrigatório, inteiro ≥ 1 |
| `title` | `issue.title` | obrigatório; só espaços conta como ausente; até `MAX_TITLE_CHARS` (256) |
| `description` | `issue.body` | opcional; `null` vira `""`; só espaços conta como vazio (`missingDescription = true`); até `MAX_DESCRIPTION_CHARS` (500) |

O payload bruto é guardado (`raw_payload`) mas nunca vai para a IA. A entrada não consulta o catálogo.

### Respostas
| Código | Corpo | Quando |
|---|---|---|
| 202 | `{"status": "accepted", "eventId", "statusUrl"}` | evento novo gravado (resposta só depois do commit) |
| 200 | `{"status": "duplicate", "reason": "duplicate_event" \| "duplicate_issue", "eventId", "state"}` | `eventId` ou issue já recebidos |
| 200 | `{"status": "ignored", "reason"}` | ping, outro evento, outra ação ou bot |
| 400 | `{"error": "invalid_payload" \| "missing_event_id" \| "missing_fields" \| "invalid_fields", "fields": [...]}` | JSON inválido ou campos com problema; `missing_fields` lista todos os faltantes |
| 401 | vazio | assinatura ausente ou inválida |
| 413 | `{"error": "payload_too_large"}` | corpo acima de 1 MB, antes do HMAC |
| 503 | `{"error": "unavailable"}` | banco indisponível; nada gravado |

Nenhuma resposta 4xx grava algo, então um reenvio corrigido do mesmo `eventId` é aceito.

### Mensagem da fila `analysis`
Fila durável `analysis`, mensagem persistente, `message_id` = `dedup_key`. Payload de um `issue.created`:
```json
{"eventId": "evt-001", "attempt": 1, "trigger": "initial", "triggerRef": null}
```
O `issue-triage` descarta repetições por `eventId + attempt`; a entrega do relay é at-least-once.

### Variáveis de ambiente
`LOG_LEVEL`, `SERVICE_NAME`, `HTTP_PORT`, `DATABASE_URL`, `RABBITMQ_URL`, `GITHUB_WEBHOOK_SECRET`, `MAX_TITLE_CHARS`, `MAX_DESCRIPTION_CHARS`, `RELAY_POLL_INTERVAL_S`, `RELAY_BATCH_SIZE`. O `.env.example` traz só os nomes.

## Modelo de dados (schema `inbound`, conforme a Modelagem de dados)

```sql
CREATE SCHEMA IF NOT EXISTS inbound;

CREATE TABLE inbound.issue_events (
  event_id             text PRIMARY KEY,           -- X-GitHub-Delivery (chave de entrada)
  type                 text NOT NULL CHECK (type IN ('issue.created', 'issue.edited')),
  occurred_at          timestamptz NOT NULL,
  received_at          timestamptz NOT NULL DEFAULT now(),
  repository           text NOT NULL,
  issue_number         int  NOT NULL CHECK (issue_number > 0),
  title                text NOT NULL CHECK (btrim(title) <> ''),
  description          text NOT NULL DEFAULT '',  -- body nulo vira ''; vazio = sem descrição
  raw_payload          jsonb NOT NULL,             -- auditoria; nunca vai para a IA
  body_hash            text NOT NULL,              -- sha256 do corpo (duplicate_mismatch)
  status               text NOT NULL DEFAULT 'ACCEPTED' CHECK (status = 'ACCEPTED')
);
CREATE INDEX ON inbound.issue_events (repository, issue_number, received_at);
CREATE UNIQUE INDEX issue_events_one_created_per_issue
  ON inbound.issue_events (repository, issue_number) WHERE type = 'issue.created';

CREATE TABLE inbound.inbound_outbox (
  id            bigserial PRIMARY KEY,
  event_id      text NOT NULL,                     -- origin_event_id do caso
  topic         text NOT NULL CHECK (topic = 'analysis'),
  dedup_key     text NOT NULL,                     -- '<eventId>:<attempt>' ou '<eventId>:close'
  payload       jsonb NOT NULL,                    -- eventId, attempt, trigger, triggerRef
  created_at    timestamptz NOT NULL DEFAULT now(),
  published_at  timestamptz,
  UNIQUE (topic, dedup_key)
);
CREATE INDEX inbound_outbox_pending ON inbound.inbound_outbox (id) WHERE published_at IS NULL;
```

`inbound.issue_review_history` e a view `inbound.issue_presentation` pertencem às specs de revisão e de consulta e ficam fora deste plano.

## Atividades

### Etapa 1 — Fundação do serviço HTTP
Requisitos: RNF14, RNF18. Depende de: pré-requisitos. Pré-condição: decisões D1 a D5 validadas.
- [ ] Adicionar dependências com `uv add`: `fastapi`, `uvicorn[standard]`, `psycopg[binary,pool]`, `aio-pika`; e com `uv add --dev`: `testcontainers[postgres,rabbitmq]`, `httpx`.
- [ ] Completar `Settings` com as variáveis de "Contratos > Variáveis de ambiente" (`GITHUB_WEBHOOK_SECRET` como `SecretStr` obrigatório; limites com os padrões 256 e 500) e ajustar os testes existentes de `Settings`.
- [ ] Criar a fábrica `create_app()` em `api/` com `GET /health` e um lifespan vazio, pronto para abrir o pool do banco e o relay nas próximas etapas.
- [ ] Fazer `main.py` subir o uvicorn com `create_app()` na porta `HTTP_PORT`.
- [ ] Atualizar o `.env.example` (só nomes) e criar `docker-compose.yml` local com PostgreSQL e RabbitMQ para desenvolvimento.
- [ ] Criar `Dockerfile` do serviço baseado em uv.
- [ ] Testes: `/health` responde 200 sem banco nem broker; `Settings` falha sem `GITHUB_WEBHOOK_SECRET`.
- [ ] Atualizar o README com como subir o compose e iniciar o serviço.

### Etapa 2 — Schema inbound e migrações
Requisitos: RNF07. Depende de: Etapa 1.
- [ ] Criar `migrations/0001_inbound_schema.sql` com o DDL da seção "Modelo de dados".
- [ ] Criar o runner `python -m issue_analyst_api.migrate` (decisão D2), idempotente: rodar duas vezes não falha nem duplica.
- [ ] Criar o adaptador de conexão em `adapters/` (pool assíncrono do psycopg) e abrir e fechar o pool no lifespan.
- [ ] Criar fixtures compartilhadas em `tests/conftest.py`: PostgreSQL do testcontainers por sessão, com as migrações aplicadas.
- [ ] Testes de integração: tabelas e índices criados; o índice parcial barra um segundo `issue.created` da mesma issue e aceita `issue.edited`; o `CHECK` recusa título só com espaços; o runner é idempotente.

### Etapa 3 — Webhook, limite de tamanho e assinatura HMAC
Requisitos: RF01, RF02. Depende de: Etapa 1.
- [ ] Criar `POST /webhooks/github` lendo o corpo bruto em streaming e recusando acima de 1 MB com `413 payload_too_large`, antes de conferir a assinatura.
- [ ] Conferir o HMAC SHA-256 do corpo bruto contra `X-Hub-Signature-256` com `hmac.compare_digest`; ausente, malformado ou divergente → `401` sem corpo.
- [ ] Interpretar o JSON só depois da assinatura válida; JSON inválido → `400 invalid_payload`.
- [ ] Até a Etapa 5, uma requisição que passa por tudo isso responde `501` (provisório, substituído na Etapa 5).
- [ ] Testes unitários do verificador de assinatura e testes do endpoint pelo cliente de teste do FastAPI: válida, ausente, errada, malformada, corpo acima do limite com assinatura inválida (413) e JSON inválido; o segredo não aparece em nenhum log.

### Etapa 4 — Filtro de eventos e normalização
Requisitos: RF02, RF03, RF04, RF05, RF28 (entrada). Depende de: Etapa 3. Pré-condição: decisões D7 e D8 validadas.
- [ ] Filtro: só `issues` com `opened` ou `edited` segue; `ping`, outros eventos, outras ações e `sender.type = "Bot"` → `200 ignored` com o motivo (D7).
- [ ] Modelo Pydantic do evento normalizado e função de mapeamento conforme "Contratos > Normalização", incluindo `missingDescription` derivado da descrição vazia.
- [ ] Validação com os códigos de "Contratos > Respostas": `missing_event_id`, `missing_fields` (lista todos os faltantes), `invalid_fields` (repositório fora de `owner/name`, número não inteiro ou menor que 1, título acima de `MAX_TITLE_CHARS`, descrição acima de `MAX_DESCRIPTION_CHARS`), com a precedência de D8.
- [ ] Ligar filtro e normalização ao endpoint; evento válido continua respondendo `501` até a Etapa 5.
- [ ] Testes parametrizados cobrindo cada linha da tabela de Validação da Entrada de issues, incluindo descrição nula, vazia e só com espaços (aceita, `missingDescription = true`) e repositório fora do catálogo (aceito, sem consulta ao catálogo).

### Etapa 5 — Persistência transacional com outbox
Requisitos: RF07, RNF05, RNF07. Depende de: Etapas 2 e 4. Pré-condição: decisão D9 validada.
- [ ] Repositório em `adapters/` que, numa única transação, faz `INSERT ... ON CONFLICT DO NOTHING` (sem alvo) em `inbound.issue_events` e, só se a linha foi inserida, grava em `inbound.inbound_outbox` a mensagem de "Contratos > Mensagem da fila `analysis`" (`dedup_key = '<eventId>:1'`), com `body_hash` = SHA-256 do corpo bruto.
- [ ] `issue.created` aceito → `202 accepted` com `eventId` e `statusUrl`, só depois do commit.
- [ ] Sem linha inserida: encerrar sem gravar outbox e responder `200 duplicate` só com `eventId` (o motivo e o estado são completados na Etapa 6).
- [ ] `issue.edited` válido continua respondendo `501` até a Etapa 9.
- [ ] Banco indisponível → `503 unavailable` e nada gravado.
- [ ] Testes de integração: 202 grava exatamente uma linha em cada tabela; falha no insert da outbox não deixa evento gravado (atomicidade); banco parado → 503; `raw_payload` guardado sem alteração.

### Etapa 6 — Deduplicação
Requisitos: RF06. Depende de: Etapa 5. Pré-condição: decisão D10 validada.
- [ ] Quando nada foi inserido, consultar qual chave conflitou e responder `200 duplicate` com `reason` (`duplicate_event` para o mesmo `eventId`, `duplicate_issue` para a mesma issue com outro `eventId`), `eventId` e `state`.
- [ ] Mesmo `eventId` com corpo diferente: ganha o primeiro, e é registrado o log `duplicate_mismatch` comparando os `body_hash`.
- [ ] Testes de integração: reenvio do mesmo `eventId` (`duplicate_event`, sem nova mensagem na outbox); segundo `issue.created` da mesma issue com outro `eventId` (`duplicate_issue`); evento recusado com 400 ou 401 não ocupa a chave e o reenvio corrigido é aceito; duas entregas simultâneas do mesmo `eventId` gravam uma única linha e uma única mensagem.

### Etapa 7 — Relay da outbox
Requisitos: RF07, RNF02, RNF04, RNF05. Depende de: Etapa 5. Pré-condição: decisão D6 validada e ponto em aberto P4 decidido (argumentos da fila `analysis`).
- [ ] Tarefa em segundo plano iniciada no lifespan, fora do caminho da requisição: lê até `RELAY_BATCH_SIZE` linhas com `published_at` nulo em ordem de `id` (`FOR UPDATE SKIP LOCKED`), publica e marca `published_at` na mesma transação da leitura.
- [ ] Publicação com aio-pika na fila durável `analysis`: mensagem persistente, `message_id = dedup_key`, publisher confirms; `published_at` só é marcado depois da confirmação.
- [ ] Broker indisponível: o relay tenta reconectar com backoff (D6) sem derrubar o serviço, e a entrada continua respondendo 202.
- [ ] Declarar a fila `analysis` conforme a decisão de P4.
- [ ] Testes de integração com RabbitMQ do testcontainers: mensagens pendentes publicadas em ordem e marcadas; broker parado acumula na outbox e publica ao voltar; queda entre publicar e marcar republica na volta (at-least-once); o encerramento do serviço para o relay sem perder linhas.

### Etapa 8 — Observabilidade da entrada
Requisitos: RNF14. Depende de: Etapas 6 e 7.
- [ ] Um log estruturado por resposta do webhook com `eventId` (quando houver), código HTTP e decisão (`accepted`, `duplicate`, `ignored`, `rejected`) e o motivo.
- [ ] `eventId` ligado ao contexto do structlog durante a requisição e nas publicações do relay.
- [ ] Logs do relay: publicação, falha de conexão e retomada.
- [ ] Nenhum log contém o segredo, o corpo bruto ou a descrição completa.
- [ ] Testes com `structlog.testing.capture_logs` cobrindo as quatro decisões e a ausência de dados sensíveis.

### Etapa 9 — Mensagem de issue.edited
Requisitos: RF28 (entrada). Depende de: Etapa 6. Pré-condição: ponto em aberto P1 decidido e registrado neste plano.
- [ ] Gravar o `issue.edited` aceito em `inbound.issue_events` e a mensagem de `analysis` no formato decidido em P1, na mesma transação.
- [ ] Substituir o `501` provisório do `issue.edited` pela resposta definitiva.
- [ ] Testes de integração do formato decidido, incluindo reenvio do mesmo `issue.edited` (`duplicate_event`).

### Etapa 10 — Resiliência, script de envio e demonstração
Requisitos: RNF01, RNF02, RNF16. Depende de: Etapas 7, 8 e 9.
- [ ] Criar `scripts/send_event.py`: monta um payload no formato do GitHub (`opened` ou `edited`, repositório, número, título, descrição e delivery id por argumento), assina com `GITHUB_WEBHOOK_SECRET` lido do ambiente e envia ao `POST /webhooks/github`.
- [ ] Testes de ponta a ponta da entrada (serviço, PostgreSQL e RabbitMQ reais, sem consumidor): sem `issue-triage`, a mensagem fica pendente na fila `analysis`; broker parado → 202 e a outbox publica ao voltar; banco parado → 503; reenvio da mesma entrega → `200 duplicate` e uma única mensagem na fila.
- [ ] README: como rodar localmente, migrar, enviar eventos com o script, rodar os testes e o que é real ou simulado em cada teste.
- [ ] (manual) Configurar o webhook no repositório de teste do GitHub apontando para o serviço por um túnel e com o segredo configurado.
- [ ] (manual) Executar o trecho da entrada no roteiro da demonstração: parar o `issue-triage`, abrir uma issue, mostrar o 202 e a mensagem pendente; reenviar a entrega e mostrar o `200 duplicate`.

## Pontos em aberto

Divergências entre documentos e lacunas que bloqueiam etapas. Cada decisão tomada aqui deve voltar para a Entrada de issues, a Modelagem de dados e o High Level Design.

| # | Ponto | Situação | Bloqueia |
|---|---|---|---|
| P1 | Mensagem de `issue.edited` | A Entrada de issues diz que o evento é enfileirado e o `issue-triage` decide se a edição importa. O High Level Design (seção 3.2) e a Modelagem de dados dizem que o `issue-analyst-api` produz `attempt + 1` só em `READY_TO_PUBLISH` com `empty_description`, com o `origin_event_id` do caso em `eventId` e o delivery id da edição em `triggerRef`. Falta decidir quem filtra e o formato do payload | Etapa 9 |
| P2 | Nomes das tabelas | A Entrada de issues usa `ingested_events`, `outbox` e o índice `ingested_events_one_created_per_issue`; a Modelagem de dados usa `inbound.issue_events`, `inbound.inbound_outbox` e `issue_events_one_created_per_issue`. Este plano segue a Modelagem de dados, que é a referência das colunas | — (corrigir a Entrada de issues) |
| P3 | Coluna `missing_description` | Citada na Entrada de issues e ausente no DDL da Modelagem de dados. Este plano deriva `missingDescription` de `description = ''`, sem coluna | — (corrigir a Entrada de issues) |
| P4 | Argumentos da fila `analysis` | O High Level Design prevê DLQ para `analysis`, e a definição dela está pendente na spec de IA. Como a declaração precisa ter os mesmos argumentos em quem declara e em quem consome, falta decidir os argumentos e qual serviço declara a topologia | Etapa 7 |
| P5 | Papéis e permissões do banco | O `GRANT` e o papel `analyst_api` da Modelagem de dados atravessam serviços; falta decidir onde ficam (script de inicialização do ambiente integrado) e onde mora o compose que sobe os três serviços | Não bloqueia: este plano usa o compose local e `DATABASE_URL` |

## Fora do escopo

API de consulta (RF27) e a view `inbound.issue_presentation`, endpoint de resolução da revisão humana e `inbound.issue_review_history`, transições de status, limpeza de dados antigos, autenticação do endpoint de revisão.

## Limitações assumidas

A ingestão depende do banco (com 503 o reenvio é manual, porque o GitHub não reenvia); descrição acima de 500 caracteres é recusada; sem limpeza de `raw_payload` e do outbox publicado; a entrada protege contra a parada da análise, não contra a queda da infraestrutura.
