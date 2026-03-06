"""
integrations.py — Cross-tool automation. This is the "greater than sum of parts" module.

Behaviors:
  1. Auto-stash on cron failure: cron-health detects failure → stash item auto-created
  2. Pre-compaction checkpoint: context-watch hits "high"+ → dump stash + trigger lens index
  3. Health-to-lens feed: cron failure summaries written to a scratch file that lens can index
  4. Unified proactive recommendations returned to heartbeat
"""

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .sources import (
    get_context_status, get_latest_cron_per_job, get_stash_items,
    get_buffer_summary, stash_add, fmt_age, LENS_BIN,
    get_lesson_items, get_lesson_stats,
)

HOME = Path.home()
BRAIN_STATE_DIR   = HOME / ".local/share/stratum"
STATE_FILE        = BRAIN_STATE_DIR / "state.json"
CRON_FEED_FILE    = BRAIN_STATE_DIR / "cron-health-feed.md"   # indexed by stratum-lens
LESSON_FEED_FILE  = BRAIN_STATE_DIR / "lesson-feed.md"        # indexed by stratum-lens
REFLECTION_FEED   = BRAIN_STATE_DIR / "reflection-feed.md"    # indexed by stratum-lens
CHECKPOINT_FILE   = BRAIN_STATE_DIR / "last-checkpoint.json"

LENS_SERVICE_FILE = HOME / ".config/systemd/user/stratum-lens.service"
LENS_SERVICE_NAME = "stratum-lens.service"

# Memory thresholds for auto-scaling
LENS_MEM_WARN_PCT  = 0.75   # warn above 75% of MemoryMax
LENS_MEM_SCALE_PCT = 0.85   # auto-scale above 85% of MemoryMax
LENS_MEM_SCALE_BY  = 512    # add 512MB each time we scale


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}

def _save_state(state: dict) -> None:
    BRAIN_STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ── 0. Lens memory monitoring + auto-scaling ──────────────────────────────

@dataclass
class LensMemoryStatus:
    current_bytes: int
    max_bytes: int
    pct: float
    scaled: bool          # True if we just auto-raised the limit
    new_max_mb: int       # non-zero if scaled
    alert: str            # non-empty if warning/action taken

def check_lens_memory() -> LensMemoryStatus:
    """
    Read stratum-lens.service memory usage via systemd, auto-scale MemoryMax if needed.

    Auto-scaling rules:
      > 85% of MemoryMax → raise limit by 512MB, daemon-reload, restart service
      > 75% of MemoryMax → return a warning (no action yet)
      ≤ 75% → all clear

    The MemoryMax line in the service file is updated in-place so the new limit
    survives future restarts without manual intervention.
    """
    try:
        r = subprocess.run(
            ["systemctl", "--user", "show", LENS_SERVICE_NAME,
             "--property=MemoryCurrent,MemoryMax"],
            capture_output=True, text=True, timeout=5
        )
        current_bytes = 0
        max_bytes = 0
        for line in r.stdout.strip().splitlines():
            if line.startswith("MemoryCurrent="):
                try:
                    current_bytes = int(line.split("=", 1)[1])
                except ValueError:
                    pass
            elif line.startswith("MemoryMax="):
                val = line.split("=", 1)[1]
                if val == "infinity":
                    max_bytes = 0  # uncapped
                else:
                    try:
                        max_bytes = int(val)
                    except ValueError:
                        pass
    except Exception:
        return LensMemoryStatus(0, 0, 0.0, False, 0, "")

    if current_bytes == 0 or max_bytes == 0:
        return LensMemoryStatus(current_bytes, max_bytes, 0.0, False, 0, "")

    pct = current_bytes / max_bytes

    if pct >= LENS_MEM_SCALE_PCT:
        # Auto-scale: raise MemoryMax by 512MB
        current_max_mb = max_bytes // (1024 * 1024)
        new_max_mb = current_max_mb + LENS_MEM_SCALE_BY
        _set_lens_memory_max(new_max_mb)
        alert = (
            f"stratum-lens memory at {pct:.0%} of {current_max_mb}MB limit — "
            f"auto-scaled to {new_max_mb}MB and restarted service."
        )
        return LensMemoryStatus(current_bytes, max_bytes, pct, True, new_max_mb, alert)

    elif pct >= LENS_MEM_WARN_PCT:
        current_max_mb = max_bytes // (1024 * 1024)
        alert = (
            f"stratum-lens memory at {pct:.0%} of {current_max_mb}MB — "
            f"approaching limit. Will auto-scale at 85%."
        )
        return LensMemoryStatus(current_bytes, max_bytes, pct, False, 0, alert)

    return LensMemoryStatus(current_bytes, max_bytes, pct, False, 0, "")


def _set_lens_memory_max(new_max_mb: int) -> bool:
    """Edit the service file's MemoryMax line and reload + restart the service."""
    try:
        if not LENS_SERVICE_FILE.exists():
            return False

        content = LENS_SERVICE_FILE.read_text()
        import re
        new_content = re.sub(
            r"^MemoryMax=.*$",
            f"MemoryMax={new_max_mb}M",
            content,
            flags=re.MULTILINE,
        )
        LENS_SERVICE_FILE.write_text(new_content)

        # Daemon reload + restart
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, timeout=10)
        subprocess.run(["systemctl", "--user", "restart", LENS_SERVICE_NAME],
                       capture_output=True, timeout=30)
        return True
    except Exception:
        return False


# ── 1. Auto-stash on cron failure ─────────────────────────────────────────

def auto_stash_failures() -> list[str]:
    """Check cron health; auto-create stash items for new failures. Returns list of stash messages."""
    state = _load_state()
    already_stashed: set[str] = set(state.get("stashed_failures", []))
    new_stashed = []

    jobs = get_latest_cron_per_job()
    for job in jobs:
        if job.status == "failure" and job.id not in already_stashed:
            msg = f"CRON FAILURE: {job.cron_name} (confidence={job.confidence:.2f}) — {', '.join(job.signals[:3]) or 'no signals'}"
            if stash_add(msg, category="dev", priority="high"):
                already_stashed.add(job.id)
                new_stashed.append(msg)

    if new_stashed:
        state["stashed_failures"] = list(already_stashed)
        _save_state(state)

    return new_stashed


# ── 2. Pre-compaction checkpoint ───────────────────────────────────────────

@dataclass
class CheckpointResult:
    triggered: bool
    level: str
    pct: float
    stash_dumped: int
    lens_reindexed: bool
    message: str

def maybe_checkpoint() -> CheckpointResult:
    """If context is HIGH+, dump stash state and trigger lens re-index."""
    ctx = get_context_status()
    if not ctx.active or ctx.level not in ("high", "critical", "urgent"):
        return CheckpointResult(False, ctx.level, ctx.pct, 0, False,
                                "Context level OK — no checkpoint needed.")

    # Check if we already checkpointed this session recently (< 15 min ago)
    state = _load_state()
    last_cp = state.get("last_checkpoint", {})
    if (last_cp.get("session_id") == ctx.session_id and
            time.time() - last_cp.get("ts", 0) < 900):
        return CheckpointResult(False, ctx.level, ctx.pct, 0, False,
                                "Checkpoint already run recently for this session.")

    # Dump pending stash to checkpoint file
    stash_items = get_stash_items(include_done=False)
    checkpoint_data = {
        "session_id": ctx.session_id,
        "context_pct": ctx.pct,
        "context_level": ctx.level,
        "ts": int(time.time()),
        "pending_stash": [
            {"id": i.id, "content": i.content, "priority": i.priority}
            for i in stash_items
        ],
    }
    BRAIN_STATE_DIR.mkdir(parents=True, exist_ok=True)
    CHECKPOINT_FILE.write_text(json.dumps(checkpoint_data, indent=2))

    # Trigger lens re-index — use signal file to avoid concurrent-write corruption.
    # If the service is running, it will pick up the signal on its next poll.
    # If not running, call the CLI directly (it will get the lock uncontested).
    lens_ok = False
    if LENS_BIN.exists():
        try:
            from pathlib import Path as _P
            _signal = _P.home() / ".local/share/stratum-lens/reindex.signal"
            _signal.parent.mkdir(parents=True, exist_ok=True)
            _signal.write_text(str(time.time()))
            lens_ok = True
        except Exception:
            pass

    # Record checkpoint
    state["last_checkpoint"] = {"session_id": ctx.session_id, "ts": int(time.time())}
    _save_state(state)

    msg = (f"⚠️ Context at {ctx.pct:.0f}% ({ctx.level}). "
           f"Checkpointed {len(stash_items)} stash items. "
           f"Lens re-indexed: {'yes' if lens_ok else 'no'}.")
    return CheckpointResult(True, ctx.level, ctx.pct, len(stash_items), lens_ok, msg)


# ── 3. Health-to-lens feed ──────────────────────────────────────────────────

def update_cron_health_feed() -> None:
    """Write cron health summary to a markdown file that stratum-lens indexes."""
    jobs = get_latest_cron_per_job()
    if not jobs:
        return

    BRAIN_STATE_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Cron Health Feed",
        f"*Updated: {time.strftime('%Y-%m-%d %H:%M ET')}*",
        "",
        "## Recent Cron Job Outcomes",
        "",
    ]
    for job in jobs:
        icon = "✅" if job.status == "success" else ("❌" if job.status == "failure" else "⚠️")
        signals_str = ", ".join(job.signals[:4]) if job.signals else "none"
        lines.append(f"- **{job.cron_name}**: {icon} {job.status} — {fmt_age(job.age_secs)} — signals: {signals_str}")

    CRON_FEED_FILE.write_text("\n".join(lines))


# ── 3b. Lesson-to-lens feed ────────────────────────────────────────────────

def update_lesson_feed() -> None:
    """Write unresolved lessons to a markdown file that stratum-lens indexes.

    This makes all captured lessons semantically searchable via `stratum-lens query`
    and `stratum-brain query`. Updated on every heartbeat so the index stays current.
    """
    lessons = get_lesson_items(include_resolved=True, limit=200)
    if not lessons:
        return

    BRAIN_STATE_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Lesson Feed",
        f"*Updated: {time.strftime('%Y-%m-%d %H:%M ET')}*",
        "",
        "Stratum's accumulated lessons — tool failures, API changes, corrections, discoveries.",
        "",
        "## Unresolved Lessons",
        "",
    ]
    unresolved = [l for l in lessons if not l.resolved]
    if unresolved:
        for l in unresolved:
            src = f" *({l.source})*" if l.source else ""
            lines.append(
                f"- **[{l.id}]** `{l.severity}/{l.category}` {l.content}{src}"
                f" — *{fmt_age(l.age_secs)}*"
            )
    else:
        lines.append("*(no unresolved lessons)*")

    lines += ["", "## Resolved Lessons", ""]
    resolved = [l for l in lessons if l.resolved]
    if resolved:
        for l in resolved:
            src = f" *({l.source})*" if l.source else ""
            lines.append(
                f"- ~~[{l.id}]~~ `{l.severity}/{l.category}` {l.content}{src}"
                f" — *resolved*"
            )
    else:
        lines.append("*(none yet)*")

    LESSON_FEED_FILE.write_text("\n".join(lines))


# ── 4. Unified heartbeat recommendations ──────────────────────────────────

@dataclass
class HeartbeatResult:
    alerts: list[str]
    recommendations: list[str]
    auto_actions: list[str]
    context_pct: float
    context_level: str
    cron_failures: list[str]
    stash_pending: int
    buffer_unacked: int
    lesson_unresolved: int
    needs_attention: bool
    # stratum-mind world + goals fields
    critical_goals: int = 0
    stale_goals: int = 0   # active goals not evaluated in >7 days
    world_entities: int = 0

def run_heartbeat_integrations() -> HeartbeatResult:
    """The master integration call — runs all cross-tool checks."""
    alerts = []
    recommendations = []
    auto_actions = []

    # Context watch
    ctx = get_context_status()
    if ctx.active and ctx.level in ("critical", "urgent"):
        alerts.append(f"Context at {ctx.pct:.0f}% ({ctx.level}) — {ctx.recommendation}")
        cp = maybe_checkpoint()
        if cp.triggered:
            auto_actions.append(cp.message)

    elif ctx.active and ctx.level == "high":
        recommendations.append(f"Context at {ctx.pct:.0f}% — consider wrapping up or running memory maintenance.")
        maybe_checkpoint()  # quiet checkpoint

    # Cron health — auto-stash failures
    new_failures = auto_stash_failures()
    cron_failures = []
    for job in get_latest_cron_per_job():
        if job.status == "failure":
            cron_failures.append(f"{job.cron_name} ({fmt_age(job.age_secs)})")
    if cron_failures:
        alerts.append(f"Cron failures detected: {', '.join(cron_failures)}")
    if new_failures:
        auto_actions.append(f"Auto-stashed {len(new_failures)} new cron failure(s)")

    # Update cron feed and lesson feed for lens indexing
    update_cron_health_feed()
    update_lesson_feed()
    # Regenerate world + goals feeds for lens indexing (added 2026-02-25)
    update_world_feed()
    update_goals_feed()
    # Refresh new tool feeds for lens indexing (added 2026-03-03)
    _refresh_version_watch_feed()
    _refresh_report_timeline_feed()
    _run_cron_cleanup()
    _refresh_docker_watcher_feed()
    _refresh_pipeline_timer_feed()  # added 2026-03-03
    _refresh_memory_projector_feed()  # added 2026-03-03
    _refresh_task_primer_feed()       # added 2026-03-03

    # Stash
    stash = get_stash_items()
    urgent = [i for i in stash if i.priority in ("urgent", "high")]
    if urgent:
        recommendations.append(f"{len(urgent)} high-priority stash item(s) pending: {urgent[0].content[:60]}...")
    stash_pending = len(stash)

    # Buffer
    buf = get_buffer_summary()
    buffer_unacked = buf.get("unacked", 0)
    if buffer_unacked > 3:
        recommendations.append(f"{buffer_unacked} unacknowledged buffer entries — run 'stratum-watch buffer status' to review.")

    # Lessons — surface critical/high unresolved lessons
    lessons = get_lesson_items(include_resolved=False)
    lesson_unresolved = len(lessons)
    critical_lessons = [l for l in lessons if l.severity in ("critical", "high")]
    if critical_lessons:
        first = critical_lessons[0]
        src_note = f" (from {first.source})" if first.source else ""
        recommendations.append(
            f"{len(critical_lessons)} unresolved high-severity lesson(s){src_note}: "
            f"{first.content[:60]}..."
        )

    # Lens memory check — auto-scale before it causes a crash
    lens_mem = check_lens_memory()
    if lens_mem.scaled:
        auto_actions.append(lens_mem.alert)
    elif lens_mem.alert:
        recommendations.append(lens_mem.alert)

    # Lesson domain clusters → research suggestions (cross-loop: operational → growth)
    # If 5+ unresolved lessons cluster around a domain, suggest queuing research
    try:
        all_lessons = get_lesson_items(include_resolved=False)
        domain_counts: dict[str, int] = {}
        # Map domain keywords to canonical names
        # Use word-boundary-style matching: check for the keyword as a standalone word
        # to avoid "HA" matching "sha256", "that", "what", etc.
        DOMAIN_KEYWORDS: dict[str, str] = {
            "home assistant": "Home Assistant",
            "home-assistant": "Home Assistant",
            "hass-cli": "Home Assistant",
            "gog gmail": "gog/Gmail",
            "gog calendar": "gog/Gmail",
            " gog ": "gog/Gmail",
            "ffmpeg": "ffmpeg",
            "synology": "Synology",
            "veridianos": "VeridianOS",
            "bevy": "Bevy",
            "stratum-lens": "stratum-lens",
            "chromadb": "ChromaDB",
            "openai": "OpenAI",
            "remotion": "Remotion",
            "tailscale": "Tailscale",
            "cloudflare": "Cloudflare",
        }
        for l in all_lessons:
            text = " " + (l.content + " " + l.source).lower() + " "
            for keyword, canonical in DOMAIN_KEYWORDS.items():
                if keyword in text:
                    domain_counts[canonical] = domain_counts.get(canonical, 0) + 1
                    break
        hot_domains = [(k, v) for k, v in domain_counts.items() if v >= 5]
        if hot_domains:
            top = sorted(hot_domains, key=lambda x: -x[1])[:2]
            for domain, count in top:
                recommendations.append(
                    f"Lesson cluster: {count} unresolved lessons in '{domain}' — "
                    f"consider researching: {domain} best practices"
                )
    except Exception:
        pass

    needs_attention = bool(alerts) or bool(cron_failures)

    # v5 core module presence checks (updated 2026-03-06)
    try:
        core_bins = {
            "stratum-mind": HOME / ".local/bin/stratum-mind",
            "stratum-watch": HOME / ".local/bin/stratum-watch",
            "stratum-ops": HOME / ".local/bin/stratum-ops",
            "stratum-continuity": HOME / ".local/bin/stratum-continuity",
            "stratum-lens": HOME / ".local/bin/stratum-lens",
                    }
        missing = [name for name, path in core_bins.items() if not path.exists()]
        if missing:
            alerts.append(f"Missing core v5 tools: {', '.join(missing)}")

        # Deep report validator status (added 2026-03-06)
        validator_status = HOME / ".local/share/stratum/validator-status.json"
        if validator_status.exists():
            try:
                vdata = json.loads(validator_status.read_text())
                if not vdata.get("passed", True):
                    failures = "; ".join(vdata.get("failures", []))
                    alerts.append(f"Deep report quality gate failed: {failures}")
            except Exception:
                pass
    except Exception:
        pass

    # Continuity integration (added 2026-02-28)
    try:
        continuity_bin = HOME / ".local/bin/stratum-continuity"
        continuity_feed = HOME / ".local/share/stratum/continuity-feed.md"
        continuity_status = HOME / ".local/share/stratum-continuity/status.json"
        if not continuity_bin.exists():
            alerts.append("stratum-continuity missing — continuity snapshots unavailable")
        else:
            # refresh continuity analysis opportunistically
            subprocess.run([str(continuity_bin), "analyze"], capture_output=True, text=True, timeout=20)

            now = time.time()
            if not continuity_feed.exists() or (now - continuity_feed.stat().st_mtime) > 24 * 3600:
                recommendations.append("Continuity feed stale — run: stratum-continuity checkpoint")

            if continuity_status.exists():
                try:
                    data = json.loads(continuity_status.read_text(encoding="utf-8"))
                    flags = data.get("flags", [])
                    if flags:
                        sev = [f.get("severity", "") for f in flags]
                        high = sum(1 for x in sev if x in ("high", "critical"))
                        if high:
                            alerts.append(f"Continuity analysis reports {high} high/critical drift flag(s)")
                    for rec in data.get("recommendations", [])[:2]:
                        recommendations.append(f"Continuity: {rec}")
                except Exception:
                    pass
    except Exception:
        pass

    # stratum-mind goals + world summary
    from .sources import get_goal_stats, get_world_summary
    goal_stats   = get_goal_stats()
    world_summ   = get_world_summary()
    critical_goals = goal_stats.get("critical_active", 0)
    stale_goals    = goal_stats.get("stale_active", 0)
    world_entities = world_summ.entity_count
    if critical_goals:
        alerts.append(f"{critical_goals} critical goal(s) active — check: stratum-mind goals list --tree")
    if stale_goals > 3:
        recommendations.append(
            f"{stale_goals} active goals not evaluated in >7 days — run: stratum-mind goals list --tree"
        )
    # Stale beliefs alert (v2 — decay system)
    if world_summ.stale_count > 0:
        recommendations.append(
            f"{world_summ.stale_count} stale belief(s) need re-verification — "
            f"run: stratum-mind world consolidate-log"
        )
    # Consolidation freshness check — warn if not run in >48h
    if world_summ.last_consolidated:
        try:
            import datetime as _dt
            last_run = _dt.datetime.fromisoformat(world_summ.last_consolidated)
            age_h = (_dt.datetime.utcnow() - last_run).total_seconds() / 3600
            if age_h > 48:
                recommendations.append(
                    f"Knowledge consolidation last ran {age_h:.0f}h ago — "
                    f"run: stratum-mind world consolidate"
                )
        except Exception:
            pass

    return HeartbeatResult(
        alerts=alerts,
        recommendations=recommendations,
        auto_actions=auto_actions,
        context_pct=ctx.pct if ctx.active else 0.0,
        context_level=ctx.level if ctx.active else "inactive",
        cron_failures=cron_failures,
        stash_pending=stash_pending,
        buffer_unacked=buffer_unacked,
        lesson_unresolved=lesson_unresolved,
        needs_attention=needs_attention,
        critical_goals=critical_goals,
        stale_goals=stale_goals,
        world_entities=world_entities,
    )


# ── stratum-mind world + goals feed integration ─────────────────────────

WORLD_FEED_FILE = BRAIN_STATE_DIR / "world-feed.md"
GOALS_FEED_FILE = BRAIN_STATE_DIR / "goals-feed.md"


def update_world_feed() -> None:
    """
    Regenerate the world model feed from mind.db for stratum-lens indexing.

    Writes ~/.local/share/stratum/world-feed.md with entities, beliefs,
    and relations in plain Markdown so stratum-lens can embed and search them.
    Stale beliefs are excluded (marked with stale=1).
    """
    try:
        import sqlite3 as _sql
        from .sources import WORLD_DB
        if not WORLD_DB.exists():
            return
        conn = _sql.connect(str(WORLD_DB))

        entities  = conn.execute("SELECT name, entity_type, description FROM entities ORDER BY name").fetchall()
        beliefs   = conn.execute(
            "SELECT entity, attribute, value, confidence, last_verified FROM beliefs "
            "WHERE stale=0 ORDER BY entity, attribute"
        ).fetchall()
        relations = conn.execute("SELECT subject, predicate, object FROM relations ORDER BY subject").fetchall()
        # stale summary
        stale_n   = conn.execute("SELECT COUNT(*) FROM beliefs WHERE stale=1").fetchone()[0]
        conn.close()

        lines = ["# World Model Feed\n",
                 f"_Generated: {__import__('datetime').datetime.utcnow().isoformat(timespec='minutes')} UTC_\n\n"]

        lines.append("## Entities\n")
        for name, etype, desc in entities:
            lines.append(f"- **{name}** ({etype}){': ' + desc if desc else ''}\n")

        lines.append("\n## Beliefs\n")
        for entity, attr, val, conf, lv in beliefs:
            lv_str = f"  _(verified: {lv[:10]})_" if lv else ""
            lines.append(f"- {entity}[{attr}] = {val}  (conf: {conf:.2f}){lv_str}\n")
        if stale_n:
            lines.append(f"\n_{stale_n} stale belief(s) excluded from this feed._\n")

        lines.append("\n## Relations\n")
        for subj, pred, obj in relations:
            lines.append(f"- {subj} → {pred} → {obj}\n")

        WORLD_FEED_FILE.write_text("".join(lines))
    except Exception:
        pass


def update_goals_feed() -> None:
    """Regenerate the goals feed from unified mind.db (stratum-mind)."""
    try:
        import sqlite3
        from .sources import GOALS_DB
        feed = HOME / ".local/share/stratum/goals-feed.md"
        if not GOALS_DB.exists():
            return
        conn = sqlite3.connect(GOALS_DB)
        rows = conn.execute(
            "SELECT id, title, status, priority, description FROM goals WHERE status='active' ORDER BY CASE priority WHEN 'critical' THEN 1 WHEN 'high' THEN 2 ELSE 3 END"
        ).fetchall()
        conn.close()
        lines = ["# Active Goals (stratum-mind)\n"]
        for r in rows:
            lines.append(f"- [{r[0]}] {r[3]} — {r[1]}: {r[4] or ''}\n")
        feed.write_text("".join(lines))
    except Exception:
        pass



def assemble_session_context(token_budget: int = 2000) -> str:
    """
    Assemble a structured context block for session start.

    Includes (in priority order within the token budget):
      1. Active critical + high goals with success criteria
      2. World model summary (entity/belief counts)
      3. High-severity unresolved lessons (up to 5)
      4. Urgent/high-priority stash items (up to 3)

    Args:
        token_budget: Approximate token budget; 1 token ≈ 4 chars.

    Returns:
        Formatted markdown string ready for prompt injection.
    """
    from .sources import (
        get_active_goals, get_world_summary, get_lesson_items, get_stash_items,
    )

    char_budget = token_budget * 4
    sections: list[str] = []

    # ── 1. Active critical + high goals ──────────────────────────────────
    goals = [g for g in get_active_goals() if g.priority in ("critical", "high")]
    if goals:
        goal_lines = ["## Active Goals (critical + high)\n"]
        for g in goals:
            priority_marker = "🔴" if g.priority == "critical" else "🟡"
            line = f"{priority_marker} [{g.goal_type.upper()}] {g.title}\n"
            if g.success_criteria:
                line += f"  → {g.success_criteria}\n"
            goal_lines.append(line)
        sections.append("".join(goal_lines))

    # ── 2. World model summary ────────────────────────────────────────────
    world = get_world_summary()
    if world.entity_count > 0:
        w_section = (
            f"## World Model\n"
            f"Entities: {world.entity_count}  "
            f"Relationships: {world.relationship_count}  "
            f"Beliefs: {world.belief_count}\n"
        )
        if world.low_confidence_count:
            w_section += f"Low-confidence beliefs: {world.low_confidence_count}\n"
        sections.append(w_section)

    # ── 3. High-severity unresolved lessons ───────────────────────────────
    lessons = [l for l in get_lesson_items(include_resolved=False)
               if l.severity in ("critical", "high")][:5]
    if lessons:
        lesson_lines = [f"## Unresolved Lessons ({len(lessons)} critical/high)\n"]
        for l in lessons:
            lesson_lines.append(f"- [{l.severity}] {l.content[:80]}\n")
        sections.append("".join(lesson_lines))

    # ── 4. Urgent/high stash items ────────────────────────────────────────
    stash = [i for i in get_stash_items() if i.priority in ("urgent", "high")][:3]
    if stash:
        stash_lines = [f"## Stash Alerts ({len(stash)} items)\n"]
        for item in stash:
            stash_lines.append(f"- [{item.priority}] {item.content[:80]}\n")
        sections.append("".join(stash_lines))

    # Combine within budget
    result: list[str] = ["# Session Context\n\n"]
    used = len(result[0])
    for section in sections:
        if used + len(section) > char_budget:
            break
        result.append(section)
        used += len(section)

    return "".join(result)


# ── New tool integrations (2026-03-03) ────────────────────────────────────────

def _refresh_version_watch_feed() -> None:
    """Run stratum-watch version check; updates version feed if drift detected."""
    import subprocess
    vw = HOME / ".local" / "bin" / "stratum-watch"
    if vw.exists():
        subprocess.run([str(vw), "--quiet"], capture_output=True, timeout=30)


def _refresh_report_timeline_feed() -> None:
    """Regenerate stratum-reports timeline feed for lens indexing (every heartbeat)."""
    import subprocess
    feed_path = HOME / ".local" / "share" / "stratum" / "report-timeline-feed.md"
    rt = HOME / ".local" / "bin" / "report-timeline"
    if not rt.exists():
        return
    try:
        lines = ["# Deep Report Timeline\n\n"]
        for cmd in [["list"], ["stats"], ["suggest"], ["themes"]]:
            r = subprocess.run([str(rt)] + cmd, capture_output=True, text=True, timeout=15)
            if r.stdout:
                lines.append(r.stdout + "\n")
        feed_path.parent.mkdir(parents=True, exist_ok=True)
        feed_path.write_text("".join(lines))
    except Exception:
        pass


def _run_cron_cleanup() -> None:
    """Auto-prune completed one-shot crons on every heartbeat."""
    import subprocess
    cc = HOME / ".local" / "bin" / "clawd-cron-cleanup"
    if cc.exists():
        subprocess.run([str(cc), "--apply", "--quiet"], capture_output=True, timeout=30)


def _refresh_pipeline_timer_feed() -> None:
    """Regenerate pipeline-timer feed for lens indexing (every heartbeat).

    Runs 'stratum-pipeline-timer status' which internally writes the feed.md file.
    Low-cost: pure SQLite read, no external calls.
    """
    import subprocess
    pt = HOME / ".local" / "bin" / "clawd-pipeline-timer"
    feed = HOME / ".local" / "share" / "stratum" / "pipeline-timer-feed.md"
    if not pt.exists():
        return
    try:
        # 'status' command writes feed.md as a side effect via _write_feed()
        # We call 'recommend' only if feed is stale (>6h old) to avoid DB writes every heartbeat
        import time
        if feed.exists() and (time.time() - feed.stat().st_mtime) < 21600:
            return  # Feed is fresh enough
        subprocess.run([str(pt), "recommend"], capture_output=True, timeout=15)
    except Exception:
        pass


def _refresh_docker_watcher_feed() -> None:
    """Run clawd-docker-watcher check (non-destructive); updates feed.md for lens indexing.

    Only polls nodejs.org + Docker Hub; does NOT trigger SSH rebuilds unless a
    pending_rebuild was already set and the Docker Hub tag is now available.
    Safe to run on every heartbeat — all network calls are read-only unless the
    tag appears AND pending_rebuild is True (which triggers the rebuild correctly).
    """
    import subprocess
    dw = HOME / ".local" / "bin" / "clawd-docker-watcher"
    if dw.exists():
        subprocess.run([str(dw), "check"], capture_output=True, timeout=60)


def _refresh_memory_projector_feed() -> None:
    """Take a memory tier snapshot and update the projector feed.md for lens indexing.

    Runs 'clawd-memory-projector snapshot' then 'clawd-memory-projector status'
    (status writes feed.md as a side effect). Capped to once per hour to avoid
    polluting the DB with redundant snapshots on rapid heartbeat cycles.

    Added 2026-03-03.
    """
    import time
    mp = HOME / ".local" / "bin" / "clawd-memory-projector"
    feed = HOME / ".local" / "share" / "clawd-memory-projector" / "feed.md"
    if not mp.exists():
        return
    try:
        # Only snapshot once per hour — status/feed refresh always runs
        if not feed.exists() or (time.time() - feed.stat().st_mtime) >= 3600:
            subprocess.run([str(mp), "snapshot"], capture_output=True, timeout=10)
        subprocess.run([str(mp), "status"], capture_output=True, timeout=10)
    except Exception:
        pass


def _refresh_task_primer_feed() -> None:
    """Refresh clawd-task-primer feed.md if stale (>2h), using active-context task.

    Reads active-context.md to extract the first non-blank, non-header line as
    the 'current task' description, then runs clawd-task-primer surface on it.
    This ensures the lens feed stays current without needing an explicit call.

    Added 2026-03-03.
    """
    import time
    tp = HOME / ".local" / "bin" / "clawd-task-primer"
    feed = HOME / ".local" / "share" / "clawd-task-primer" / "feed.md"
    if not tp.exists():
        return
    # Only refresh if feed is stale (>2h)
    if feed.exists() and (time.time() - feed.stat().st_mtime) < 7200:
        return
    try:
        active_ctx = HOME / "clawd" / "memory" / "active-context.md"
        task = ""
        if active_ctx.exists():
            for line in active_ctx.read_text(errors="ignore").splitlines():
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and len(stripped) > 15:
                    task = stripped[:120]
                    break
        if task:
            subprocess.run([str(tp), "surface", task], capture_output=True, timeout=30)
    except Exception:
        pass
