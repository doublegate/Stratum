// src/memory/mod.rs — Memory tier governance (replaces clawd-memory)
//
// Manages hot/warm/cold tier assignments for memory files.
// Hot tier = MEMORY.md (loaded every session, budget: ~2000 words / ~12KB)
// Warm tier = memory/warm/*.md (loaded on demand, per-topic)
// Cold tier = memory/YYYY-MM-DD.md daily notes (archived, query via stratum-brain)
//
// The `weekly` subcommand is the primary entry point for the Memory-Tier-Rebalance cron.

use anyhow::Result;
use colored::*;
use rusqlite::{params, Connection};

/// Word count for a file (approx)
fn word_count(path: &std::path::Path) -> usize {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .split_whitespace()
        .count()
}

pub fn status(conn: &Connection) -> Result<()> {
    let hot: i64 = conn.query_row(
        "SELECT COUNT(*) FROM memory_tiers WHERE tier='hot'",
        [],
        |r| r.get(0),
    )?;
    let warm: i64 = conn.query_row(
        "SELECT COUNT(*) FROM memory_tiers WHERE tier='warm'",
        [],
        |r| r.get(0),
    )?;
    let cold: i64 = conn.query_row(
        "SELECT COUNT(*) FROM memory_tiers WHERE tier='cold'",
        [],
        |r| r.get(0),
    )?;

    let ws = std::env::var("STRATUM_WORKSPACE")
        .unwrap_or_else(|_| format!("{}/clawd", dirs::home_dir().unwrap_or_default().display()));
    let memory_path = std::path::PathBuf::from(&ws).join("MEMORY.md");
    let words = word_count(&memory_path);
    let size_kb = std::fs::metadata(&memory_path)
        .map(|m| m.len())
        .unwrap_or(0)
        / 1024;

    let health = if words > 2000 {
        format!(" ⚠ OVER BUDGET ({} words > 2000)", words)
            .red()
            .to_string()
    } else if words > 1500 {
        format!(" ⚠ approaching limit ({} words)", words)
            .yellow()
            .to_string()
    } else {
        format!(" ✓ healthy ({} words)", words).green().to_string()
    };

    println!(
        "Memory tier registry: {} hot, {} warm, {} cold",
        hot.to_string().red(),
        warm.to_string().yellow(),
        cold.to_string().blue()
    );
    println!("MEMORY.md: {}KB{}", size_kb, health);
    Ok(())
}

/// Weekly rebalance: check MEMORY.md budget, scan warm files, suggest/apply demotions.
pub fn weekly(conn: &Connection) -> Result<()> {
    let home = dirs::home_dir().unwrap_or_default();
    let ws =
        std::env::var("STRATUM_WORKSPACE").unwrap_or_else(|_| format!("{}/clawd", home.display()));
    let memory_path = std::path::PathBuf::from(&ws).join("MEMORY.md");
    let warm_dir = std::path::PathBuf::from(&ws).join("memory/warm");

    let hot_words = word_count(&memory_path);
    println!("=== Memory Tier Rebalance ===");
    println!("MEMORY.md: {} words (budget: 2000)", hot_words);

    // Register known warm files into memory_tiers if not already tracked
    if warm_dir.exists() {
        if let Ok(entries) = std::fs::read_dir(&warm_dir) {
            for entry in entries.flatten() {
                let path = entry.path();
                if path.extension().map(|e| e == "md").unwrap_or(false) {
                    let key = format!(
                        "memory/warm/{}",
                        path.file_name().unwrap_or_default().to_string_lossy()
                    );
                    let words = word_count(&path);
                    conn.execute(
                        "INSERT OR IGNORE INTO memory_tiers (key, tier, notes) VALUES (?1, 'warm', ?2)",
                        params![key, format!("{} words", words)],
                    )?;
                }
            }
        }
    }

    // Register daily notes as cold
    let daily_dir = std::path::PathBuf::from(&ws).join("memory");
    if let Ok(entries) = std::fs::read_dir(&daily_dir) {
        for entry in entries.flatten() {
            let path = entry.path();
            let name = path
                .file_name()
                .unwrap_or_default()
                .to_string_lossy()
                .to_string();
            // Daily notes match YYYY-MM-DD.md pattern
            if name.len() == 13 && name.ends_with(".md") && name.chars().nth(4) == Some('-') {
                let key = format!("memory/{}", name);
                conn.execute(
                    "INSERT OR IGNORE INTO memory_tiers (key, tier) VALUES (?1, 'cold')",
                    params![key],
                )?;
            }
        }
    }

    // Hot tier (MEMORY.md) budget check
    if hot_words > 2000 {
        println!(
            "\n⚠ MEMORY.md over budget ({} words). Demotion candidates:",
            hot_words
        );
        // Read MEMORY.md and suggest large sections for demotion
        let content = std::fs::read_to_string(&memory_path).unwrap_or_default();
        let sections: Vec<&str> = content.split("\n## ").collect();
        let mut section_sizes: Vec<(usize, &str)> = sections
            .iter()
            .skip(1) // skip header
            .map(|s| {
                let title = s.lines().next().unwrap_or("?");
                let words = s.split_whitespace().count();
                (words, title)
            })
            .collect();
        section_sizes.sort_by(|a, b| b.0.cmp(&a.0));
        for (words, title) in section_sizes.iter().take(3) {
            println!(
                "  - ## {} ({} words) → candidate for warm tier demotion",
                title, words
            );
        }
        println!("\nTo demote: move section to memory/warm/<topic>.md and update hot tier.");
    } else {
        println!("✓ Hot tier within budget. No action needed.");
    }

    // Show tier summary
    let tracked: i64 = conn.query_row("SELECT COUNT(*) FROM memory_tiers", [], |r| r.get(0))?;
    println!("\nTier registry: {} files tracked", tracked);

    Ok(())
}

pub fn track(conn: &Connection, key: &str, tier: &str) -> Result<()> {
    conn.execute(
        "INSERT OR REPLACE INTO memory_tiers (key, tier, last_access, access_count) VALUES (?1, ?2, datetime('now'), COALESCE((SELECT access_count+1 FROM memory_tiers WHERE key=?1), 1))",
        params![key, tier],
    )?;
    Ok(())
}

pub fn access(conn: &Connection, key: &str) -> Result<()> {
    conn.execute(
        "UPDATE memory_tiers SET last_access=datetime('now'), access_count=access_count+1 WHERE key=?1",
        params![key],
    )?;
    Ok(())
}
