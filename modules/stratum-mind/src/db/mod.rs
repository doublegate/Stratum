// src/db/mod.rs — Unified SQLite schema for stratum-mind
// Single database: ~/.local/share/stratum/mind.db
// Tables: stash, lessons, entities, relations, beliefs, goals, memory_tiers

use anyhow::Result;
use dirs::data_dir;
use rusqlite::{Connection, params};
use std::path::PathBuf;

pub fn db_path() -> PathBuf {
    let base = data_dir().unwrap_or_else(|| PathBuf::from("~/.local/share"));
    base.join("stratum").join("mind.db")
}

pub fn open() -> Result<Connection> {
    let path = db_path();
    std::fs::create_dir_all(path.parent().unwrap())?;
    let conn = Connection::open(&path)?;
    conn.execute_batch("PRAGMA journal_mode=WAL; PRAGMA foreign_keys=ON;")?;
    migrate(&conn)?;
    Ok(conn)
}

fn migrate(conn: &Connection) -> Result<()> {
    conn.execute_batch("
        CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);

        CREATE TABLE IF NOT EXISTS stash (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            content     TEXT NOT NULL,
            priority    TEXT NOT NULL DEFAULT 'normal',  -- urgent|high|normal|low
            tags        TEXT,                             -- JSON array
            done        INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS lessons (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            content     TEXT NOT NULL,
            category    TEXT NOT NULL DEFAULT 'correction', -- correction|discovery|workflow|api_change|bug|insight
            severity    TEXT NOT NULL DEFAULT 'medium',     -- low|medium|high|critical
            source      TEXT,
            resolved    INTEGER NOT NULL DEFAULT 0,
            resolve_note TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS entities (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            entity_type TEXT NOT NULL DEFAULT 'concept',   -- project|person|tool|system|concept
            description TEXT,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS relations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            subject     TEXT NOT NULL,
            predicate   TEXT NOT NULL,
            object      TEXT NOT NULL,
            confidence  REAL NOT NULL DEFAULT 1.0,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(subject, predicate, object)
        );

        CREATE TABLE IF NOT EXISTS beliefs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            entity      TEXT NOT NULL,
            attribute   TEXT NOT NULL,
            value       TEXT NOT NULL,
            confidence  REAL NOT NULL DEFAULT 1.0,
            evidence    TEXT,
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(entity, attribute)
        );

        CREATE TABLE IF NOT EXISTS goals (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            title       TEXT NOT NULL,
            description TEXT,
            parent_id   INTEGER REFERENCES goals(id),
            status      TEXT NOT NULL DEFAULT 'active',    -- active|complete|blocked|cancelled
            priority    TEXT NOT NULL DEFAULT 'medium',
            eval_notes  TEXT,                              -- JSON array of evaluation notes
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS memory_tiers (
            key         TEXT PRIMARY KEY,
            tier        TEXT NOT NULL DEFAULT 'warm',      -- hot|warm|cold
            last_access TEXT NOT NULL DEFAULT (datetime('now')),
            access_count INTEGER NOT NULL DEFAULT 0,
            notes       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_lessons_severity   ON lessons(severity, resolved);
        CREATE INDEX IF NOT EXISTS idx_lessons_category   ON lessons(category);
        CREATE INDEX IF NOT EXISTS idx_stash_priority     ON stash(priority, done);
        CREATE INDEX IF NOT EXISTS idx_beliefs_entity     ON beliefs(entity);
        CREATE INDEX IF NOT EXISTS idx_goals_status       ON goals(status, priority);
        CREATE INDEX IF NOT EXISTS idx_relations_subject  ON relations(subject);
    ")?;

    // Migration v2: belief decay columns + FTS5 indices + consolidation log
    migrate_v2(conn)?;

    Ok(())
}

fn migrate_v2(conn: &Connection) -> Result<()> {
    // Add decay tracking columns to beliefs if they don't exist yet
    let cols: Vec<String> = {
        let mut stmt = conn.prepare("PRAGMA table_info(beliefs)")?;
        let rows: Vec<String> = stmt.query_map([], |r| r.get::<_, String>(1))?
            .filter_map(|r| r.ok())
            .collect();
        rows
    };

    if !cols.contains(&"last_verified".to_string()) {
        conn.execute_batch(
            "ALTER TABLE beliefs ADD COLUMN last_verified TEXT;
             ALTER TABLE beliefs ADD COLUMN decay_rate REAL NOT NULL DEFAULT 0.05;
             ALTER TABLE beliefs ADD COLUMN stale INTEGER NOT NULL DEFAULT 0;"
        )?;
        // Backfill last_verified with updated_at for existing rows
        conn.execute_batch("UPDATE beliefs SET last_verified = updated_at WHERE last_verified IS NULL;")?;
    }

    // FTS5 virtual table over beliefs (keyword search)
    conn.execute_batch(
        "CREATE VIRTUAL TABLE IF NOT EXISTS beliefs_fts
         USING fts5(entity, attribute, value, content=beliefs, content_rowid=id);"
    )?;

    // FTS5 virtual table over lessons
    conn.execute_batch(
        "CREATE VIRTUAL TABLE IF NOT EXISTS lessons_fts
         USING fts5(content, category, content=lessons, content_rowid=id);"
    )?;

    // Consolidation run log
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS consolidation_log (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ran_at       TEXT NOT NULL DEFAULT (datetime('now')),
            decayed      INTEGER NOT NULL DEFAULT 0,
            stale_marked INTEGER NOT NULL DEFAULT 0,
            conflicts    INTEGER NOT NULL DEFAULT 0,
            fts_rebuilt  INTEGER NOT NULL DEFAULT 1,
            summary      TEXT
        );"
    )?;

    Ok(())
}
