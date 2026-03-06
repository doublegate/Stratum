"""
indexer.py — File discovery, mtime tracking, and incremental re-indexing.

Purpose: Determine which workspace files need (re)indexing based on mtime,
  then orchestrate chunking + embedding + storing for those files.

Design:
  - Index targets are configured via DEFAULT_TARGETS: a list of paths/globs
    relative to the workspace root (~/.local/share/stratum-lens/index-state.json
    tracks per-file mtime so we only re-index changed files).
  - Full re-index (--force) deletes stale chunks and re-embeds everything.
  - Incremental re-index checks mtime against last-indexed timestamp.
  - State file format: { "path": mtime_float, ... }

Target files (from your workspace):
  $STRATUM_WORKSPACE/MEMORY.md          — long-term curated memory
  $STRATUM_WORKSPACE/IDENTITY.md        — who your agent is
  $STRATUM_WORKSPACE/SOUL.md            — values and aspirations
  $STRATUM_WORKSPACE/USER.md            — about the user
  $STRATUM_WORKSPACE/TOOLS.md           — tool notes and configs
  $STRATUM_WORKSPACE/AGENTS.md          — workspace conventions
  $STRATUM_WORKSPACE/memory/*.md        — daily notes and active context
  $STRATUM_WORKSPACE/reports/markdown/  — deep reports (optional, large)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Iterator

from .chunker import Chunk, chunk_file
from .store import WorkspaceStore


# ─── Index State ──────────────────────────────────────────────────────────────

def state_path() -> Path:
    """Return the path to the mtime tracking state file."""
    base = Path(os.environ.get("HOME", "/")) / ".local/share/stratum-lens"


def _workspace() -> Path:
    """Return the Stratum workspace path from env var or default ~/clawd."""
    ws = os.environ.get("STRATUM_WORKSPACE", "")
    if ws:
        return Path(ws).expanduser()
    return Path.home() / "clawd"
    base.mkdir(parents=True, exist_ok=True)
    return base / "index-state.json"


def load_state() -> dict[str, float]:
    """Load the per-file mtime state dict. Returns {} if file doesn't exist."""
    p = state_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(state: dict[str, float]) -> None:
    """Persist the mtime state dict to disk."""
    state_path().write_text(json.dumps(state, indent=2))


# ─── Target File Discovery ────────────────────────────────────────────────────

# Core workspace files — always indexed
CORE_TARGETS: list[str] = [
    str(_workspace() / "MEMORY.md"),
    str(_workspace() / "IDENTITY.md"),
    str(_workspace() / "SOUL.md"),
    str(_workspace() / "USER.md"),
    str(_workspace() / "TOOLS.md"),
    str(_workspace() / "AGENTS.md"),
    str(_workspace() / "HEARTBEAT.md"),
    str(_workspace() / "LEARNING.md"),          # growth narrative — indexed for self-referential queries
    str(_workspace() / "memory/active-context.md"),
    # stratum-brain integration feeds (updated by clawd-cron-health and clawd-lesson)
    "~/.local/share/stratum-brain/cron-health-feed.md",
    "~/.local/share/stratum-brain/lesson-feed.md",
    # stratum-brain reflection feed (updated by weekly reflection cron)
    "~/.local/share/stratum-brain/reflection-feed.md",
    # clawd-world and clawd-goals feeds (added 2026-02-25)
    "~/.local/share/stratum-brain/world-feed.md",
    "~/.local/share/stratum-brain/goals-feed.md",
    # clawd-behavior feed (added 2026-02-25 — grows as session data accumulates)
    "~/.local/share/stratum-brain/behavior-feed.md",
    # Hardening suite feeds (added 2026-02-27)
    "~/.local/share/clawd-preflight/feed.md",
    "~/.local/share/clawd-cron-reconcile/last-report.md",
    "~/.local/share/clawd-lesson-autopilot/feed.md",
    "~/.local/share/clawd-lesson-autopilot/review-queue.md",
    "~/.local/share/clawd-report-runbook/feed.md",
    "~/.local/share/stratum-continuity/feed.md",
    "~/.local/share/stratum-continuity/status.json",
    "~/.local/share/stratum-continuity/last-report.md",
]

# Glob patterns — all matching files are indexed
GLOB_TARGETS: list[tuple[str, str]] = [
    # (base_dir, glob_pattern)
    (str(_workspace() / "memory"), "*.md"),          # daily notes and active context
    (str(_workspace() / "memory/warm"), "*.md"),     # warm tier topic files (partitioned from MEMORY.md)
    (str(_workspace() / "skills"), "*/SKILL.md"),    # skill documentation
    (str(_workspace() / "research"), "*.md"),        # autonomous research notes
    (str(_workspace() / "reports/markdown"), "*.md"),  # deep reports — always indexed
    ("~/.local/share/clawd-report-ingest", "*.md"),  # report insight extractions
]

REPORT_TARGETS: list[tuple[str, str]] = []  # moved to GLOB_TARGETS — always indexed


def discover_targets(include_reports: bool = False) -> Iterator[Path]:
    """
    Yield all target file paths that should be in the index.

    Resolves ~ and skips files that don't exist.
    """
    # Core single-file targets
    for target in CORE_TARGETS:
        p = Path(target).expanduser()
        if p.exists() and p.is_file():
            yield p

    # Glob targets
    glob_specs = GLOB_TARGETS[:]
    if include_reports:
        glob_specs.extend(REPORT_TARGETS)

    for base_str, pattern in glob_specs:
        base = Path(base_str).expanduser()
        if base.exists():
            for p in sorted(base.glob(pattern)):
                if p.is_file():
                    yield p


# ─── Indexing Logic ───────────────────────────────────────────────────────────

class IndexStats:
    """Accumulate and report indexing statistics."""

    def __init__(self) -> None:
        self.files_checked = 0
        self.files_indexed = 0
        self.files_skipped = 0
        self.chunks_added = 0
        self.errors: list[str] = []

    def __str__(self) -> str:
        lines = [
            f"Files checked:  {self.files_checked}",
            f"Files indexed:  {self.files_indexed}",
            f"Files skipped:  {self.files_skipped} (unchanged)",
            f"Chunks added:   {self.chunks_added}",
        ]
        if self.errors:
            lines.append(f"Errors:         {len(self.errors)}")
            for e in self.errors:
                lines.append(f"  - {e}")
        return "\n".join(lines)


def run_index(
    store: WorkspaceStore,
    force: bool = False,
    include_reports: bool = False,
    verbose: bool = False,
) -> IndexStats:
    """
    Run the indexing pass (must be called while holding the write lock).

    Parameters:
        store           — open WorkspaceStore to write chunks into
        force           — re-index all files even if mtime unchanged
        include_reports — also index $STRATUM_WORKSPACE/reports/markdown/*.md
        verbose         — print per-file progress

    Returns IndexStats with a summary of what was done.

    NOTE: This function does NOT acquire the write lock itself — callers
    (cli.py `index` command and run_watch) are responsible for doing so.
    This prevents double-acquisition from run_watch calling run_index.
    """
    stats = IndexStats()
    state = load_state()
    new_state = dict(state)  # we'll update this as we go

    for path in discover_targets(include_reports=include_reports):
        stats.files_checked += 1
        path_str = str(path)

        # Check if the file has changed since last index
        try:
            mtime = path.stat().st_mtime
        except OSError as e:
            stats.errors.append(f"{path.name}: stat failed — {e}")
            continue

        last_mtime = state.get(path_str, 0.0)

        if not force and mtime <= last_mtime:
            stats.files_skipped += 1
            if verbose:
                print(f"  skip  {path.name}")
            continue

        # File is new or modified — delete stale chunks and re-index
        if verbose:
            print(f"  index {path.name} ...", end=" ", flush=True)

        try:
            # Remove old chunks for this file (safe if file is new)
            store.delete_by_source(path_str)

            # Chunk and embed the file
            chunks: list[Chunk] = chunk_file(path)
            if chunks:
                n = store.upsert_chunks(chunks)
                stats.chunks_added += n
                if verbose:
                    print(f"{n} chunks")
            else:
                if verbose:
                    print("0 chunks (empty or unreadable)")

            # Update state with new mtime
            new_state[path_str] = mtime
            stats.files_indexed += 1

        except Exception as e:
            stats.errors.append(f"{path.name}: {e}")
            if verbose:
                print(f"ERROR: {e}")

    # Persist updated mtime state
    save_state(new_state)

    return stats


def run_watch(store: WorkspaceStore, interval_seconds: int = 60) -> None:
    """
    Continuously poll for file changes and re-index as needed.
    Blocks forever. Designed for running as a background daemon.

    Holds the write lock for the duration of each index pass.
    Also responds to signal_file triggers from the CLI `index` command
    when it detects the lock is held (CLI creates the signal file instead
    of trying to open ChromaDB directly).
    """
    from .lock import write_lock, check_and_clear_signal

    print(f"[stratum-lens] Watching for changes (poll every {interval_seconds}s)...")
    while True:
        # Check for external reindex signal (from CLI when it can't get the lock)
        forced = check_and_clear_signal()

        with write_lock(timeout_secs=30):
            stats = run_index(store, force=forced, verbose=False)

        if stats.files_indexed > 0:
            print(
                f"[stratum-lens] Re-indexed {stats.files_indexed} file(s), "
                f"{stats.chunks_added} chunk(s) updated."
            )
        if forced and stats.files_indexed == 0:
            print("[stratum-lens] Reindex signal processed (no changes found).")

        time.sleep(interval_seconds)
