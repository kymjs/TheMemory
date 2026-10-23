-- turns.db schema v1

CREATE TABLE schema_version (
  version    INTEGER NOT NULL,
  applied_at TEXT    NOT NULL
);

CREATE TABLE turns (
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  id              TEXT NOT NULL UNIQUE,
  session_id      TEXT NOT NULL,
  host            TEXT NOT NULL,
  host_session_id TEXT NOT NULL,
  host_turn_id    TEXT NOT NULL,
  role            TEXT NOT NULL CHECK (role IN ('user','assistant')),
  content         TEXT,
  content_hash    TEXT NOT NULL,
  created_at      TEXT NOT NULL,
  tz              TEXT,
  local_date      TEXT,
  extracted       INTEGER NOT NULL DEFAULT 0,
  protected       INTEGER NOT NULL DEFAULT 0,
  evicted         INTEGER NOT NULL DEFAULT 0,
  size_bytes      INTEGER NOT NULL,
  UNIQUE (host, host_session_id, host_turn_id, role, content_hash)
);
CREATE INDEX idx_turns_session ON turns (session_id, seq);
CREATE INDEX idx_turns_pending ON turns (extracted, seq) WHERE extracted = 0;

CREATE VIRTUAL TABLE turns_fts USING fts5(
  content,
  content='turns', content_rowid='seq',
  tokenize='unicode61'
);
