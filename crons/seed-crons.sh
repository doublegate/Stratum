#!/usr/bin/env bash
# seed-crons.sh — Install canonical Stratum cron set into OpenClaw
#
# Run once after install, or re-run to restore defaults.
# All jobs use America/New_York by default — edit TZ= below to change.
#
# Usage: bash crons/seed-crons.sh [--dry-run] [--tz "America/Chicago"]

set -euo pipefail

TZ="America/New_York"
DRY_RUN=false
for arg in "$@"; do
  case $arg in
    --dry-run) DRY_RUN=true ;;
    --tz) shift; TZ="$1" ;;
  esac
done

add_cron() {
  local name="$1" schedule="$2" model="$3" message="$4"
  if $DRY_RUN; then
    echo "[dry-run] Would add cron: $name ($schedule)"
    return
  fi
  openclaw cron add \
    --name "$name" \
    --cron "$schedule" \
    --timezone "$TZ" \
    --session isolated \
    --model "$model" \
    --announce \
    --message "$message"
  echo "  ✓ $name"
}

echo "Seeding Stratum canonical cron set (TZ: $TZ)..."
echo ""

# ── Knowledge & Memory ────────────────────────────────────────────────────────
echo "Knowledge & Memory:"
add_cron "Knowledge-Consolidation" "0 3 * * *" "anthropic/claude-sonnet-4-6" \
  "Run stratum-brain analyze --create-lessons. Then: stratum-mind memory weekly to check hot-tier budget. Capture any new insight-category lessons from the analysis. Report count of new lessons and any memory tier warnings."

add_cron "Weekly-Self-Reflection" "0 2 * * 0" "anthropic/claude-opus-4-6" \
  "Read all core workspace files (SOUL.md, AGENTS.md, HEARTBEAT.md, MEMORY.md, USER.md). Run stratum-brain status. Make targeted updates to SOUL.md and LEARNING.md for any genuine shifts in understanding or behavior since last week. Write a brief reflection summary to memory/YYYY-MM-DD.md."

add_cron "World-Model-Sync" "15 3 * * *" "anthropic/claude-sonnet-4-6" \
  "Run stratum-mind world status to check the knowledge graph. Update any stale dynamic beliefs (project versions, active tasks, host info) via stratum-mind world add belief. Log any updated belief count to memory/YYYY-MM-DD.md."

# ── Continuity ────────────────────────────────────────────────────────────────
echo "Continuity:"
add_cron "Continuity-Checkpoint-Loop" "0 */2 * * *" "anthropic/claude-sonnet-4-6" \
  "Run stratum-continuity checkpoint to snapshot current session state. Then stratum-continuity analyze to check for drift. If drift detected, capture a lesson via stratum-mind lesson learn. Report only if issues found."

add_cron "Session-Primer-Refresh" "30 7 * * *" "anthropic/claude-sonnet-4-6" \
  "Run stratum-continuity primer --refresh to regenerate the session start brief from current state. Verify it looks accurate. Log completion."

# ── Observability ─────────────────────────────────────────────────────────────
echo "Observability:"
add_cron "Cron-Health-Check" "0 9 * * *" "anthropic/claude-sonnet-4-6" \
  "Run stratum-watch status to check cron health. If any jobs show failed or unknown status 3+ times, investigate and capture a lesson. Alert only on genuine failures."

add_cron "Version-Drift-Check" "0 10 * * 1" "anthropic/claude-sonnet-4-6" \
  "Run stratum-watch version check to detect drift between Node, OpenClaw, and Stratum module versions across hosts. Log findings. Alert if any version is more than 2 weeks stale."

# ── Operations ────────────────────────────────────────────────────────────────
echo "Operations:"
add_cron "Ops-Queue-Check" "0 8 * * *" "anthropic/claude-sonnet-4-6" \
  "Run stratum-ops queue list to check for any pending privileged operations. If items are queued and safe to apply, report them so the user can approve. Never apply elevated ops automatically."

add_cron "Cron-Cleanup" "0 4 * * 0" "anthropic/claude-sonnet-4-6" \
  "Run stratum-ops cron cleanup to remove completed one-shot cron jobs. Report count removed."

# ── Autonomous Research ───────────────────────────────────────────────────────
echo "Research:"
add_cron "Daily-Autonomous-Research" "0 5 * * *" "anthropic/claude-sonnet-4-6" \
  "Check stratum-brain for any queued research topics. Pick the highest-priority item, research it using web search, save findings. Then stratum-mind lesson learn any key insights discovered. Log completion to memory/YYYY-MM-DD.md."

# ── Lens Index ────────────────────────────────────────────────────────────────
echo "Lens:"
add_cron "Lens-Index-Rebuild" "30 2 * * *" "anthropic/claude-sonnet-4-6" \
  "Run stratum-lens index to incrementally update the semantic search index with any new files from the past 24 hours. Report chunk count and any errors."

echo ""
echo "Done. Run 'openclaw cron list' to verify."
