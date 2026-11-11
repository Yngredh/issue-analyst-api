-- Schema inbound do issue-analyst-api (conforme a Modelagem de dados).
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
