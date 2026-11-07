-- Apply ONLY to a separately provisioned observation database; never responses DB.
CREATE TABLE observation_events (
  event_id TEXT PRIMARY KEY,
  schema_version INTEGER NOT NULL CHECK (schema_version = 1),
  time TEXT NOT NULL,
  path TEXT NOT NULL,
  method TEXT NOT NULL,
  status INTEGER,
  response_completed INTEGER NOT NULL,
  ua TEXT NOT NULL,
  agent_like INTEGER NOT NULL,
  payload_hash TEXT NOT NULL
);
