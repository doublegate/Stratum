// stratum-watch — Stratum observability hub
//
// Aggregates: cron-health, context-watch, buffer, version drift, observer
// All read from existing tool DBs/files — does not replace running daemons.
// Database: ~/.local/share/stratum/watch.db (version checks, observations)
//
// Usage:
//   stratum-watch status              — full dashboard
//   stratum-watch cron status         — cron health summary
//   stratum-watch context status      — context window usage
//   stratum-watch buffer status       — buffer queue status
//   stratum-watch version check       — check for OpenClaw/Node version drift
//   stratum-watch observe git <dir>   — check git repo for new commits since last observation
//   stratum-watch observe status      — show recent observations

use anyhow::Result;
use clap::{Parser, Subcommand};
use colored::Colorize;
use dirs::{data_dir, home_dir};
use rusqlite::{Connection, params};
use std::path::PathBuf;
use std::process::Command;

fn db_path() -> PathBuf {
    data_dir().unwrap_or_else(|| PathBuf::from("~/.local/share"))
        .join("stratum").join("watch.db")
}

fn open_db() -> Result<Connection> {
    let path = db_path();
    std::fs::create_dir_all(path.parent().unwrap())?;
    let conn = Connection::open(&path)?;
    conn.execute_batch("PRAGMA journal_mode=WAL;")?;
    conn.execute_batch("
        CREATE TABLE IF NOT EXISTS version_checks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            tool        TEXT NOT NULL,
            version     TEXT NOT NULL,
            checked_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS observations (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            source      TEXT NOT NULL,  -- git:<path>, file:<path>, process:<name>
            event       TEXT NOT NULL,
            detail      TEXT,
            observed_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_obs_source ON observations(source, observed_at);
        CREATE INDEX IF NOT EXISTS idx_ver_tool ON version_checks(tool, checked_at);
    ")?;
    Ok(conn)
}

#[derive(Parser)]
#[command(name = "stratum-watch", about = "Stratum observability hub", version = "0.1.0")]
struct Cli {
    #[command(subcommand)]
    command: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Full observability dashboard
    Status,
    /// Cron health
    Cron {
        #[command(subcommand)]
        action: CronAction,
    },
    /// Context window
    Context {
        #[command(subcommand)]
        action: ContextAction,
    },
    /// Sub-agent output buffer
    Buffer {
        #[command(subcommand)]
        action: BufferAction,
    },
    /// Version drift detection
    Version {
        #[command(subcommand)]
        action: VersionAction,
    },
    /// Active observer (git, file, process)
    Observe {
        #[command(subcommand)]
        action: ObserveAction,
    },
}

#[derive(Subcommand)]
enum CronAction { Status }
#[derive(Subcommand)]
enum ContextAction { Status }
#[derive(Subcommand)]
enum BufferAction { Status }
#[derive(Subcommand)]
enum VersionAction {
    Check,
    Status,
}
#[derive(Subcommand)]
enum ObserveAction {
    /// Check a git repo for new commits
    Git { path: String },
    /// Show recent observations
    Status,
}

fn cron_status() -> Result<()> {
    let home = home_dir().unwrap();
    let db = home.join(".local/share/stratum/watch.db");
    if !db.exists() {
        println!("{}", "No cron history DB found".dimmed());
        return Ok(());
    }
    let conn = Connection::open(&db)?;
    let rows: Vec<(String, String, String)> = {
        let mut stmt = conn.prepare(
            "SELECT cron_name, status, strftime('%Y-%m-%d %H:%M', MAX(scanned_at), 'unixepoch') as last_run FROM cron_runs GROUP BY cron_name ORDER BY MAX(scanned_at) DESC LIMIT 10"
        ).unwrap_or_else(|_| conn.prepare("SELECT 'n/a', 'n/a', 'n/a' WHERE 0").unwrap());
        stmt.query_map([], |r| Ok((r.get::<_,String>(0)?, r.get::<_,String>(1)?, r.get::<_,String>(2)?)))
            .map(|rows| rows.filter_map(|r| r.ok()).collect::<Vec<_>>())
            .unwrap_or_default()
    };
    if rows.is_empty() {
        println!("{}", "No cron runs recorded".dimmed());
    } else {
        for (name, status, last) in rows {
            let icon = if status == "success" { "✓".green() } else if status == "failure" { "✗".red() } else { "?".dimmed() };
            println!("  {} {} — {}", icon, name.bold(), last.dimmed());
        }
    }
    Ok(())
}

fn context_status() -> Result<()> {
    let home = home_dir().unwrap();
    let f = home.join(".local/share/clawd-context-watch/status.json");
    if !f.exists() {
        println!("{}", "clawd-context-watch not running".dimmed());
        return Ok(());
    }
    let data: serde_json::Value = serde_json::from_str(&std::fs::read_to_string(&f)?)?;
    let pct = data["pct"].as_f64().unwrap_or(0.0);
    let level = data["level"].as_str().unwrap_or("unknown");
    let bar_full = (pct / 5.0) as usize;
    let bar = format!("{}{}", "█".repeat(bar_full), "░".repeat(20usize.saturating_sub(bar_full)));
    let colored_bar = match level {
        "high" | "critical" => bar.red(),
        "medium" => bar.yellow(),
        _ => bar.green(),
    };
    println!("  Context: {} {:.0}% ({})", colored_bar, pct, level);
    Ok(())
}

fn buffer_status() -> Result<()> {
    let home = home_dir().unwrap();
    let db = home.join(".local/share/clawd-buffer/buffer.db");
    if !db.exists() {
        println!("{}", "clawd-buffer DB not found".dimmed());
        return Ok(());
    }
    let conn = Connection::open(&db)?;
    let total: i64 = conn.query_row("SELECT COUNT(*) FROM results", [], |r| r.get(0)).unwrap_or(0);
    let unacked: i64 = conn.query_row("SELECT COUNT(*) FROM results WHERE status='captured'", [], |r| r.get(0)).unwrap_or(0);
    let icon = if unacked > 50 { "⚠".yellow() } else { "✓".green() };
    println!("  {} Buffer: {} total, {} unacknowledged", icon, total, unacked);
    Ok(())
}

fn version_check(conn: &Connection) -> Result<()> {
    println!("{}", "Version checks:".bold());

    // OpenClaw
    let out = Command::new("openclaw").arg("--version").output();
    if let Ok(o) = out {
        let ver = String::from_utf8_lossy(&o.stdout).trim().to_string();
        conn.execute("INSERT INTO version_checks (tool, version) VALUES ('openclaw', ?1)", params![&ver])?;
        println!("  openclaw: {}", ver.green());
    }

    // Node
    let out = Command::new("node").arg("--version").output();
    if let Ok(o) = out {
        let ver = String::from_utf8_lossy(&o.stdout).trim().to_string();
        conn.execute("INSERT INTO version_checks (tool, version) VALUES ('node', ?1)", params![&ver])?;
        println!("  node: {}", ver.green());
    }

    // stratum-mind
    let out = Command::new("stratum-mind").arg("--version").output();
    if let Ok(o) = out {
        let ver = String::from_utf8_lossy(&o.stdout).trim().to_string();
        println!("  stratum-mind: {}", ver.green());
    }

    Ok(())
}

fn observe_git(conn: &Connection, path: &str) -> Result<()> {
    let out = Command::new("git")
        .args(["-C", path, "log", "--oneline", "-5"])
        .output()?;
    let log = String::from_utf8_lossy(&out.stdout).trim().to_string();
    let source = format!("git:{}", path);

    // Check if this is new vs last observation
    let last: Option<String> = conn.query_row(
        "SELECT detail FROM observations WHERE source=?1 ORDER BY observed_at DESC LIMIT 1",
        params![&source], |r| r.get(0)
    ).ok().flatten();

    let is_new = last.as_deref() != Some(&log);
    if is_new && !log.is_empty() {
        conn.execute(
            "INSERT INTO observations (source, event, detail) VALUES (?1, 'new_commits', ?2)",
            params![&source, &log],
        )?;
        println!("  {} New commits in {}:", "↑".green(), path);
        for line in log.lines().take(3) { println!("    {}", line.dimmed()); }
    } else {
        println!("  {} {} — no new commits", "✓".dimmed(), path);
    }
    Ok(())
}

fn observe_status(conn: &Connection) -> Result<()> {
    let mut stmt = conn.prepare(
        "SELECT source, event, detail, observed_at FROM observations ORDER BY observed_at DESC LIMIT 10"
    )?;
    let rows: Vec<_> = stmt.query_map([], |r| {
        Ok((r.get::<_,String>(0)?, r.get::<_,String>(1)?, r.get::<_,Option<String>>(2)?, r.get::<_,String>(3)?))
    })?.filter_map(|r| r.ok()).collect();

    if rows.is_empty() {
        println!("{}", "No observations recorded yet".dimmed());
    } else {
        for (source, event, detail, at) in rows {
            println!("  {} {} — {}  {}", "·".dimmed(), source.bold(), event, &at[..16].dimmed());
            if let Some(d) = detail { println!("    {}", d.lines().next().unwrap_or("").dimmed()); }
        }
    }
    Ok(())
}

fn full_status(conn: &Connection) -> Result<()> {
    println!("{}", "=== stratum-watch status ===".bold());
    println!("{}", "Cron Health:".bold());
    cron_status()?;
    println!("{}", "Context Window:".bold());
    context_status()?;
    println!("{}", "Buffer:".bold());
    buffer_status()?;
    println!("{}", "Recent Observations:".bold());
    observe_status(conn)?;
    Ok(())
}

fn main() -> Result<()> {
    let conn = open_db()?;
    let cli = Cli::parse();
    match cli.command {
        Cmd::Status => full_status(&conn)?,
        Cmd::Cron { action: CronAction::Status } => cron_status()?,
        Cmd::Context { action: ContextAction::Status } => context_status()?,
        Cmd::Buffer { action: BufferAction::Status } => buffer_status()?,
        Cmd::Version { action } => match action {
            VersionAction::Check | VersionAction::Status => version_check(&conn)?,
        },
        Cmd::Observe { action } => match action {
            ObserveAction::Git { path } => observe_git(&conn, &path)?,
            ObserveAction::Status => observe_status(&conn)?,
        },
    }
    Ok(())
}
