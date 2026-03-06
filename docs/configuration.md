# Stratum — Configuration Reference

All configuration lives in `~/.stratum/config.json`. The installer creates this
from `config/examples/config.example.json`. This file is **gitignored** — it
never leaves your machine.

---

## Full Schema

```json
{
  "user": {
    "name": "Your Name",
    "timezone": "America/New_York",
    "telegram_id": ""
  },
  "paths": {
    "workspace": "~/clawd",
    "data": "~/.local/share/stratum",
    "bin": "~/.local/bin"
  },
  "modules": {
    "lens": {
      "auto_scale_threshold": 0.85,
      "embedding_model": "all-MiniLM-L6-v2",
      "chroma_path": "~/.local/share/stratum/chroma"
    },
    "brain": {
      "consolidation_hour": 3,
      "belief_decay_days": 30,
      "fts_weight_semantic": 0.6,
      "fts_weight_keyword": 0.4
    },
    "continuity": {
      "snapshot_interval_hours": 2,
      "brief_max_words": 500
    },
    "watch": {
      "context_alert_threshold": 0.6,
      "version_stale_days": 14
    }
  },
  "notifications": {
    "channel": "telegram",
    "quiet_hours_start": "23:00",
    "quiet_hours_end": "08:00"
  },
  "hosts": {
    "primary": {
      "name": "primary",
      "hostname": "localhost"
    },
    "standby": {
      "name": "standby",
      "hostname": ""
    }
  }
}
```

---

## Field Reference

### `user`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `name` | string | — | Your name. Used in persona templates and reflection prompts. |
| `timezone` | string | `"UTC"` | IANA timezone string (e.g. `"America/New_York"`). Used by cron jobs. |
| `telegram_id` | string | `""` | Your Telegram chat ID. Used by notification crons. Optional. |

### `paths`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `workspace` | string | `"~/clawd"` | Path to your OpenClaw workspace directory. Can also be set via `$STRATUM_WORKSPACE` env var. |
| `data` | string | `"~/.local/share/stratum"` | Where Stratum stores its SQLite databases, feeds, and state files. |
| `bin` | string | `"~/.local/bin"` | Where compiled binaries are installed. Must be in your `$PATH`. |

### `modules.lens`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `auto_scale_threshold` | float | `0.85` | Memory usage fraction at which `stratum-lens` triggers a reindex + prune. |
| `embedding_model` | string | `"all-MiniLM-L6-v2"` | Local embedding model. `all-MiniLM-L6-v2` is the default; ARM64-compatible, no GPU required. |
| `chroma_path` | string | `"~/.local/share/stratum/chroma"` | ChromaDB persistence directory. |

### `modules.brain`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `consolidation_hour` | int | `3` | Hour (24h, local time) when nightly consolidation runs. Default 3 AM. |
| `belief_decay_days` | int | `30` | Beliefs not reinforced within this many days have their confidence reduced. |
| `fts_weight_semantic` | float | `0.6` | Weight given to semantic (vector) results in hybrid search. |
| `fts_weight_keyword` | float | `0.4` | Weight given to FTS5 keyword results in hybrid search. |

### `modules.continuity`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `snapshot_interval_hours` | int | `2` | How often (hours) to automatically snapshot session state. |
| `brief_max_words` | int | `500` | Maximum word count for the session-start primer brief. |

### `modules.watch`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `context_alert_threshold` | float | `0.6` | Context window fill fraction at which `stratum-watch` triggers a pre-compaction checkpoint. |
| `version_stale_days` | int | `14` | Warn when any Stratum component hasn't been updated in this many days. |

### `notifications`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `channel` | string | `"telegram"` | Notification channel. `"telegram"` is the only currently-supported value. |
| `quiet_hours_start` | string | `"23:00"` | No notifications sent after this time (local timezone). |
| `quiet_hours_end` | string | `"08:00"` | Notifications resume after this time. |

### `hosts`

| Field | Type | Description |
|-------|------|-------------|
| `primary.name` | string | Human-readable name for the primary host. |
| `primary.hostname` | string | Hostname or IP of the primary host (used in status displays). |
| `standby.name` | string | Human-readable name for the optional standby host. Leave blank if not using dual-host. |
| `standby.hostname` | string | Hostname or IP of the standby host. |

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `STRATUM_WORKSPACE` | Override `paths.workspace`. Useful in scripts or containers. |
| `STRATUM_DATA` | Override `paths.data`. |
| `STRATUM_CONFIG` | Override the config file path (default `~/.stratum/config.json`). |

---

## First-Time Setup

The installer handles this interactively. To reconfigure manually:

```bash
# View current config
cat ~/.stratum/config.json

# Edit
$EDITOR ~/.stratum/config.json

# Validate (stratum-brain will warn on missing required fields)
stratum-brain status
```

---

## Workspace Path

The `workspace` path must point to your OpenClaw workspace — the directory
containing `SOUL.md`, `AGENTS.md`, `MEMORY.md`, etc.

If you used the default OpenClaw setup, this is `~/clawd`. If you customized it,
update `paths.workspace` accordingly or set `$STRATUM_WORKSPACE`.

`stratum-lens` uses this path to discover and index workspace files. `stratum-mind`
uses it to find `MEMORY.md` for memory tier governance.
