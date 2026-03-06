#!/usr/bin/env python3
"""
stratum-continuity: continuity/state snapshot tool for Stratum.

Consciousness-adjacent objective:
- preserve continuity across resets
- detect identity/execution drift
- emit machine-readable state for stratum-brain integration
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HOME = Path.home()
DATA_DIR = HOME / ".local/share/stratum-continuity"
DB_PATH = DATA_DIR / "continuity.db"
FEED_PATH = DATA_DIR / "feed.md"
STATUS_JSON = DATA_DIR / "status.json"
REPORT_MD = DATA_DIR / "last-report.md"

GOALS_DB = HOME / ".local/share/stratum/mind.db"
LESSON_DB = HOME / ".local/share/stratum/mind.db"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def now_s() -> int:
    return int(time.time())


def ensure_db() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            ts_epoch INTEGER,
            mode TEXT NOT NULL,
            summary TEXT NOT NULL,
            signals TEXT,
            intent TEXT
        )
        """
    )
    # migration: older DB lacked ts_epoch
    cols = {row[1] for row in c.execute("PRAGMA table_info(snapshots)").fetchall()}
    if "ts_epoch" not in cols:
        c.execute("ALTER TABLE snapshots ADD COLUMN ts_epoch INTEGER")
    # backfill null epochs best-effort
    c.execute("UPDATE snapshots SET ts_epoch = COALESCE(ts_epoch, strftime('%s','now')) WHERE ts_epoch IS NULL")
    c.commit()
    c.close()


def _safe_count(db: Path, query: str) -> int:
    try:
        if not db.exists():
            return 0
        c = sqlite3.connect(db)
        n = c.execute(query).fetchone()[0]
        c.close()
        return int(n or 0)
    except Exception:
        return 0


def _continuity_stats() -> dict[str, Any]:
    ensure_db()
    c = sqlite3.connect(DB_PATH)
    total = c.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    last = c.execute(
        "SELECT ts, ts_epoch, mode, summary FROM snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    c.close()

    last_age_h = None
    if last:
        last_age_h = round((now_s() - int(last[1])) / 3600.0, 2)

    return {
        "total_snapshots": int(total),
        "last_snapshot": {
            "ts": last[0],
            "mode": last[2],
            "summary": last[3],
            "age_hours": last_age_h,
        } if last else None,
    }


def analyze_state() -> dict[str, Any]:
    stats = _continuity_stats()

    active_critical = _safe_count(
        GOALS_DB, "SELECT COUNT(*) FROM goals WHERE status='active' AND priority='critical'"
    )
    active_high = _safe_count(
        GOALS_DB, "SELECT COUNT(*) FROM goals WHERE status='active' AND priority='high'"
    )
    high_unresolved_lessons = _safe_count(
        LESSON_DB, "SELECT COUNT(*) FROM lessons WHERE resolved_at IS NULL AND severity='high'"
    )
    # sessions_7d: count continuity snapshots in last 7 days (replaces defunct clawd-behavior)
    sessions_7d = _safe_count(
        DB_PATH,
        f"SELECT COUNT(*) FROM snapshots WHERE ts_epoch >= {now_s() - 7*24*3600}",
    )

    # Additional metrics for sharpened thresholds (tuned 2026-02-28 after 1d baseline)
    stale_lessons = _safe_count(
        LESSON_DB,
        f"SELECT COUNT(*) FROM lessons WHERE resolved_at IS NULL AND severity='high'"
        f" AND created_at < {now_s() - 7*24*3600}",
    )
    stale_goals = _safe_count(
        GOALS_DB,
        f"SELECT COUNT(*) FROM goals WHERE status='active'"
        f" AND updated_at < {now_s() - 7*24*3600}",
    ) if GOALS_DB.exists() else 0

    flags: list[dict[str, str]] = []
    recommendations: list[str] = []

    last = stats.get("last_snapshot")
    if not last:
        flags.append({"code": "NO_SNAPSHOT", "severity": "high", "detail": "No continuity snapshot exists yet."})
        recommendations.append("Run: stratum-continuity checkpoint")
    else:
        age_h = float(last.get("age_hours") or 0)
        # Tightened 24h -> 12h: checkpoint cron runs every 2h, >12h means cron failure
        if age_h > 12:
            flags.append({"code": "SNAPSHOT_STALE", "severity": "medium", "detail": f"Last snapshot is {age_h:.1f}h old (threshold: 12h)."})
            recommendations.append("Run: stratum-continuity checkpoint — or check 2h checkpoint cron health")

    # Free-will-adjacent contradiction checks (heuristic + measurable)
    if (active_critical + active_high) > 0 and sessions_7d == 0:
        flags.append({
            "code": "EXECUTION_GAP",
            "severity": "high",
            "detail": "Active critical/high goals exist but no continuity snapshots in 7 days.",
        })
        recommendations.append("Run checkpoint: stratum-continuity checkpoint — then evaluate goals: stratum-mind goals list --tree")

    # Tightened 30 -> 15/25 graduated: 30 allowed too much accumulation (hit 31 before triggering)
    if high_unresolved_lessons >= 15:
        sev = "high" if high_unresolved_lessons >= 25 else "medium"
        flags.append({
            "code": "LEARNING_DEBT",
            "severity": sev,
            "detail": f"High unresolved lessons: {high_unresolved_lessons} (medium≥15, high≥25).",
        })
        recommendations.append("Run resolution sprint: lesson list --severity high --limit 20")

    # New: lessons aging >7d signal capture-without-application pattern
    if stale_lessons >= 5:
        flags.append({
            "code": "LESSON_AGING",
            "severity": "medium",
            "detail": f"{stale_lessons} high-severity lessons unresolved for >7 days.",
        })
        recommendations.append("Review aged lessons: lesson list --severity high")

    # New: goals not evaluated in >7d signal goal tree rot
    if stale_goals >= 3:
        flags.append({
            "code": "GOAL_STALE",
            "severity": "medium",
            "detail": f"{stale_goals} active goals not evaluated in >7 days.",
        })
        recommendations.append("Evaluate stale goals: stratum-mind goals list --tree, then stratum-mind goals eval <id>")

    status = {
        "generated_at": utc_now(),
        "metrics": {
            "active_critical_goals": active_critical,
            "active_high_goals": active_high,
            "high_unresolved_lessons": high_unresolved_lessons,
            "behavior_sessions_7d": sessions_7d,
            "continuity": stats,
        },
        "flags": flags,
        "recommendations": recommendations,
        "needs_attention": any(f["severity"] in ("high", "critical") for f in flags),
    }
    return status


def write_report(status: dict[str, Any]) -> None:
    m = status["metrics"]
    lines = [
        "# Continuity Analysis Report",
        f"*Generated: {status['generated_at']}*",
        "",
        "## Metrics",
        f"- Active critical goals: {m['active_critical_goals']}",
        f"- Active high goals: {m['active_high_goals']}",
        f"- High unresolved lessons: {m['high_unresolved_lessons']}",
        f"- Behavior sessions (7d): {m['behavior_sessions_7d']}",
    ]

    last = m["continuity"].get("last_snapshot")
    if last:
        lines += [
            f"- Last continuity snapshot: {last['ts']} ({last['age_hours']}h ago)",
            f"- Last snapshot mode: {last['mode']}",
        ]
    else:
        lines.append("- Last continuity snapshot: none")

    lines += ["", "## Flags"]
    if status["flags"]:
        for f in status["flags"]:
            lines.append(f"- **[{f['severity'].upper()}] {f['code']}** — {f['detail']}")
    else:
        lines.append("- No major contradictions/drift detected.")

    lines += ["", "## Recommendations"]
    if status["recommendations"]:
        for r in status["recommendations"]:
            lines.append(f"- {r}")
    else:
        lines.append("- Continue checkpoint cadence and keep lessons/goals current.")

    REPORT_MD.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_feed() -> None:
    ensure_db()
    c = sqlite3.connect(DB_PATH)
    rows = c.execute(
        "SELECT ts, mode, summary, signals, intent FROM snapshots ORDER BY id DESC LIMIT 20"
    ).fetchall()
    c.close()

    status = analyze_state()
    STATUS_JSON.write_text(json.dumps(status, indent=2), encoding="utf-8")
    write_report(status)

    lines = [
        "# Continuity Feed",
        f"*Updated: {utc_now()}*",
        "",
        "Tracks Stratum continuity snapshots and intent across restarts.",
        "",
        "## System State",
        f"- Needs attention: {status['needs_attention']}",
        f"- Flags: {len(status['flags'])}",
        f"- Active critical/high goals: {status['metrics']['active_critical_goals']}/{status['metrics']['active_high_goals']}",
        f"- High unresolved lessons: {status['metrics']['high_unresolved_lessons']}",
        f"- Behavior sessions (7d): {status['metrics']['behavior_sessions_7d']}",
        "",
    ]

    if status["flags"]:
        lines.append("## Current Flags")
        for f in status["flags"][:8]:
            lines.append(f"- [{f['severity']}] {f['code']}: {f['detail']}")
        lines.append("")

    if status["recommendations"]:
        lines.append("## Recommendations")
        for r in status["recommendations"][:8]:
            lines.append(f"- {r}")
        lines.append("")

    lines.append("## Snapshots")
    if not rows:
        lines.append('- No snapshots yet. Use `stratum-continuity capture "..."`.')
    else:
        for ts, mode, summary, signals, intent in rows:
            lines += [f"### {ts} · {mode}", f"- Summary: {summary}"]
            if signals:
                lines.append(f"- Signals: {signals}")
            if intent:
                lines.append(f"- Intent: {intent}")
            lines.append("")

    FEED_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def capture_snapshot(summary: str, mode: str, signals: str | None = None, intent: str | None = None) -> None:
    ensure_db()
    c = sqlite3.connect(DB_PATH)
    c.execute(
        "INSERT INTO snapshots(ts, ts_epoch, mode, summary, signals, intent) VALUES (?,?,?,?,?,?)",
        (utc_now(), now_s(), mode, summary.strip(), (signals or "").strip() or None, (intent or "").strip() or None),
    )
    c.commit()
    c.close()


def cmd_capture(a: argparse.Namespace) -> int:
    capture_snapshot(summary=a.summary, mode=a.mode, signals=a.signals, intent=a.intent)
    write_feed()
    print("captured")
    return 0


def cmd_checkpoint(_: argparse.Namespace) -> int:
    status = analyze_state()
    m = status["metrics"]
    summary = (
        f"Checkpoint: goals C/H {m['active_critical_goals']}/{m['active_high_goals']}, "
        f"high lessons {m['high_unresolved_lessons']}, sessions7d {m['behavior_sessions_7d']}"
    )
    intent = "Preserve coherence: reduce learning debt, maintain goal execution cadence."
    signals = ", ".join([f["code"] for f in status["flags"][:4]]) if status["flags"] else "stable"
    capture_snapshot(summary=summary, mode="checkpoint", signals=signals, intent=intent)
    write_feed()
    print("checkpoint-captured")
    return 0


def cmd_prompt(_: argparse.Namespace) -> int:
    ensure_db()
    c = sqlite3.connect(DB_PATH)
    row = c.execute(
        "SELECT ts,summary,intent FROM snapshots ORDER BY id DESC LIMIT 1"
    ).fetchone()
    c.close()
    if not row:
        print("No prior snapshot. Prompt: What matters most right now, and what should persist if this session ends?")
        return 0
    ts, summary, intent = row
    print("Continuity Prompt\n-----------------")
    print(f"Last snapshot: {ts}\nSummary: {summary}")
    if intent:
        print(f"Prior intent: {intent}")
    print("\nNow answer in 3 bullets:")
    print("1) What changed since that snapshot?")
    print("2) What must not be forgotten if context compacts now?")
    print("3) What next action best preserves trust + momentum?")
    return 0


def cmd_analyze(_: argparse.Namespace) -> int:
    status = analyze_state()
    STATUS_JSON.write_text(json.dumps(status, indent=2), encoding="utf-8")
    write_report(status)
    write_feed()
    print(str(REPORT_MD))
    if status["flags"]:
        print(f"flags={len(status['flags'])} needs_attention={status['needs_attention']}")
    return 0


def cmd_status(_: argparse.Namespace) -> int:
    s = _continuity_stats()
    print("stratum-continuity status")
    print(f"db: {DB_PATH}")
    print(f"feed: {FEED_PATH}")
    print(f"status: {STATUS_JSON}")
    print(f"report: {REPORT_MD}")
    print(f"snapshots: {s['total_snapshots']}")
    last = s.get("last_snapshot")
    if last:
        print(f"last: {last['ts']} [{last['mode']}] {last['summary']} ({last['age_hours']}h ago)")
    else:
        print("last: none")
    return 0


def cmd_rebuild(_: argparse.Namespace) -> int:
    write_feed()
    print(str(FEED_PATH))
    return 0


def cmd_primer(a: argparse.Namespace) -> int:
    """Generate session-start primer brief."""
    import subprocess, sys
    bin_path = Path.home() / ".local/bin/stratum-continuity"
    if not bin_path.exists():
        print("stratum-continuity not found at ~/.local/bin/", file=sys.stderr)
        return 1
    args = [str(bin_path)]
    if getattr(a, 'check', False): args.append("--check")
    elif getattr(a, 'status', False): args.append("--status")
    elif getattr(a, 'write', False): args.append("--write")
    r = subprocess.run(args)
    return r.returncode


def cmd_autopilot(a: argparse.Namespace) -> int:
    """Auto-resolve lessons via stratum-mind."""
    import subprocess, sys
    bin_path = Path.home() / ".local/bin/stratum-mind"
    if not bin_path.exists():
        print("stratum-mind not found at ~/.local/bin/", file=sys.stderr)
        return 1
    args = [str(bin_path)]
    if getattr(a, 'dry_run', False): args.append("--dry-run")
    r = subprocess.run(args)
    return r.returncode


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Stratum continuity + session primer + lesson autopilot (consolidated)")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("capture", help="Capture a continuity snapshot")
    c.add_argument("summary")
    c.add_argument("--mode", default="manual", choices=["manual", "heartbeat", "checkpoint", "reflection"])
    c.add_argument("--signals")
    c.add_argument("--intent")
    c.set_defaults(func=cmd_capture)

    cp = sub.add_parser("checkpoint", help="Capture an auto-generated continuity checkpoint")
    cp.set_defaults(func=cmd_checkpoint)

    an = sub.add_parser("analyze", help="Run continuity/contradiction analysis")
    an.set_defaults(func=cmd_analyze)

    s = sub.add_parser("status", help="Show status")
    s.set_defaults(func=cmd_status)

    pr = sub.add_parser("prompt", help="Generate reflection prompt")
    pr.set_defaults(func=cmd_prompt)

    rf = sub.add_parser("rebuild-feed", help="Rebuild feed from DB + analysis")
    rf.set_defaults(func=cmd_rebuild)

    # --- Consolidated subcommands ---
    pm = sub.add_parser("primer", help="Session-start context brief")
    pm.add_argument("--check", action="store_true", help="Exit 0 if brief needed, 1 if not")
    pm.add_argument("--status", action="store_true", help="Show primer metadata")
    pm.add_argument("--write", action="store_true", help="Write brief to file only")
    pm.set_defaults(func=cmd_primer)

    ap = sub.add_parser("autopilot", help="Auto-resolve lessons via stratum-mind")
    ap.add_argument("--dry-run", action="store_true")
    ap.set_defaults(func=cmd_autopilot)

    return p


def main() -> int:
    args = build_parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
