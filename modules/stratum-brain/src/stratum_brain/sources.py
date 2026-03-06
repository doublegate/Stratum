"""
sources.py — Read from all five tool data stores.

stratum-brain aggregates:
  - stratum-watch  → ~/.local/share/stratum/watch.db
  - stratum-mind   → ~/.local/share/stratum/mind.db (stash, lessons)
  - stratum-lens           → subprocess call to `stratum-lens query`
"""

import json
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HOME = Path.home()
CONTEXT_WATCH_STATUS = HOME / ".local/share/stratum/context-watch-status.json"
CRON_HEALTH_DB       = HOME / ".local/share/stratum/watch.db"
STASH_DB             = HOME / ".local/share/stratum/mind.db"   # unified (was clawd-stash/stash.db)
BUFFER_DB            = HOME / ".local/share/stratum/watch.db"  # unified
LESSON_DB            = HOME / ".local/share/stratum/mind.db"  # unified (was clawd-lesson/lessons.db)
STASH_BIN            = HOME / ".local/bin/stratum-mind"        # unified (was clawd-stash)
LESSON_BIN           = HOME / ".local/bin/stratum-mind"        # unified (was clawd-lesson)
LENS_BIN             = HOME / ".local/bin/stratum-lens"


# ── Context Watch ──────────────────────────────────────────────────────────

@dataclass
class ContextStatus:
    active: bool = False
    session_id: str = ""
    estimated_tokens: int = 0
    max_tokens: int = 200_000
    pct: float = 0.0
    level: str = "low"
    recommendation: str | None = None
    updated_at: int = 0
    age_secs: int = 0

def get_context_status() -> ContextStatus:
    try:
        data = json.loads(CONTEXT_WATCH_STATUS.read_text())
        now = int(time.time())
        age = now - data.get("updated_at", now)
        return ContextStatus(
            active=data.get("active", False),
            session_id=data.get("session_id", ""),
            estimated_tokens=data.get("estimated_tokens", 0),
            max_tokens=data.get("max_tokens", 200_000),
            pct=data.get("pct", 0.0),
            level=data.get("level", "low"),
            recommendation=data.get("recommendation"),
            updated_at=data.get("updated_at", 0),
            age_secs=age,
        )
    except Exception:
        return ContextStatus()


# ── Cron Health ────────────────────────────────────────────────────────────

@dataclass
class CronRun:
    id: str
    cron_name: str
    status: str
    confidence: float
    signals: list[str]
    duration_secs: float
    ended_at: int
    age_secs: int

def get_cron_health(limit: int = 20) -> list[CronRun]:
    """Read cron health directly from OpenClaw jobs.json (authoritative source)."""
    jobs_path = HOME / ".openclaw/cron/jobs.json"
    if not jobs_path.exists():
        # Fallback: watch.db may be empty on first run
        if not CRON_HEALTH_DB.exists():
            return []
        try:
            con = sqlite3.connect(CRON_HEALTH_DB)
            con.row_factory = sqlite3.Row
            now = int(time.time())
            rows = con.execute(
                "SELECT id, cron_name, status, confidence, signals, duration_secs, ended_at FROM cron_runs ORDER BY ended_at DESC LIMIT ?",
                (limit,)).fetchall()
            con.close()
            result = []
            for r in rows:
                signals = json.loads(r["signals"] or "[]")
                ended_s = r["ended_at"] or 0
                result.append(CronRun(id=r["id"], cron_name=r["cron_name"] or "Unknown",
                    status=r["status"] or "unknown", confidence=r["confidence"] or 0.0,
                    signals=signals, duration_secs=r["duration_secs"] or 0.0,
                    ended_at=ended_s, age_secs=max(0, int(time.time()) - ended_s)))
            return result
        except Exception:
            return []
    try:
        data = json.loads(jobs_path.read_text())
        jobs = data.get("jobs", data) if isinstance(data, dict) else data
        now = int(time.time())
        result = []
        for j in jobs:
            state = j.get("state", {})
            last_ms = state.get("lastRunAtMs")
            if not last_ms:
                continue
            ended_s = last_ms // 1000
            last_status = state.get("lastStatus", "unknown")
            errors = state.get("consecutiveErrors", 0)
            duration_ms = state.get("lastDurationMs", 0)
            # Map openclaw status to our status + confidence
            if last_status == "ok" and errors == 0:
                status, confidence = "success", 0.95
            elif errors > 0:
                status, confidence = "error", 0.8
            else:
                status, confidence = last_status, 0.5
            signals = [f"openclaw:{last_status}"]
            if errors > 0:
                signals.append(f"consecutive_errors:{errors}")
            result.append(CronRun(
                id=j.get("id", ""),
                cron_name=j.get("name", "Unknown"),
                status=status,
                confidence=confidence,
                signals=signals,
                duration_secs=duration_ms / 1000.0,
                ended_at=ended_s,
                age_secs=max(0, now - ended_s),
            ))
        # Sort by most recent, apply limit
        result.sort(key=lambda r: r.ended_at, reverse=True)
        return result[:limit]
    except Exception:
        return []

def get_latest_cron_per_job() -> list[CronRun]:
    """Most recent run per unique recognized cron_name (filters out 'Unknown')."""
    runs = get_cron_health(limit=200)
    seen: dict[str, CronRun] = {}
    for r in runs:
        if r.cron_name == "Unknown":
            continue
        if r.cron_name not in seen:
            seen[r.cron_name] = r
    return list(seen.values())


# ── Stash ──────────────────────────────────────────────────────────────────

@dataclass
class StashItem:
    id: int
    content: str
    category: str
    priority: str
    created_at: int
    age_secs: int
    done: bool

def get_stash_items(include_done: bool = False) -> list[StashItem]:
    """Read stash items from unified mind.db (stratum-mind schema)."""
    if not STASH_DB.exists():
        return []
    try:
        con = sqlite3.connect(STASH_DB)
        con.row_factory = sqlite3.Row
        where = "" if include_done else "WHERE done=0"
        rows = con.execute(f"""
            SELECT id, content, tags, priority, created_at, done
            FROM stash
            {where}
            ORDER BY
                CASE priority
                    WHEN 'urgent' THEN 0
                    WHEN 'high' THEN 1
                    WHEN 'normal' THEN 2
                    WHEN 'low' THEN 3
                    ELSE 4
                END,
                created_at DESC
            LIMIT 50
        """).fetchall()
        con.close()
        result = []
        for r in rows:
            result.append(StashItem(
                id=r["id"],
                content=r["content"],
                category=r["tags"] or "note",
                priority=r["priority"] or "normal",
                created_at=0,
                age_secs=0,
                done=r["done"] == 1,
            ))
        return result
    except Exception:
        return []

def stash_add(content: str, category: str = "note", priority: str = "normal") -> bool:
    """Add a stash item via stratum-mind stash add."""
    try:
        # stratum-mind stash add <content> --priority <p> [--tags <category>]
        cmd = [str(STASH_BIN), "stash", "add", content, "--priority", priority, "--tags", category]
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except Exception:
        return False


# ── Lessons ────────────────────────────────────────────────────────────────

@dataclass
class LessonItem:
    id: int
    content: str
    category: str
    severity: str
    source: str
    created_at: int
    resolved_at: int | None
    age_secs: int

    @property
    def resolved(self) -> bool:
        return self.resolved_at is not None

def get_lesson_items(include_resolved: bool = False, limit: int = 100) -> list[LessonItem]:
    """Read lessons from unified mind.db (stratum-mind schema)."""
    if not LESSON_DB.exists():
        return []
    try:
        con = sqlite3.connect(LESSON_DB)
        con.row_factory = sqlite3.Row
        where = "" if include_resolved else "WHERE resolved=0"
        rows = con.execute(f"""
            SELECT id, content, category, severity, source, created_at, resolved
            FROM lessons
            {where}
            ORDER BY CASE severity
                WHEN 'critical' THEN 0 WHEN 'high' THEN 1
                WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END,
                created_at DESC
            LIMIT ?
        """, (limit,)).fetchall()
        con.close()
        result = []
        for r in rows:
            result.append(LessonItem(
                id=r["id"],
                content=r["content"],
                category=r["category"] or "discovery",
                severity=r["severity"] or "medium",
                source=r["source"] or "",
                created_at=0,
                resolved_at=None if r["resolved"] == 0 else 1,
                age_secs=0,
            ))
        return result
    except Exception:
        return []

def lesson_add(content: str, category: str = "discovery", severity: str = "medium",
               source: str = "") -> bool:
    """Record a lesson via stratum-mind lesson learn."""
    try:
        cmd = [str(LESSON_BIN), "lesson", "learn", content,
               "--category", category, "--severity", severity]
        if source:
            cmd += ["--source", source]
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except Exception:
        return False

def get_lesson_stats() -> dict[str, Any]:
    """Return counts by category and severity for the analyze command."""
    if not LESSON_DB.exists():
        return {}
    try:
        con = sqlite3.connect(LESSON_DB)
        total = con.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
        unresolved = con.execute(
            "SELECT COUNT(*) FROM lessons WHERE resolved=0"
        ).fetchone()[0]
        by_category = dict(con.execute(
            "SELECT category, COUNT(*) FROM lessons WHERE resolved=0 "
            "GROUP BY category"
        ).fetchall())
        by_severity = dict(con.execute(
            "SELECT severity, COUNT(*) FROM lessons WHERE resolved=0 "
            "GROUP BY severity"
        ).fetchall())
        con.close()
        return {
            "total": total,
            "unresolved": unresolved,
            "resolved": total - unresolved,
            "by_category": by_category,
            "by_severity": by_severity,
        }
    except Exception:
        return {}


# ── Buffer ──────────────────────────────────────────────────────────────────

@dataclass
class BufferEntry:
    id: str
    session_key: str
    content_preview: str
    captured_at: int
    acknowledged: bool
    age_secs: int

def get_buffer_summary() -> dict[str, Any]:
    if not BUFFER_DB.exists():
        return {"total": 0, "unacked": 0, "entries": []}
    try:
        con = sqlite3.connect(BUFFER_DB)
        con.row_factory = sqlite3.Row
        now_ms = int(time.time() * 1000)
        total = con.execute("SELECT COUNT(*) FROM results").fetchone()[0]
        unacked = con.execute(
            "SELECT COUNT(*) FROM results WHERE status = 'captured'"
        ).fetchone()[0]
        recent = con.execute("""
            SELECT id, label, content, captured_at, status
            FROM results
            WHERE status = 'captured'
            ORDER BY captured_at DESC
            LIMIT 5
        """).fetchall()
        con.close()
        entries = []
        for r in recent:
            age = max(0, (now_ms - (r["captured_at"] or now_ms)) // 1000)
            preview = (r["content"] or "")[:80].replace("\n", " ")
            entries.append(BufferEntry(
                id=r["id"],
                session_key=r["label"] or "",
                content_preview=preview,
                captured_at=r["captured_at"] or 0,
                acknowledged=r["status"] == "acked",
                age_secs=age,
            ))
        return {"total": total, "unacked": unacked, "entries": entries}
    except Exception:
        return {"total": 0, "unacked": 0, "entries": []}


# ── Lens ───────────────────────────────────────────────────────────────────

@dataclass
class LensResult:
    source: str
    text_preview: str
    score: float

def lens_query(query: str, top_k: int = 5) -> list[LensResult]:
    """Query stratum-lens (--compact mode) and return parsed results."""
    if not LENS_BIN.exists():
        return []
    try:
        import re
        result = subprocess.run(
            [str(LENS_BIN), "query", query, "--top-k", str(top_k), "--compact"],
            capture_output=True, text=True, timeout=15,
        )
        text = result.stdout
        results: list[LensResult] = []
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            line = lines[i]
            # Score pattern: "0.745" with surrounding spaces
            m = re.search(r"(\d\.\d{3})\s{2,}(.+)", line)
            if m:
                score_str = m.group(1)
                rest = m.group(2).strip()
                # rest is "source.md § section  (~line N)" — may wrap to next line
                # Combine with next line if (~line is missing (wrapped)
                combined = rest
                if "(~line" not in combined and i + 1 < len(lines):
                    combined += " " + lines[i+1].strip()
                    i += 1
                score = float(score_str)
                # Extract source (before §/\xa7 or before (~line)
                src = combined.split("\xa7")[0] if "\xa7" in combined else combined.split("\u00a7")[0] if "\u00a7" in combined else combined
                src = src.split("(~line")[0].strip()
                # Strip the section part (after §)
                if " § " in src:
                    src = src.split(" § ")[0].strip()
                # Preview: next indented line
                preview = ""
                if i + 1 < len(lines) and lines[i+1].startswith("    "):
                    preview = lines[i+1].strip()[:120]
                    i += 1
                results.append(LensResult(source=src, text_preview=preview, score=score))
                if len(results) >= top_k:
                    break
            i += 1
        return results
    except Exception:
        return []

# ── Helpers ────────────────────────────────────────────────────────────────

def fmt_age(secs: int) -> str:
    if secs < 60: return f"{secs}s ago"
    if secs < 3600: return f"{secs//60}m ago"
    if secs < 86400: return f"{secs//3600}h ago"
    return f"{secs//86400}d ago"


# ── stratum-mind world + goals integration ───────────────────────────────

WORLD_DB    = HOME / ".local/share/stratum/mind.db"   # unified (was clawd-world/world.db)
GOALS_DB    = HOME / ".local/share/stratum/mind.db"   # unified (was clawd-goals/goals.db)
WORLD_BIN   = HOME / ".local/bin/stratum-mind"         # unified
GOALS_BIN   = HOME / ".local/bin/stratum-mind"         # unified


@dataclass
class WorldSummary:
    """Aggregated counts from the clawd-world knowledge graph."""
    entity_count: int = 0
    relationship_count: int = 0
    belief_count: int = 0
    low_confidence_count: int = 0   # confidence < 0.7
    stale_count: int = 0            # beliefs marked stale by consolidation
    last_consolidated: str = ""     # ISO timestamp of last consolidation run


def get_world_summary() -> WorldSummary:
    """Read summary counts from the unified mind.db world tables."""
    if not WORLD_DB.exists():
        return WorldSummary()
    try:
        conn = sqlite3.connect(WORLD_DB)
        entity_count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
        rel_count    = conn.execute("SELECT COUNT(*) FROM relations").fetchone()[0]
        belief_count = conn.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0]
        low_conf     = conn.execute("SELECT COUNT(*) FROM beliefs WHERE confidence < 0.7").fetchone()[0]
        # v2: stale beliefs and last consolidation run
        try:
            stale = conn.execute("SELECT COUNT(*) FROM beliefs WHERE stale=1").fetchone()[0]
        except Exception:
            stale = 0
        try:
            last_consolidated = conn.execute(
                "SELECT ran_at FROM consolidation_log ORDER BY ran_at DESC LIMIT 1"
            ).fetchone()
            last_consolidated = last_consolidated[0] if last_consolidated else ""
        except Exception:
            last_consolidated = ""
        conn.close()
        return WorldSummary(
            entity_count=entity_count,
            relationship_count=rel_count,
            belief_count=belief_count,
            low_confidence_count=low_conf,
            stale_count=stale,
            last_consolidated=last_consolidated,
        )
    except Exception:
        return WorldSummary()


@dataclass
class WorldSearchResult:
    """A single result from stratum-mind world search (FTS5 + LIKE hybrid)."""
    kind: str       # "belief", "lesson"
    entity: str     # entity name (beliefs) or "" (lessons)
    attribute: str  # attribute (beliefs) or category/severity (lessons)
    value: str      # value (beliefs) or lesson content
    confidence: float = 0.0
    id: int = 0


def world_search(query: str, limit: int = 10) -> list[WorldSearchResult]:
    """
    Query the world knowledge graph via FTS5 + LIKE hybrid search.

    Searches beliefs (entity, attribute, value) and unresolved lessons.
    Falls back to LIKE when FTS5 returns no results.

    Args:
        query:  Keyword or phrase to search.
        limit:  Max results per category.

    Returns:
        List of WorldSearchResult sorted by kind (beliefs first).
    """
    if not WORLD_DB.exists():
        return []
    results: list[WorldSearchResult] = []
    try:
        conn = sqlite3.connect(WORLD_DB)

        # FTS5 on beliefs
        try:
            rows = conn.execute(
                "SELECT b.id, b.entity, b.attribute, b.value, b.confidence "
                "FROM beliefs_fts f JOIN beliefs b ON b.id = f.rowid "
                "WHERE beliefs_fts MATCH ? AND b.stale=0 ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
            for r in rows:
                results.append(WorldSearchResult("belief", r[1], r[2], r[3], r[4], r[0]))
        except Exception:
            pass  # FTS5 table not ready — fall through to LIKE

        # FTS5 on lessons
        try:
            rows = conn.execute(
                "SELECT l.id, l.content, l.category, l.severity "
                "FROM lessons_fts f JOIN lessons l ON l.id = f.rowid "
                "WHERE lessons_fts MATCH ? AND l.resolved=0 ORDER BY rank LIMIT ?",
                (query, limit),
            ).fetchall()
            for r in rows:
                results.append(WorldSearchResult("lesson", "", f"{r[3]}/{r[2]}", r[1][:120], 0.0, r[0]))
        except Exception:
            pass

        # LIKE fallback for beliefs if FTS returned nothing
        if not any(r.kind == "belief" for r in results):
            pat = f"%{query}%"
            rows = conn.execute(
                "SELECT id, entity, attribute, value, confidence FROM beliefs "
                "WHERE (entity LIKE ? OR attribute LIKE ? OR value LIKE ?) AND stale=0 LIMIT ?",
                (pat, pat, pat, limit),
            ).fetchall()
            for r in rows:
                results.append(WorldSearchResult("belief", r[1], r[2], r[3], r[4], r[0]))

        conn.close()
    except Exception:
        pass
    # beliefs first, then lessons
    results.sort(key=lambda r: (0 if r.kind == "belief" else 1))
    return results


def world_traverse(entity: str, hops: int = 2) -> dict:
    """
    BFS graph traversal from an entity in the knowledge graph.

    Loads all relations into memory and walks BFS up to `hops` depth.

    Returns:
        dict with keys:
          - "edges": list of (subject, predicate, object, hop) tuples
          - "beliefs": list of (entity, attribute, value, confidence)
          - "nodes": set of reachable node names
    """
    if not WORLD_DB.exists():
        return {"edges": [], "beliefs": [], "nodes": []}
    try:
        conn = sqlite3.connect(WORLD_DB)
        all_rels = conn.execute("SELECT subject, predicate, object FROM relations").fetchall()

        visited: set[str] = set()
        queue: list[tuple[str, int]] = [(entity, 0)]
        visited.add(entity.lower())
        edges: list[tuple[str, str, str, int]] = []
        edge_keys: set[str] = set()

        while queue:
            node, depth = queue.pop(0)
            if depth >= hops:
                continue
            node_lower = node.lower()
            for subj, pred, obj in all_rels:
                if node_lower not in subj.lower() and node_lower not in obj.lower():
                    continue
                neighbor = obj if node_lower in subj.lower() else subj
                key = f"{subj}\x00{pred}\x00{obj}"
                if key not in edge_keys:
                    edge_keys.add(key)
                    edges.append((subj, pred, obj, depth + 1))
                nb_lower = neighbor.lower()
                if nb_lower not in visited:
                    visited.add(nb_lower)
                    queue.append((neighbor, depth + 1))

        # Collect beliefs for all visited nodes
        beliefs = []
        for node in visited:
            pat = f"%{node}%"
            rows = conn.execute(
                "SELECT entity, attribute, value, confidence FROM beliefs "
                "WHERE lower(entity) LIKE ? AND stale=0 LIMIT 5",
                (pat,),
            ).fetchall()
            beliefs.extend(rows)

        conn.close()
        return {
            "edges": edges,
            "beliefs": beliefs,
            "nodes": sorted(visited),
        }
    except Exception:
        return {"edges": [], "beliefs": [], "nodes": []}


@dataclass
class GoalItem:
    """A single goal row from stratum-mind goals table."""
    id: int
    title: str
    goal_type: str
    parent_id: int | None
    status: str
    priority: str
    success_criteria: str


def get_active_goals() -> list[GoalItem]:
    """Return active goals from unified mind.db, ordered by priority."""
    if not GOALS_DB.exists():
        return []
    PRIORITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "normal": 3, "low": 4}
    try:
        conn = sqlite3.connect(GOALS_DB)
        rows = conn.execute("SELECT id, title, parent_id, status, priority, description FROM goals WHERE status='active'").fetchall()
        conn.close()
        goals = [GoalItem(
            id=r[0], title=r[1], goal_type="goal", parent_id=r[2],
            status=r[3], priority=r[4], success_criteria=r[5] or ""
        ) for r in rows]
        goals.sort(key=lambda g: (PRIORITY_ORDER.get(g.priority, 99), g.title))
        return goals
    except Exception:
        return []


def get_goal_stats() -> dict[str, Any]:
    """
    Return goal counts by status and priority.

    Returns zeros if the DB does not yet exist.
    """
    if not GOALS_DB.exists():
        return {"active": 0, "completed": 0, "deferred": 0,
                "critical_active": 0, "high_active": 0, "stale_active": 0}
    try:
        conn = sqlite3.connect(GOALS_DB)
        conn.row_factory = sqlite3.Row
        now = int(time.time())
        active   = conn.execute("SELECT COUNT(*) FROM goals WHERE status='active'").fetchone()[0]
        completed= conn.execute("SELECT COUNT(*) FROM goals WHERE status='completed'").fetchone()[0]
        deferred = conn.execute("SELECT COUNT(*) FROM goals WHERE status='deferred'").fetchone()[0]
        critical = conn.execute(
            "SELECT COUNT(*) FROM goals WHERE status='active' AND priority='critical'"
        ).fetchone()[0]
        high = conn.execute(
            "SELECT COUNT(*) FROM goals WHERE status='active' AND priority='high'"
        ).fetchone()[0]
        stale = conn.execute(
            "SELECT COUNT(*) FROM goals WHERE status='active'"
            " AND (last_evaluated_at IS NULL OR last_evaluated_at < ?)",
            (now - 7 * 86400,),
        ).fetchone()[0]
        conn.close()
        return {
            "active": active, "completed": completed, "deferred": deferred,
            "critical_active": critical, "high_active": high, "stale_active": stale,
        }
    except Exception:
        return {"active": 0, "completed": 0, "deferred": 0,
                "critical_active": 0, "high_active": 0, "stale_active": 0}
