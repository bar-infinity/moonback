-- Dedicated observation DB only. Existing rows/content remain unchanged.
CREATE INDEX IF NOT EXISTS idx_observation_history
ON observation_events ((substr(time,1,19)||'.'||substr((CASE WHEN substr(time,20,1)='.' THEN replace(replace(substr(time,21),'+00:00',''),'Z','') ELSE '' END)||'000000',1,6)||'Z'),event_id);
