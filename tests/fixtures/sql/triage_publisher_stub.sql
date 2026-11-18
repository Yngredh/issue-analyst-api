CREATE SCHEMA triage;
CREATE SCHEMA publisher;

CREATE TABLE triage.issue_cases (
  id               bigserial PRIMARY KEY,
  repository       text NOT NULL,
  issue_number     int  NOT NULL,
  origin_event_id  text NOT NULL UNIQUE,           -- primeiro issue.created da issue
  status           text NOT NULL CHECK (status IN (
                     'ANALYZING', 'NEEDS_REVIEW', 'MAX_RETRIES_REACHED',
                     'READY_TO_PUBLISH', 'CANCELLED', 'FINISHED')),
  status_reason    text NOT NULL,                  -- motivo do status (combinação no CHECK abaixo)
  category         text CHECK (category IN ('bug', 'melhoria', 'duvida', 'indeterminado')),
  current_attempt  int NOT NULL DEFAULT 1 CHECK (current_attempt >= 1),
  manual_attempts  int NOT NULL DEFAULT 0 CHECK (manual_attempts >= 0),  -- limite: MAX_MANUAL_ATTEMPTS
  closed_by_review_id  bigint,                 -- inbound.issue_review_history.id
  created_at       timestamptz NOT NULL DEFAULT now(),
  updated_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (repository, issue_number),               -- um caso por issue
  CHECK (
    (status = 'ANALYZING'              AND status_reason IN ('initial', 'author_edit', 'enrich', 'reprocess'))
    OR (status = 'READY_TO_PUBLISH'    AND status_reason = 'empty_description')
    OR (status = 'READY_TO_PUBLISH'    AND status_reason = 'proposal_validated')
    OR (status = 'NEEDS_REVIEW'        AND status_reason IN ('unknown_repo', 'insufficient_content',
                                                           'invalid_proposal', 'technical_failure'))
    OR (status = 'MAX_RETRIES_REACHED' AND status_reason = 'manual_attempts_exhausted')
    OR (status IN ('CANCELLED', 'FINISHED') AND status_reason = 'closed_by_reviewer')
  ),
  CHECK (status NOT IN ('CANCELLED', 'FINISHED') OR closed_by_review_id IS NOT NULL)
);

CREATE TABLE triage.analysis_attempts (
  id           bigserial PRIMARY KEY,
  case_id      bigint NOT NULL REFERENCES triage.issue_cases (id),
  event_id     text NOT NULL,                      -- origin_event_id do caso
  attempt      int  NOT NULL CHECK (attempt >= 1),
  trigger      text NOT NULL CHECK (trigger IN ('initial', 'author_edit', 'enrich', 'reprocess')),
  trigger_ref  text,                               -- eventId do issues.edited ou id da resolução
  state        text NOT NULL CHECK (state IN ('IN_PROGRESS', 'DONE')),
  outcome      text,                               -- status do caso ao terminar a tentativa
  error        text,                               -- motivo da falha da tentativa, técnica ou do modelo
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  UNIQUE (event_id, attempt)                       -- chave de deduplicação da fila analysis
);

CREATE UNIQUE INDEX analysis_attempts_one_per_edit  -- reentrega da mesma edição não abre outra tentativa
  ON triage.analysis_attempts (event_id, trigger_ref) WHERE trigger = 'author_edit';

CREATE TABLE triage.proposals (
  id                 bigserial PRIMARY KEY,
  attempt_id         bigint NOT NULL UNIQUE REFERENCES triage.analysis_attempts (id),
  kind               text NOT NULL CHECK (kind IN ('triage', 'missing_info')),
  specialist         text CHECK (specialist IN ('bug', 'melhoria', 'dominio')),
  category           text CHECK (category IN ('bug', 'melhoria', 'duvida')),
  summary            text,
  next_step          text,                         -- próximo passo sugerido (RF16)
  conclusion         text CHECK (conclusion IN ('procede', 'nao_procede', 'sem_informacao')),
  conclusion_reason  text,                         -- por que o relato procede ou não
  answer             text,                         -- só no especialista de domínio
  evidence           jsonb NOT NULL DEFAULT '[]',  -- trechos do evento ou do catálogo
  tool_calls         jsonb NOT NULL DEFAULT '[]',  -- ferramenta, argumentos, duração, chamada 1 ou 2 (RNF11)
  raw_output         jsonb,                        -- lista com a saída bruta de cada chamada e o motivo da reprovação (RF31)
  validation_status  text NOT NULL CHECK (validation_status IN ('VALID', 'INVALID')),
  validation_error   text,                         -- motivo da reprovação
  comment_body       text,                         -- texto final do template, com marcador oculto
  prompt_version     text,
  model              text,
  created_at         timestamptz NOT NULL DEFAULT now(),
  CHECK (kind = 'missing_info' OR specialist IS NOT NULL),
  CHECK (validation_status = 'VALID' OR validation_error IS NOT NULL),
  CHECK (validation_status = 'INVALID' OR comment_body IS NOT NULL),
  CHECK (answer IS NULL OR specialist = 'dominio')  -- campos obrigatórios da proposta: validador (Pydantic)
);

CREATE TABLE publisher.publications (
  id                  bigserial PRIMARY KEY,
  repository          text NOT NULL,
  issue_number        int  NOT NULL,
  revision            int  NOT NULL,              -- attempt que gerou a proposta
  proposal_id         bigint NOT NULL,            -- triage.proposals.id
  origin_event_id     text NOT NULL,              -- vai no marcador oculto do comentário
  kind                text NOT NULL CHECK (kind IN ('triage', 'missing_info')),
  state               text NOT NULL CHECK (state IN ('PENDING', 'PUBLISHED', 'FAILED')),
  tries               smallint NOT NULL DEFAULT 0 CHECK (tries BETWEEN 0 AND 3),
  failure_kind        text CHECK (failure_kind IN ('retries_exhausted', 'permanent')),
  last_error          text,
  github_comment_id   bigint,
  github_comment_url  text,
  published_at        timestamptz,
  created_at          timestamptz NOT NULL DEFAULT now(),
  updated_at          timestamptz NOT NULL DEFAULT now(),
  UNIQUE (repository, issue_number, revision),    -- chave de deduplicação da publicação
  CHECK (state <> 'PUBLISHED' OR (github_comment_id IS NOT NULL AND github_comment_url IS NOT NULL)),
  CHECK (state <> 'FAILED' OR failure_kind IS NOT NULL)
);
