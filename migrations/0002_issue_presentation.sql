-- View de consulta do resultado (RF27, D14, D16), conforme a Modelagem de dados.
-- Só leitura: lê triage e publisher e não escreve em nenhuma tabela deles (RNF07).
-- Estrita (D15): falha se os schemas triage e publisher ainda não foram migrados.
CREATE VIEW inbound.issue_presentation AS
WITH first_event AS (
  SELECT DISTINCT ON (repository, issue_number) *
  FROM inbound.issue_events
  WHERE type = 'issue.created'
  ORDER BY repository, issue_number, received_at
)
SELECT
  e.repository,
  e.issue_number,
  e.event_id,
  CASE
    WHEN c.id IS NULL THEN 'ACCEPTED'                         -- aceito, ainda não analisado
    WHEN pub.state = 'FAILED' AND pub.revision = c.current_attempt
         AND c.status = 'READY_TO_PUBLISH' THEN 'NEEDS_REVIEW'
    ELSE c.status
  END AS status,
  CASE
    WHEN c.id IS NULL THEN 'awaiting_analysis'
    WHEN pub.state = 'FAILED' AND pub.revision = c.current_attempt
         AND c.status = 'READY_TO_PUBLISH' THEN 'technical_failure'
    ELSE c.status_reason
  END AS status_reason,
  c.category,
  c.current_attempt,
  c.manual_attempts,
  p.specialist,
  p.summary,
  p.next_step,
  p.conclusion,
  p.conclusion_reason,
  p.answer,
  pub.state              AS publication_state,
  pub.github_comment_url,
  pub.published_at
FROM first_event e
LEFT JOIN triage.issue_cases c ON c.origin_event_id = e.event_id
LEFT JOIN LATERAL (
  SELECT pr.* FROM triage.proposals pr
  JOIN triage.analysis_attempts a ON a.id = pr.attempt_id
  WHERE a.case_id = c.id AND pr.validation_status = 'VALID'
  ORDER BY a.attempt DESC LIMIT 1
) p ON true
LEFT JOIN LATERAL (
  SELECT * FROM publisher.publications x
  WHERE x.repository = e.repository AND x.issue_number = e.issue_number
  ORDER BY x.revision DESC LIMIT 1
) pub ON true;
