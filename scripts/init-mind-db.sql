-- init-mind-db.sql — Initialize stratum-mind database schema
-- Run: sqlite3 ~/.local/share/stratum/mind.db < scripts/init-mind-db.sql

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS lessons (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'discovery',
    severity    TEXT NOT NULL DEFAULT 'medium',
    resolved    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS stash (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    content     TEXT NOT NULL,
    priority    TEXT NOT NULL DEFAULT 'normal',
    resolved    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS goals (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    parent_id   INTEGER REFERENCES goals(id),
    priority    TEXT NOT NULL DEFAULT 'medium',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS goal_evaluations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id     INTEGER NOT NULL REFERENCES goals(id),
    note        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS entities (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    type        TEXT NOT NULL DEFAULT 'concept',
    description TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS relations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER NOT NULL REFERENCES entities(id),
    target_id   INTEGER NOT NULL REFERENCES entities(id),
    relation_type TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS beliefs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id   INTEGER NOT NULL REFERENCES entities(id),
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    confidence  REAL NOT NULL DEFAULT 1.0,
    evidence    TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS memory_tiers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    key           TEXT NOT NULL UNIQUE,
    tier          TEXT NOT NULL DEFAULT 'warm',
    notes         TEXT,
    last_access   TEXT NOT NULL DEFAULT (datetime('now')),
    access_count  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS consolidation_log (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at             TEXT NOT NULL DEFAULT (datetime('now')),
    lessons_processed  INTEGER DEFAULT 0,
    beliefs_decayed    INTEGER DEFAULT 0,
    summary            TEXT
);

-- FTS5 virtual tables for full-text search
CREATE VIRTUAL TABLE IF NOT EXISTS lessons_fts USING fts5(
    content, category, severity,
    content=lessons, content_rowid=id
);

CREATE VIRTUAL TABLE IF NOT EXISTS stash_fts USING fts5(
    content,
    content=stash, content_rowid=id
);

-- Triggers to keep FTS in sync
CREATE TRIGGER IF NOT EXISTS lessons_ai AFTER INSERT ON lessons BEGIN
    INSERT INTO lessons_fts(rowid, content, category, severity)
    VALUES (new.id, new.content, new.category, new.severity);
END;

CREATE TRIGGER IF NOT EXISTS stash_ai AFTER INSERT ON stash BEGIN
    INSERT INTO stash_fts(rowid, content) VALUES (new.id, new.content);
END;

-- Indexes
CREATE INDEX IF NOT EXISTS idx_lessons_severity  ON lessons(severity);
CREATE INDEX IF NOT EXISTS idx_lessons_resolved  ON lessons(resolved);
CREATE INDEX IF NOT EXISTS idx_lessons_category  ON lessons(category);
CREATE INDEX IF NOT EXISTS idx_goals_status      ON goals(status);
CREATE INDEX IF NOT EXISTS idx_beliefs_entity    ON beliefs(entity_id);
CREATE INDEX IF NOT EXISTS idx_beliefs_key       ON beliefs(key);
