-- memory.db schema v2: session contamination + daemon meta (pause)

ALTER TABLE sessions ADD COLUMN contaminated INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN contaminated_at TEXT;

CREATE TABLE IF NOT EXISTS daemon_meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
