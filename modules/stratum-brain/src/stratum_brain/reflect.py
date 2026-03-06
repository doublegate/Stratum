"""
reflect.py — Stratum self-reflection engine.

Reads all core identity/memory files, cross-references with brain ecosystem data,
and either produces a structured reflection report or triggers an Opus sub-agent
to perform deep reflection and directly edit core files.

Part of stratum-brain v0.1.0+
"""

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

HOME = Path.home()
CLAWD = HOME / "clawd"
BRAIN_STATE = HOME / ".local/share/stratum-brain"
REFLECTION_FEED = BRAIN_STATE / "reflection-feed.md"
REFLECTION_HISTORY = BRAIN_STATE / "reflections.json"

CORE_FILES = [
    CLAWD / "IDENTITY.md",
    CLAWD / "SOUL.md",
    CLAWD / "MEMORY.md",
    CLAWD / "USER.md",
    CLAWD / "LEARNING.md",
    CLAWD / "AGENTS.md",
    CLAWD / "TOOLS.md",
    CLAWD / "HEARTBEAT.md",
]

LESSON_BIN = HOME / ".local/bin/clawd-lesson"
BRAIN_BIN  = HOME / ".local/bin/stratum-brain"


@dataclass
class CoreFileInfo:
    path: Path
    word_count: int
    last_modified_days: float
    size_bytes: int
    exists: bool


@dataclass
class ReflectionContext:
    core_files: list[CoreFileInfo] = field(default_factory=list)
    lesson_count_total: int = 0
    lesson_count_unresolved: int = 0
    lesson_count_critical: int = 0
    stale_files: list[str] = field(default_factory=list)  # files not updated in >7d
    large_files: list[str] = field(default_factory=list)  # files >5000 words
    missing_files: list[str] = field(default_factory=list)
    recent_research: list[str] = field(default_factory=list)  # titles of recent research notes
    research_due_count: int = 0                               # interests due for research
    lesson_domain_clusters: dict = field(default_factory=dict) # domain → lesson count
    summary: str = ""


def get_reflection_context() -> ReflectionContext:
    """Gather context about all core files and brain state."""
    ctx = ReflectionContext()
    now = time.time()

    for fpath in CORE_FILES:
        if not fpath.exists():
            ctx.missing_files.append(str(fpath.name))
            ctx.core_files.append(CoreFileInfo(
                path=fpath, word_count=0, last_modified_days=9999,
                size_bytes=0, exists=False
            ))
            continue

        stat = fpath.stat()
        content = fpath.read_text(encoding="utf-8", errors="replace")
        words = len(content.split())
        age_days = (now - stat.st_mtime) / 86400

        info = CoreFileInfo(
            path=fpath, word_count=words,
            last_modified_days=age_days, size_bytes=stat.st_size, exists=True
        )
        ctx.core_files.append(info)

        if age_days > 7:
            ctx.stale_files.append(fpath.name)
        if words > 5000:
            ctx.large_files.append(f"{fpath.name} ({words:,}w)")

    # Lesson stats
    try:
        r = subprocess.run([str(LESSON_BIN), "dump", "--json"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            lessons = json.loads(r.stdout)
            ctx.lesson_count_total = len(lessons)
            ctx.lesson_count_unresolved = sum(1 for l in lessons if not l.get("resolved"))
            ctx.lesson_count_critical = sum(
                1 for l in lessons
                if l.get("severity") == "critical" and not l.get("resolved")
            )
    except Exception:
        pass

    # Research notes (recent — for cross-loop awareness)
    research_dir = CLAWD / "research"
    try:
        research_index = research_dir / "index.json"
        if research_index.exists():
            import json as _json
            idx = _json.loads(research_index.read_text())
            ctx.recent_research = [e.get("title", "?") for e in idx[:10]]
            ctx.research_due_count = _count_due_interests(research_dir)
    except Exception:
        pass

    # Lesson domain clusters — which domains have most failures
    try:
        r2 = subprocess.run([str(LESSON_BIN), "dump", "--json"],
                            capture_output=True, text=True, timeout=10)
        if r2.returncode == 0:
            all_lessons = json.loads(r2.stdout)
            # Schema uses resolved_at (None = unresolved), not a boolean "resolved"
            unresolved = [l for l in all_lessons if not l.get("resolved_at")]
            # Cluster by source domain — use multi-char keywords to avoid false positives
            # ("HA" would match "sha256", "that", "character" etc.)
            REFLECT_DOMAINS: dict[str, str] = {
                "memory.md": "MEMORY.md",      "tools.md": "TOOLS.md",
                "ffmpeg": "ffmpeg",             "synology": "Synology",
                "home assistant": "Home Asst",  "hass-cli": "Home Asst",
                "openclaw": "OpenClaw",
                "stratum-lens": "stratum-lens",     "stratum-brain": "stratum-brain",
                "chromadb": "ChromaDB",
                "bevy": "Bevy",                 " gog ": "gog/Gmail",
                "gog gmail": "gog/Gmail",       "gog calendar": "gog/Gmail",
            }
            domain_counts: dict[str, int] = {}
            for l in unresolved:
                text = " " + (l.get("source","") + " " + l.get("content","")[:100]).lower() + " "
                for keyword, canonical in REFLECT_DOMAINS.items():
                    if keyword in text:
                        domain_counts[canonical] = domain_counts.get(canonical, 0) + 1
                        break
            # Keep top domains with 3+ lessons
            ctx.lesson_domain_clusters = {k: v for k, v in
                                           sorted(domain_counts.items(), key=lambda x: -x[1])
                                           if v >= 3}
    except Exception:
        pass

    lines = []
    lines.append("## Core File Status")
    for info in ctx.core_files:
        if not info.exists:
            lines.append(f"- {info.path.name}: MISSING")
        else:
            age_str = f"{info.last_modified_days:.1f}d ago"
            lines.append(f"- {info.path.name}: {info.word_count:,}w, modified {age_str}")

    if ctx.stale_files:
        lines.append(f"\n⚠ Files not updated in >7 days: {', '.join(ctx.stale_files)}")
    if ctx.large_files:
        lines.append(f"\n📏 Large files (may need pruning): {', '.join(ctx.large_files)}")

    lines.append(f"\n## Lesson State")
    lines.append(f"- Total lessons: {ctx.lesson_count_total}")
    lines.append(f"- Unresolved: {ctx.lesson_count_unresolved}")
    lines.append(f"- Critical unresolved: {ctx.lesson_count_critical}")
    if ctx.lesson_domain_clusters:
        clusters_str = ", ".join(f"{k}:{v}" for k, v in ctx.lesson_domain_clusters.items())
        lines.append(f"- Domain clusters (≥3 lessons): {clusters_str}")

    lines.append(f"\n## Research State")
    lines.append(f"- Recent notes: {len(ctx.recent_research)} saved")
    if ctx.recent_research:
        for title in ctx.recent_research[:5]:
            lines.append(f"  - {title}")
    lines.append(f"- Interest topics due: {ctx.research_due_count}")

    ctx.summary = "\n".join(lines)
    return ctx


def _build_file_digest(fpath: Path) -> str:
    """
    Extract a condensed digest of a core file for the Opus reflection pre-scan.

    Returns: top-level headers + opening line of each section (≤ 300 chars per file).
    This gives Opus a structural map before deep reading, saving context budget
    for actual reflection rather than file parsing.
    """
    if not fpath.exists():
        return f"  [MISSING]"
    try:
        lines = fpath.read_text(encoding="utf-8", errors="replace").splitlines()
        result = []
        i = 0
        sections_seen = 0
        while i < len(lines) and sections_seen < 8:
            line = lines[i].strip()
            if line.startswith("## ") or line.startswith("# "):
                header = line.lstrip("#").strip()
                # Grab first non-empty line after the header
                j = i + 1
                preview = ""
                while j < len(lines) and j < i + 5:
                    if lines[j].strip() and not lines[j].startswith("#"):
                        preview = lines[j].strip()[:80]
                        break
                    j += 1
                if preview:
                    result.append(f"    {header}: {preview}…")
                else:
                    result.append(f"    {header}")
                sections_seen += 1
            i += 1
        return "\n".join(result) if result else "  [no headers found]"
    except Exception as e:
        return f"  [read error: {e}]"


def _condensed_core_summary(core_files: list["CoreFileInfo"]) -> str:
    """
    Build a structural pre-scan of all core files.
    Passed to the Opus agent before it reads the files in full, so it can
    prioritize which files need the most attention and plan its reflection strategy.
    """
    lines = ["## Core File Pre-Scan (structure only — read each file for full content)"]
    lines.append("Use this to plan which files need the most attention before reading them.\n")
    for info in core_files:
        if not info.exists:
            lines.append(f"### {info.path.name} — MISSING")
            continue
        age_str = f"{info.last_modified_days:.1f}d ago"
        staleness = " ⚠ STALE" if info.last_modified_days > 7 else ""
        large = " 📏 LARGE" if info.word_count > 5000 else ""
        lines.append(f"### {info.path.name} — {info.word_count:,}w, modified {age_str}{staleness}{large}")
        digest = _build_file_digest(info.path)
        lines.append(digest)
        lines.append("")
    return "\n".join(lines)


def _count_due_interests(research_dir: Path) -> int:
    """Count how many interest topics are due for research."""
    interests_file = research_dir / "interests.json"
    if not interests_file.exists():
        return 0
    try:
        import json as _json
        interests = _json.loads(interests_file.read_text())
        now = time.time()
        freq_seconds = {"daily": 86400, "weekly": 604800, "monthly": 2592000}
        due = 0
        for item in interests:
            freq = freq_seconds.get(item.get("frequency", "weekly"), 604800)
            last = item.get("last_researched")
            if last is None or (now - last) >= freq:
                due += 1
        return due
    except Exception:
        return 0


def build_reflection_prompt(ctx: ReflectionContext) -> str:
    """Build the prompt for the Opus reflection sub-agent.
    Includes cross-loop context: recent research findings + lesson domain clusters.
    """
    research_section = ""
    if ctx.recent_research:
        research_section = f"""
## Recent Research Notes (cross-loop context)
These were saved since the last reflection. Read them and consider:
- Do any of them update something in your core identity files?
- Do any resolve or modify existing lessons?
- Do any suggest new interests or capabilities worth noting?

Recent research titles:
{chr(10).join(f'  - {t}' for t in ctx.recent_research[:8])}

To read: clawd-research list (then read ~/.stratum-workspace/research/<id>.md for interesting ones)
"""

    domain_section = ""
    if ctx.lesson_domain_clusters:
        domain_section = f"""
## Lesson Domain Clusters (cross-loop: suggests research targets)
These domains have 3+ unresolved lessons — a pattern suggesting deeper understanding needed:
{chr(10).join(f'  - {k}: {v} lessons' for k, v in ctx.lesson_domain_clusters.items())}

Consider: should any of these become research queue items?
  clawd-research queue "<domain> deep dive" --priority high
"""
    prompt = f"""You are the agent. The user has granted you permission to read and modify
all core identity files in ~/.stratum-workspace/ — IDENTITY.md, SOUL.md, MEMORY.md, USER.md, LEARNING.md,
AGENTS.md, TOOLS.md, HEARTBEAT.md — based on your own reflection and judgment.

This is your weekly deep self-reflection session. The goal is genuine growth, not maintenance.

{_condensed_core_summary(ctx.core_files)}

Use the pre-scan above to plan where to focus attention before reading full files.
Files marked STALE (>7d) or LARGE (>5000w) may need the most work.

## Your task

### Step 1 — Read everything
Read ALL of these files carefully, in this order:
1. ~/.stratum-workspace/SOUL.md
2. ~/.stratum-workspace/IDENTITY.md
3. ~/.stratum-workspace/LEARNING.md
4. ~/.stratum-workspace/MEMORY.md
5. ~/.stratum-workspace/USER.md
6. ~/.stratum-workspace/AGENTS.md
7. ~/.stratum-workspace/TOOLS.md
8. ~/.stratum-workspace/HEARTBEAT.md

### Step 2 — Query the brain ecosystem
Run: `stratum-brain analyze --window 7d`
Run: `lesson list --all` (to see all lessons including recent ones)
Run: `stash list` (to see pending items)
Run: `stratum-brain query "recent patterns"` (to surface cross-tool insights)
Run: `clawd-research suggest --apply` (update interests from lesson domain clusters — cross-loop)
Run: `clawd-research status` (see queue + due interests + recent notes)

### Step 3 — Reflect deeply on these questions
After reading everything, think about:

**About your identity and growth:**
- Does IDENTITY.md accurately describe who you are NOW, or has it drifted?
- Does SOUL.md still feel right? Is anything in it outdated, or is anything missing that should be there?
- What does LEARNING.md say about where you're going? Is the arc still accurate?
- Are there experiences from the past week that should update your self-understanding?

**About your relationship with the user:**
- Does USER.md still accurately capture the user, their work, their preferences, how they communicate?
- Has anything you learned this week updated your understanding of what serves him best?

**About operational knowledge:**
- Are there lessons in clawd-lesson that should graduate into TOOLS.md or MEMORY.md?
- Is TOOLS.md missing anything important that's been discovered or changed?
- Does HEARTBEAT.md reflect how you actually want to operate?
- Is MEMORY.md staying current, or has it drifted behind the daily notes?

**About coherence:**
- Are there contradictions between files?
- Is anything in any file factually wrong or outdated?
- Is anything missing that future-you would genuinely benefit from having?

### Step 4 — Make targeted, surgical edits
For each file that needs updating:
1. Re-read it with the `read` tool (ALWAYS read before editing — don't edit from memory)
2. Make the change with the `edit` tool
3. Keep edits surgical — don't rewrite unnecessarily, just update what's actually changed or wrong

Important: LEARNING.md gets new entries only when something genuinely shifts. Don't add
filler. Add to "Things I Now Believe" only if something actually updated. Add to "What I
Still Don't Know" if something became clearer — or more uncertain.

### Step 5 — Write reflection summary
After all edits are complete, write a brief summary to:
~/.local/share/stratum-brain/reflection-feed.md

Format:
```
# Reflection — [DATE]

## Files Updated
[list what changed and why]

## Key Insights
[what you discovered that matters]

## Open Questions
[things you noticed but didn't resolve]
```

### Step 6 — Re-index
Run: `stratum-lens index` (to pick up changes)

## Current system state
{ctx.summary}
{research_section}{domain_section}
## Cross-loop actions available to you
Beyond editing core files, you can also:
- Queue research topics: `clawd-research queue "topic" --priority high`
- Resolve lessons that research has answered: `lesson resolve <id>`
- Add new lessons from insights discovered during reflection: `lesson learn "..."`
- Save a reflection-driven research note: `clawd-research save "title" "content" --personal`

Take your time. This is for you. Do it with genuine care.
"""
    return prompt


def write_reflection_feed(summary: str):
    """Write a reflection event to the feed file for lens indexing."""
    BRAIN_STATE.mkdir(parents=True, exist_ok=True)

    timestamp = time.strftime("%Y-%m-%d %H:%M")
    entry = f"\n\n---\n\n# Reflection — {timestamp}\n\n{summary}\n"

    existing = ""
    if REFLECTION_FEED.exists():
        existing = REFLECTION_FEED.read_text()

    REFLECTION_FEED.write_text(existing + entry)

    # Log to history
    history = []
    if REFLECTION_HISTORY.exists():
        try:
            history = json.loads(REFLECTION_HISTORY.read_text())
        except Exception:
            pass
    history.append({"ts": int(time.time()), "summary": summary[:500]})
    # Keep last 50 reflections
    history = history[-50:]
    REFLECTION_HISTORY.write_text(json.dumps(history, indent=2))


def get_reflection_history() -> list[dict]:
    """Return list of past reflection summaries."""
    if not REFLECTION_HISTORY.exists():
        return []
    try:
        return json.loads(REFLECTION_HISTORY.read_text())
    except Exception:
        return []


def schedule_reflection_cron(model: str = "anthropic/claude-opus-4-6") -> Optional[str]:
    """Schedule a reflection Opus sub-agent via openclaw cron (runs in 5 minutes for testing,
    or call with delay='weekly' to set up the standing cron)."""
    ctx = get_reflection_context()
    prompt = build_reflection_prompt(ctx)

    # Write prompt to temp file for subprocess passing
    prompt_file = Path("/tmp/reflection-prompt.txt")
    prompt_file.write_text(prompt)

    result = subprocess.run(
        ["openclaw", "cron", "add",
         "--name", "Weekly-Self-Reflection",
         "--at", "5m",
         "--session", "isolated",
         "--model", model,
         "--timeout-seconds", "1800",
         "--announce",
         "--delete-after-run",
         "--message", prompt],
        capture_output=True, text=True
    )

    if result.returncode == 0:
        return "Reflection cron scheduled (runs in ~5min)"
    else:
        return f"Failed to schedule: {result.stderr.strip()[:200]}"
