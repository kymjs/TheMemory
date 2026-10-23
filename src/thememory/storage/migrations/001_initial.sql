-- memory.db schema v1

CREATE TABLE schema_version (
  version    INTEGER NOT NULL,
  applied_at TEXT    NOT NULL
);

CREATE TABLE memory_items (
  seq               INTEGER PRIMARY KEY AUTOINCREMENT,
  id                TEXT    NOT NULL UNIQUE,
  peer_id           TEXT    NOT NULL DEFAULT 'local',
  observer          TEXT    NOT NULL,
  scope             TEXT    NOT NULL DEFAULT 'user',
  type              TEXT    NOT NULL CHECK (type IN
                      ('profile_fact','preference','pattern','note','procedural','event','plan')),
  slot_key          TEXT,
  content           TEXT    NOT NULL,
  source_quote      TEXT,
  keywords          TEXT    NOT NULL DEFAULT '[]',
  aliases           TEXT    NOT NULL DEFAULT '[]',
  fts_text          TEXT    NOT NULL DEFAULT '',
  origin            TEXT    NOT NULL CHECK (origin IN
                      ('user_requested','user_stated','agent_inferred','extracted')),
  provenance        TEXT    NOT NULL CHECK (provenance IN
                      ('owner','agent','untrusted','system')),
  status            TEXT    NOT NULL CHECK (status IN
                      ('candidate','active','superseded','retracted','deleted')),
  promoted          INTEGER NOT NULL DEFAULT 0,
  hold_reason       TEXT,
  flags             TEXT    NOT NULL DEFAULT '[]',
  evidence_count    INTEGER NOT NULL DEFAULT 0,
  last_reinforced_at TEXT,
  observed_at       TEXT    NOT NULL,
  valid_from        TEXT,
  valid_to          TEXT,
  supersedes_id     TEXT,
  superseded_by     TEXT,
  content_hash      TEXT    NOT NULL,
  source_host       TEXT    NOT NULL,
  source_session    TEXT,
  created_at        TEXT    NOT NULL,
  updated_at        TEXT    NOT NULL
);
CREATE INDEX idx_items_vis    ON memory_items (status, scope, type);
CREATE INDEX idx_items_slot   ON memory_items (slot_key, status);
CREATE INDEX idx_items_hash   ON memory_items (content_hash);
CREATE INDEX idx_items_window ON memory_items (valid_from, valid_to) WHERE type IN ('event','plan');
CREATE INDEX idx_items_obs    ON memory_items (observer, status);

CREATE VIRTUAL TABLE item_fts USING fts5(
  fts_text,
  content='memory_items', content_rowid='seq',
  tokenize='unicode61'
);

CREATE TABLE item_embeddings (
  item_id    TEXT PRIMARY KEY REFERENCES memory_items(id),
  model_id   TEXT    NOT NULL,
  dim        INTEGER NOT NULL,
  vector     BLOB    NOT NULL,
  created_at TEXT    NOT NULL
);

CREATE TABLE tombstones (
  item_id      TEXT PRIMARY KEY,
  type         TEXT NOT NULL,
  deleted_at   TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  model_id     TEXT,
  dim          INTEGER,
  fp_vector    BLOB
);
CREATE INDEX idx_tomb_hash ON tombstones (content_hash);

CREATE TABLE item_evidence (
  item_id  TEXT NOT NULL REFERENCES memory_items(id),
  turn_id  TEXT NOT NULL,
  role     TEXT NOT NULL CHECK (role IN ('user','assistant')),
  quote    TEXT,
  PRIMARY KEY (item_id, turn_id)
);

CREATE TABLE changes (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             TEXT NOT NULL,
  item_id        TEXT,
  op             TEXT NOT NULL,
  actor          TEXT NOT NULL,
  prompt_version TEXT,
  proposal       TEXT,
  result         TEXT,
  reason         TEXT
);
CREATE INDEX idx_changes_item ON changes (item_id, ts);

CREATE TABLE sessions (
  id               TEXT PRIMARY KEY,
  host             TEXT NOT NULL,
  host_session_id  TEXT NOT NULL,
  project_key      TEXT,
  started_at       TEXT NOT NULL,
  last_event_at    TEXT NOT NULL,
  ended_at         TEXT,
  tz               TEXT,
  incognito        INTEGER NOT NULL DEFAULT 0,
  snapshot         TEXT,
  snapshot_hash    TEXT,
  snapshot_seq     INTEGER,
  extract_cursor   INTEGER NOT NULL DEFAULT 0,
  summary          TEXT,
  summary_at       TEXT,
  UNIQUE (host, host_session_id)
);

CREATE TABLE injection_ledger (
  session_id  TEXT NOT NULL,
  item_id     TEXT NOT NULL,
  channel     TEXT NOT NULL CHECK (channel IN ('session_start','per_turn','context_tool')),
  injected_at TEXT NOT NULL,
  PRIMARY KEY (session_id, item_id)
);

CREATE TABLE jobs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  kind        TEXT NOT NULL,
  payload     TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('pending','running','done','failed')),
  attempts    INTEGER NOT NULL DEFAULT 0,
  next_run_at TEXT,
  last_error  TEXT,
  created_at  TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

CREATE TABLE inbox (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  received_at TEXT NOT NULL,
  host        TEXT NOT NULL,
  idem_key    TEXT NOT NULL UNIQUE,
  payload     TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'pending'
);

CREATE TABLE usage_log (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                TEXT NOT NULL,
  purpose           TEXT NOT NULL,
  model             TEXT NOT NULL,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  ok                INTEGER NOT NULL,
  latency_ms        INTEGER
);

CREATE TABLE degrade_events (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts         TEXT NOT NULL,
  component  TEXT NOT NULL,
  reason     TEXT NOT NULL,
  detail     TEXT,
  host       TEXT,
  session_id TEXT
);
CREATE INDEX idx_degrade_ts ON degrade_events (ts);
