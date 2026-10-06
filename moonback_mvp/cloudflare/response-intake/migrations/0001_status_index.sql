CREATE INDEX IF NOT EXISTS idx_responses_status_received_at
ON responses(status, received_at, id);
