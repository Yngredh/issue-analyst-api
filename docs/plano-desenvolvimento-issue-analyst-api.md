# Plano de desenvolvimento — issue-analyst-api (Entrada de issues)

Plano de implementação da **Entrada de issues (issue-analyst-api) | Spec Geral**, organizado em etapas para execução por agentes orquestrados. Cobre a entrada do sistema: webhook do GitHub → assinatura → filtro → normalização e validação → deduplicação → persistência com outbox → relay para a fila `analysis` → resposta 202 sem esperar a análise.

Requisitos atendidos: RF01 a RF07, entrada do RF28 (`issues.edited`), RNF01, RNF02, RNF04, RNF05, RNF07, RNF14, RNF16 e RNF18.

## Cabeçalho

**Stack.** Python 3.12 (uv), FastAPI + uvicorn, Pydantic v2, pydantic-settings, structlog, PostgreSQL (psycopg 3 async), RabbitMQ (aio-pika), pytest, pytest-asyncio, testcontainers, ruff, Docker Compose. Detalhes em "Decisões técnicas da implementação".

**Comando de validação.** `uv run ruff check . && uv run pytest -q` (o Docker precisa estar rodando: os testes de integração sobem PostgreSQL e RabbitMQ com testcontainers).

**Branch.** `feature/entrada-de-issues`. Uma etapa por commit, com a mensagem `etapa N - <título da etapa>`.

**Referências** (fonte da verdade; em caso de divergência, ver "Pontos em aberto"). Não existe `docs/referencias/` neste repositório: a fonte da verdade é `../docs/doc-reference.md` (pasta `docs/` da raiz do projeto), que aponta para as páginas web de cada documento (registrado em 2026-11-10):
- Entrada de issues (issue-analyst-api) | Spec Geral — "Ingestão Spec" do `doc-reference.md`: https://claude.ai/artifact/C35MqLEDZ2k8wacbDpwde2 (confirmado pelo usuário em 2026-11-10; o link KpLq1aug1K7yFwaSfKQcvf é da Issue Publisher Spec e não se aplica a este serviço)
- Modelagem de dados (schema `inbound`) — link no `doc-reference.md`
- Requisitos — link no `doc-reference.md`
- High Level Design (seções 3.2 Filas e 5 Tecnologias) — link no `doc-reference.md`
- Lista de features — `docs/features-issue-analyst-api.md`

As seções "Contratos" e "Modelo de dados" deste plano resumem o que cada etapa precisa, para que o agente não dependa de ler os documentos inteiros.

**Pré-requisitos (manuais, antes da Etapa 1)**
- [x] Commitar na `main` o esqueleto gerado pelo `uv init` (pyproject, `src/`, `tests/`, `.env.example`, `.gitignore`, README) e este plano. (feito em `d1ac992`, mergeado na `main` em `0d71ca8`)
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

Escolhas de implementação que os documentos não fecham. **Status: D1 a D5 validadas pelo usuário em 2026-11-10; D7 e D8 validadas pelo usuário em 2026-11-10 como estão; D11 validada pelo usuário em 2026-11-10 com alteração (limite configurável); D12 validada pelo usuário em 2026-11-10; D6 e D10 validadas pelo usuário em 2026-11-10 como estão; D9 alterada pelo usuário em 2026-11-10 para seguir a Ingestão Spec; D13 validada pelo usuário em 2026-11-12. Regra geral do usuário: em caso de dúvida ou divergência, prevalece o que está definido na Ingestão Spec.**

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
| D9 | Link de consulta no 202 | **Alterada pelo usuário em 2026-11-10 para seguir a Ingestão Spec:** objeto `links` com `result` = `/issues/{owner}/{repo}/{number}`, em vez do campo `statusUrl`. Exemplo da spec: `{"status": "accepted", "eventId": "evt-001", "links": {"result": "/issues/equipe/projeto-teste/42"}}` |
| D10 | "Estado atual" no `duplicate` | `status` lido de `inbound.issue_events` (sempre `ACCEPTED` nesta entrega); a combinação com triage e publisher fica para a spec da consulta |
| D11 | Limite do corpo | **Alterada pelo usuário em 2026-11-10:** configurável pela variável `WEBHOOK_MAX_BODY_BYTES` (int, padrão 1.048.576 bytes = 1 MB), em vez de constante no código |
| D12 | Detalhes da normalização e validação (Etapa 4), **validada pelo usuário em 2026-11-10** | 1) `occurredAt` ausente, não string, fora de ISO 8601 ou sem fuso (não se assume UTC) **não recusa o evento**: o normalizado sai com `occurredAt = null` e `occurredAtFallback` = `missing` ou `invalid`, é registrado o log de warning `occurred_at_fallback` com o motivo, e a Etapa 5 grava em `occurred_at` o mesmo instante de `received_at` (a coluna continua NOT NULL); `occurredAt` nunca entra em `missing_fields` nem em `invalid_fields`. 2) `missing_event_id` responde `"fields": ["eventId"]`; só `invalid_payload` usa a lista vazia. 3) JSON válido que não é objeto → `400 invalid_payload` com `"fields": []`, antes do filtro. 4) Motivos de `ignored` avaliados na ordem da D7 (`ping` > `unsupported_event` > `unsupported_action` > `bot_sender`), e o filtro roda antes da exigência de `X-GitHub-Delivery`: um `ping` sem `X-GitHub-Delivery` responde `200 ignored`. 5) Descrição só com espaços é gravada como `""`; não string ou acima do limite → `invalid_fields`; os limites contam caracteres, não bytes. 6) `repository` no formato `owner/name` (uma barra, partes não vazias e sem espaços; ausente/`null` → `missing_fields`, vazio ou não string → `invalid_fields`); `issueNumber` aceito só com `type(v) is int` e ≥ 1 (`true`, `7.0`, `"7"` → `invalid_fields`); limites inclusivos (`<=`); ordem dos campos no erro: repository, issueNumber, title, description |
| D13 | Detalhes da deduplicação (Etapa 6), **validada pelo usuário em 2026-11-12** | 1) Em `duplicate_issue`, `eventId` da resposta é o da entrega que acabou de chegar, não o do `issue.created` já gravado. 2) Estado atual = campo `"state": "ACCEPTED"` em texto (D10), lido da linha já gravada: em `duplicate_event`, a do mesmo `eventId`; em `duplicate_issue`, a do `issue.created` da issue. 3) Log `duplicate_mismatch` só em `duplicate_event` e só quando os hashes diferem: nível warning, campos `eventId`, `storedBodyHash` e `receivedBodyHash`; é um log extra, além do log de decisão da Etapa 8. 4) Na consulta feita depois do conflito, o `event_id` é verificado **primeiro** e só depois o par `repository + issue_number`: mesmo `eventId` e mesma issue ao mesmo tempo → `duplicate_event`. Com `ON CONFLICT DO NOTHING`, o PostgreSQL espera a transação concorrente terminar, então a consulta sempre encontra a linha vencedora |

**Decisões aprovadas pelo usuário em 2026-11-10 (tomadas na Etapa 1):**
- `DATABASE_URL` e `RABBITMQ_URL` são obrigatórias, sem valor padrão.
- `HTTP_PORT` tem padrão 8000, com bind em `0.0.0.0`.
- Credenciais locais do compose: PostgreSQL `postgres`/`postgres` com o banco `issue_analyst`; RabbitMQ `guest`/`guest`.
- `RELAY_POLL_INTERVAL_S` é `float`.

**Decisão do usuário em 2026-11-10 (aplicada na Etapa 5):**
- Windows: `main.py`, só quando `sys.platform == "win32"`, roda o servidor com `asyncio.run(server.serve(), loop_factory=asyncio.SelectorEventLoop)` (Python 3.12), porque o psycopg assíncrono não funciona com o loop Proactor. Docker/Linux não muda (`server.run()`).

**Escolha de implementação da Etapa 5 (proposta, aguarda validação):**
- Espera máxima por conexão do pool do banco: `CONNECTION_TIMEOUT_S = 5` s (constante em `adapters/database.py`); passado esse tempo sem conexão, o webhook responde `503 unavailable`. Escolhido abaixo do tempo limite de entrega do webhook do GitHub (10 s); o padrão do psycopg (30 s) passaria desse limite.

## Contratos

### Entrada
- `POST /webhooks/github`, corpo = payload do GitHub. `GET /health` → `200 {"status": "ok"}`, sem tocar banco nem broker.
- Ordem de processamento: limite de `WEBHOOK_MAX_BODY_BYTES` (padrão 1 MB) na leitura → HMAC SHA-256 do corpo bruto com `GITHUB_WEBHOOK_SECRET`, comparado a `X-Hub-Signature-256` (`sha256=<hex>`) em tempo constante → JSON → filtro → normalização e validação → transação.
- Filtro: só `X-GitHub-Event: issues` com `action` `opened` (→ `issue.created`) ou `edited` (→ `issue.edited`). `ping`, outros eventos, outras ações e `sender.type = "Bot"` → `200 ignored`.

### Normalização
| Campo | Origem | Regra |
|---|---|---|
| `eventId` | header `X-GitHub-Delivery` | obrigatório |
| `type` | `X-GitHub-Event` + `action` | `issue.created` ou `issue.edited` |
| `occurredAt` | `issue.created_at` (em `issue.edited`, `issue.updated_at`) | ISO 8601 em UTC; ausente ou inválido → fallback para `received_at` (D12) |
| `repository` | `repository.full_name` | obrigatório, formato `owner/name` |
| `issueNumber` | `issue.number` | obrigatório, inteiro ≥ 1 |
| `title` | `issue.title` | obrigatório; só espaços conta como ausente; até `MAX_TITLE_CHARS` (256) |
| `description` | `issue.body` | opcional; `null` vira `""`; só espaços conta como vazio (`missingDescription = true`); até `MAX_DESCRIPTION_CHARS` (500) |

O payload bruto é guardado (`raw_payload`) mas nunca vai para a IA. A entrada não consulta o catálogo.

### Validação (copiada da Ingestão Spec, rev 113)

A validação recusa só o que impede identificar a issue. Descrição vazia não é erro de entrada: o evento é aceito e a falta de informação fica registrada para o `issue-triage` tratar (RF05, RF11).

| Cenário | Resposta | Grava algo? | Observação |
|---|---|---|---|
| Assinatura ausente ou inválida | 401 | Não | Conferida antes de ler o JSON |
| Corpo que não é JSON válido | 400 (`invalid_payload`) | Não | |
| Evento que não é `issues.opened` nem `issues.edited`, `ping` ou enviado por bot | 200 (`ignored`) | Não | 200 evita que o GitHub marque a entrega como falha |
| `X-GitHub-Delivery` ausente | 400 (`missing_event_id`) | Não | Sem `eventId` não há como deduplicar |
| `repository`, `issueNumber` ou `title` ausente | 400 (`missing_fields`) | Não | Lista todos os campos faltantes, não só o primeiro (RF04) |
| `repository` fora do formato `owner/name` | 400 (`invalid_fields`) | Não | |
| `issueNumber` não inteiro ou menor que 1 | 400 (`invalid_fields`) | Não | |
| `title` só com espaços | 400 (`missing_fields`) | Não | Tratado como ausente |
| `description` vazia, nula ou só com espaços | 202 | Sim, com `missingDescription = true` | Segue para análise, que leva a `READY_TO_PUBLISH com empty_description` |
| Repositório fora do catálogo | 202 | Sim | O catálogo não é consultado na entrada; o `issue-triage` decide (`unknown_repo`) |
| title acima de 256 caracteres | 400 (invalid_fields) | Não | Limite configurável (MAX_TITLE_CHARS) |
| description acima de 500 caracteres | 400 (invalid_fields) | Não | Limite da primeira versão, configurável (MAX_DESCRIPTION_CHARS); ver Decisões tomadas |
| Corpo HTTP acima de 1 MB | 413 (payload_too_large) | Não | Limitado na leitura, antes de conferir o HMAC |

A entrada não consulta o catálogo de propósito: mantém o `issue-analyst-api` independente do domínio e concentra a decisão de escopo em um só componente.

Exemplo de corpo de erro na spec:
```json
{
  "error": "missing_fields",
  "fields": ["issueNumber", "title"]
}
```

### Respostas
| Código | Corpo | Quando |
|---|---|---|
| 202 | `{"status": "accepted", "eventId", "links": {"result": "/issues/{owner}/{repo}/{number}"}}` (D9) | evento novo gravado (resposta só depois do commit, sem esperar fila, IA ou GitHub; 202 significa "aceito e guardado", não "analisado") |
| 200 | `{"status": "duplicate", "reason": "duplicate_event" \| "duplicate_issue", "eventId", "state"}` | `eventId` ou issue já recebidos |
| 200 | `{"status": "ignored", "reason"}` | ping, outro evento, outra ação ou bot |
| 400 | `{"error": "invalid_payload" \| "missing_event_id" \| "missing_fields" \| "invalid_fields", "fields": [...]}` | JSON inválido ou campos com problema; `missing_fields` lista todos os faltantes |
| 401 | vazio | assinatura ausente ou inválida |
| 413 | `{"error": "payload_too_large"}` | corpo acima de `WEBHOOK_MAX_BODY_BYTES` (padrão 1 MB), antes do HMAC |
| 503 | `{"error": "unavailable"}` | banco indisponível; nada gravado |

Nenhuma resposta 4xx grava algo, então um reenvio corrigido do mesmo `eventId` é aceito.

### Mensagem da fila `analysis`
Fila durável `analysis`, mensagem persistente, `message_id` = `dedup_key`. Payload de um `issue.created`:
```json
{"eventId": "evt-001", "attempt": 1, "trigger": "initial", "triggerRef": null}
```
Payload de um `issue.edited` (P1, decidido em 2026-11-12), gravado só quando a issue já tem `issue.created` em `inbound.issue_events`; sem ele, a edição é só registrada, sem mensagem:
```json
{"eventId": "<origin_event_id>", "attempt": null, "trigger": "author_edit", "triggerRef": "<delivery id da edição>"}
```
Na outbox: `event_id = origin_event_id`, `dedup_key = '<origin_event_id>:edit:<delivery id>'`. A entrada não lê o estado do caso (schema `triage`): o `issue-triage` decide se a edição reabre a análise (só em `READY_TO_PUBLISH` com `empty_description`, com `attempt = current_attempt + 1`) e descarta as demais com log.

O `issue-triage` descarta repetições por `eventId + attempt` (na edição, `eventId + triggerRef`); a entrega do relay é at-least-once.

### Variáveis de ambiente
`LOG_LEVEL`, `SERVICE_NAME`, `HTTP_PORT`, `DATABASE_URL`, `RABBITMQ_URL`, `GITHUB_WEBHOOK_SECRET`, `WEBHOOK_MAX_BODY_BYTES`, `MAX_TITLE_CHARS`, `MAX_DESCRIPTION_CHARS`, `RELAY_POLL_INTERVAL_S`, `RELAY_BATCH_SIZE`. O `.env.example` traz só os nomes.

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
- [x] Adicionar dependências com `uv add`: `fastapi`, `uvicorn[standard]`, `psycopg[binary,pool]`, `aio-pika`; e com `uv add --dev`: `testcontainers[postgres,rabbitmq]`, `httpx`.
- [x] Completar `Settings` com as variáveis de "Contratos > Variáveis de ambiente" (`GITHUB_WEBHOOK_SECRET` como `SecretStr` obrigatório; limites com os padrões 256 e 500) e ajustar os testes existentes de `Settings`.
- [x] Criar a fábrica `create_app()` em `api/` com `GET /health` e um lifespan vazio, pronto para abrir o pool do banco e o relay nas próximas etapas.
- [x] Fazer `main.py` subir o uvicorn com `create_app()` na porta `HTTP_PORT`.
- [x] Atualizar o `.env.example` (só nomes) e criar `docker-compose.yml` local com PostgreSQL e RabbitMQ para desenvolvimento.
- [x] Criar `Dockerfile` do serviço baseado em uv.
- [x] Testes: `/health` responde 200 sem banco nem broker; `Settings` falha sem `GITHUB_WEBHOOK_SECRET`.
- [x] Atualizar o README com como subir o compose e iniciar o serviço.

### Etapa 2 — Schema inbound e migrações
Requisitos: RNF07. Depende de: Etapa 1.
- [x] Criar `migrations/0001_inbound_schema.sql` com o DDL da seção "Modelo de dados".
- [x] Criar o runner `python -m issue_analyst_api.migrate` (decisão D2), idempotente: rodar duas vezes não falha nem duplica.
- [x] Criar o adaptador de conexão em `adapters/` (pool assíncrono do psycopg) e abrir e fechar o pool no lifespan.
- [x] Criar fixtures compartilhadas em `tests/conftest.py`: PostgreSQL do testcontainers por sessão, com as migrações aplicadas.
- [x] Testes de integração: tabelas e índices criados; o índice parcial barra um segundo `issue.created` da mesma issue e aceita `issue.edited`; o `CHECK` recusa título só com espaços; o runner é idempotente.

### Etapa 3 — Webhook, limite de tamanho e assinatura HMAC
Requisitos: RF01, RF02. Depende de: Etapa 1.
- [x] Criar `POST /webhooks/github` lendo o corpo bruto em streaming e recusando acima de 1 MB com `413 payload_too_large`, antes de conferir a assinatura.
- [x] Conferir o HMAC SHA-256 do corpo bruto contra `X-Hub-Signature-256` com `hmac.compare_digest`; ausente, malformado ou divergente → `401` sem corpo.
- [x] Interpretar o JSON só depois da assinatura válida; JSON inválido → `400 invalid_payload`.
- [x] Até a Etapa 5, uma requisição que passa por tudo isso responde `501` (provisório, substituído na Etapa 5).
- [x] Testes unitários do verificador de assinatura e testes do endpoint pelo cliente de teste do FastAPI: válida, ausente, errada, malformada, corpo acima do limite com assinatura inválida (413) e JSON inválido; o segredo não aparece em nenhum log.

### Etapa 4 — Filtro de eventos e normalização
Requisitos: RF02, RF03, RF04, RF05, RF28 (entrada). Depende de: Etapa 3. Pré-condição: decisões D7 e D8 validadas.
- [x] Filtro: só `issues` com `opened` ou `edited` segue; `ping`, outros eventos, outras ações e `sender.type = "Bot"` → `200 ignored` com o motivo (D7).
- [x] Modelo Pydantic do evento normalizado e função de mapeamento conforme "Contratos > Normalização", incluindo `missingDescription` derivado da descrição vazia e o fallback de `occurredAt` (D12: ausente ou inválido → `occurredAt = null` com `occurredAtFallback` e log `occurred_at_fallback`; a Etapa 5 grava `received_at`).
- [x] Validação com os códigos de "Contratos > Respostas": `missing_event_id`, `missing_fields` (lista todos os faltantes), `invalid_fields` (repositório fora de `owner/name`, número não inteiro ou menor que 1, título acima de `MAX_TITLE_CHARS`, descrição acima de `MAX_DESCRIPTION_CHARS`), com a precedência de D8 e os detalhes de D12 (`occurredAt` nunca recusa o evento).
- [x] Ligar filtro e normalização ao endpoint; evento válido continua respondendo `501` até a Etapa 5.
- [x] Testes parametrizados cobrindo cada linha da tabela de Validação da Entrada de issues, incluindo descrição nula, vazia e só com espaços (aceita, `missingDescription = true`) e repositório fora do catálogo (aceito, sem consulta ao catálogo), além do fallback de `occurredAt`.

### Etapa 5 — Persistência transacional com outbox
Requisitos: RF07, RNF05, RNF07. Depende de: Etapas 2 e 4. Pré-condição: decisão D9 validada.
- [x] Repositório em `adapters/` que, numa única transação, faz `INSERT ... ON CONFLICT DO NOTHING` (sem alvo) em `inbound.issue_events` (com `occurred_at = received_at` quando o normalizado vier com `occurredAtFallback`, D12) e, só se a linha foi inserida, grava em `inbound.inbound_outbox` a mensagem de "Contratos > Mensagem da fila `analysis`" (`dedup_key = '<eventId>:1'`), com `body_hash` = SHA-256 do corpo bruto.
- [x] `issue.created` aceito → `202 accepted` com `eventId` e `links.result` (D9), só depois do commit.
- [x] Sem linha inserida: encerrar sem gravar outbox e responder `200 duplicate` só com `eventId` (o motivo e o estado são completados na Etapa 6).
- [x] `issue.edited` válido continua respondendo `501` até a Etapa 9.
- [x] Banco indisponível → `503 unavailable` e nada gravado.
- [x] Testes de integração: 202 grava exatamente uma linha em cada tabela; falha no insert da outbox não deixa evento gravado (atomicidade); banco parado → 503; `raw_payload` guardado sem alteração.
- [x] Windows: `main.py` sobe o uvicorn com `SelectorEventLoop` (decisão do usuário de 2026-11-10).

### Etapa 6 — Deduplicação
Requisitos: RF06. Depende de: Etapa 5. Pré-condição: decisões D10 e D13 validadas.
- [x] Quando nada foi inserido, consultar qual chave conflitou e responder `200 duplicate` com `reason` (`duplicate_event` para o mesmo `eventId`, `duplicate_issue` para a mesma issue com outro `eventId`), `eventId` e `state`, conforme D13 (consulta pelo `event_id` primeiro; `eventId` da entrega recebida; `state` da linha gravada).
- [x] Mesmo `eventId` com corpo diferente: ganha o primeiro, e é registrado o log `duplicate_mismatch` comparando os `body_hash` (D13: warning, `eventId`, `storedBodyHash`, `receivedBodyHash`; sem log quando os hashes são iguais).
- [x] Testes de integração: reenvio do mesmo `eventId` (`duplicate_event`, sem nova mensagem na outbox); segundo `issue.created` da mesma issue com outro `eventId` (`duplicate_issue`); evento recusado com 400 ou 401 não ocupa a chave e o reenvio corrigido é aceito; duas entregas simultâneas do mesmo `eventId` gravam uma única linha e uma única mensagem; mesmo `eventId` e mesma issue → `duplicate_event`; `duplicate_mismatch` registrado só com hash diferente.

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
Requisitos: RF28 (entrada). Depende de: Etapa 6. Pré-condição: ponto em aberto P1 decidido e registrado neste plano (decidido em 2026-11-12).
- [ ] Gravar o `issue.edited` aceito em `inbound.issue_events` e, na mesma transação, se existir `issue.created` da mesma issue, gravar em `inbound.inbound_outbox` a mensagem de "Contratos > Mensagem da fila `analysis`" para edição (`event_id = origin_event_id`, `attempt` nulo, `trigger = author_edit`, `triggerRef` = delivery id, `dedup_key = '<origin_event_id>:edit:<delivery id>'`). Sem `issue.created`, só grava o evento. Não ler o schema `triage`.
- [ ] Substituir o `501` provisório do `issue.edited` pela resposta definitiva: `202 accepted` com `eventId` e `links.result` (D9), com ou sem mensagem.
- [ ] Testes de integração: edição de issue com `issue.created` gravado (uma linha em cada tabela, payload e `dedup_key` exatos); edição sem `issue.created` (evento gravado, outbox vazia); reenvio do mesmo `issue.edited` (`duplicate_event`, sem nova mensagem); duas edições diferentes da mesma issue geram duas mensagens.

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
| P1 | Mensagem de `issue.edited` | **Decidido pelo usuário em 2026-11-12, seguindo a Ingestão Spec:** toda edição válida é gravada; se a issue já tem `issue.created`, vai para a fila `analysis` com `attempt` nulo, `trigger = author_edit` e `triggerRef` = delivery id (formato em "Contratos > Mensagem da fila `analysis`"); o `issue-triage` decide se a edição reabre a análise. High Level Design (seção 3.2) e Modelagem de dados corrigidos em 2026-11-12 (inclui o índice `triage.analysis_attempts_one_per_edit`). Situação original: a Ingestão Spec dizia que o `issue-triage` decide; o High Level Design e a Modelagem de dados diziam que o `issue-analyst-api` filtra e produz `attempt + 1` | Etapa 9 |
| P2 | Nomes das tabelas | A Entrada de issues usa `ingested_events`, `outbox` e o índice `ingested_events_one_created_per_issue`; a Modelagem de dados usa `inbound.issue_events`, `inbound.inbound_outbox` e `issue_events_one_created_per_issue`. Este plano segue a Modelagem de dados, que é a referência das colunas | — (corrigir a Entrada de issues) |
| P3 | Coluna `missing_description` | Citada na Entrada de issues e ausente no DDL da Modelagem de dados. Este plano deriva `missingDescription` de `description = ''`, sem coluna | — (corrigir a Entrada de issues) |
| P5 | Papéis e permissões do banco | O `GRANT` e o papel `analyst_api` da Modelagem de dados atravessam serviços; falta decidir onde ficam (script de inicialização do ambiente integrado) e onde mora o compose que sobe os três serviços | Não bloqueia: este plano usa o compose local e `DATABASE_URL` |

### Pontos decididos

| # | Ponto | Decisão | Etapa |
|---|---|---|---|
| P4 | Argumentos da fila `analysis` | **Decidido pelo usuário em 2026-11-10:** o `issue-analyst-api` declara `analysis` só como durável, sem nenhum argumento `x-*`, e o `issue-triage` declara a fila igual. A DLQ (`analysis.dlx`/`analysis.dlq`) fica por policy do RabbitMQ, a cargo da spec de IA e da inicialização do ambiente integrado (P5). Situação original: o High Level Design prevê DLQ para `analysis`, e a definição dela está pendente na spec de IA; a declaração precisa ter os mesmos argumentos em quem declara e em quem consome | Etapa 7 |

## Fora do escopo

API de consulta (RF27) e a view `inbound.issue_presentation`, endpoint de resolução da revisão humana e `inbound.issue_review_history`, transições de status, limpeza de dados antigos, autenticação do endpoint de revisão.

## Limitações assumidas

A ingestão depende do banco (com 503 o reenvio é manual, porque o GitHub não reenvia); descrição acima de 500 caracteres é recusada; sem limpeza de `raw_payload` e do outbox publicado; a entrada protege contra a parada da análise, não contra a queda da infraestrutura.
