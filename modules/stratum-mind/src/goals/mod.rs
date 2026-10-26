// src/goals/mod.rs — Persistent goal tree (replaces clawd-goals)

use anyhow::Result;
use colored::*;
use rusqlite::{params, Connection};

pub fn add(
    conn: &Connection,
    title: &str,
    description: Option<&str>,
    parent_id: Option<i64>,
    priority: &str,
) -> Result<i64> {
    conn.execute(
        "INSERT INTO goals (title, description, parent_id, priority) VALUES (?1, ?2, ?3, ?4)",
        params![title, description, parent_id, priority],
    )?;
    let id = conn.last_insert_rowid();
    println!("Goal [{}] added: {}", id, title.bold());
    Ok(id)
}

pub fn list(conn: &Connection, tree: bool, status_filter: Option<&str>) -> Result<()> {
    let status_clause = status_filter
        .map(|s| format!("AND status='{}'", s))
        .unwrap_or_default();

    let q = format!(
        "SELECT id, title, status, priority, parent_id, updated_at FROM goals WHERE 1=1 {} ORDER BY parent_id NULLS FIRST, CASE priority WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 ELSE 4 END",
        status_clause
    );

    let mut stmt = conn.prepare(&q)?;
    let rows: Vec<(i64, String, String, String, Option<i64>, String)> = stmt
        .query_map([], |r| {
            Ok((
                r.get(0)?,
                r.get(1)?,
                r.get(2)?,
                r.get(3)?,
                r.get(4)?,
                r.get(5)?,
            ))
        })?
        .filter_map(|r| r.ok())
        .collect();

    if rows.is_empty() {
        println!("{}", "No goals found.".dimmed());
        return Ok(());
    }

    for (id, title, status, priority, parent_id, updated_at) in &rows {
        let indent = if tree && parent_id.is_some() {
            "  └─ "
        } else {
            ""
        };
        let status_icon = match status.as_str() {
            "complete" => "✓".green(),
            "blocked" => "✗".red(),
            "cancelled" => "–".dimmed(),
            _ => "○".cyan(),
        };
        let pri_color = match priority.as_str() {
            "critical" => priority.red().bold(),
            "high" => priority.yellow(),
            _ => priority.dimmed(),
        };
        println!(
            "{}{} [{:>3}] {} {}  {}",
            indent,
            status_icon,
            id.to_string().dimmed(),
            pri_color,
            title.bold(),
            updated_at[..10].dimmed()
        );
    }
    Ok(())
}

pub fn eval(conn: &Connection, id: i64, note: &str) -> Result<bool> {
    // Append to eval_notes JSON array
    let existing: Option<String> = conn
        .query_row(
            "SELECT eval_notes FROM goals WHERE id=?1",
            params![id],
            |r| r.get(0),
        )
        .ok()
        .flatten();

    let mut notes: Vec<String> = existing
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_default();

    let timestamped = format!("[{}] {}", chrono::Utc::now().format("%Y-%m-%d"), note);
    notes.push(timestamped);
    let notes_json = serde_json::to_string(&notes)?;

    let n = conn.execute(
        "UPDATE goals SET eval_notes=?2, updated_at=datetime('now') WHERE id=?1",
        params![id, notes_json],
    )?;
    Ok(n > 0)
}

pub fn complete(conn: &Connection, id: i64, note: Option<&str>) -> Result<bool> {
    if let Some(n) = note {
        eval(conn, id, n)?;
    }
    let n = conn.execute(
        "UPDATE goals SET status='complete', updated_at=datetime('now') WHERE id=?1",
        params![id],
    )?;
    Ok(n > 0)
}

pub fn status_cmd(conn: &Connection) -> Result<()> {
    let active: i64 = conn.query_row(
        "SELECT COUNT(*) FROM goals WHERE status='active'",
        [],
        |r| r.get(0),
    )?;
    let complete: i64 = conn.query_row(
        "SELECT COUNT(*) FROM goals WHERE status='complete'",
        [],
        |r| r.get(0),
    )?;
    let blocked: i64 = conn.query_row(
        "SELECT COUNT(*) FROM goals WHERE status='blocked'",
        [],
        |r| r.get(0),
    )?;
    println!(
        "Goals: {} active, {} complete, {} blocked",
        active, complete, blocked
    );
    Ok(())
}
