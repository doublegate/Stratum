# Contributing to Stratum

Thanks for your interest. Stratum is a personal infrastructure project that's been
open-sourced as a reference and starting point — contributions are welcome but
the project evolves based on real operational needs, not roadmap votes.

---

## What's Useful

**Bug reports** — If something in the source is broken, wrong, or wouldn't work
as documented, open an issue. Include your OS, Rust/Python version, and what you
expected vs. what happened.

**Fixes** — Small, focused PRs that fix a real problem. No drive-by refactors.

**Improvements to docs** — Quickstart unclear? Configuration field undocumented?
Config example wrong? PRs welcome.

**Platform support** — Stratum runs on Linux. If you've made it work on macOS or
adapted `stratum-boot-health` for a different platform, that's worth sharing.

---

## What's Not

- Feature requests for things that don't align with the core use case
- Large refactors of working code
- Dependencies on paid APIs or external services (Stratum is local-only by design)
- Anything that requires always-on internet access to function

---

## Development Setup

```bash
git clone https://github.com/doublegate/Stratum
cd Stratum

# Rust modules
cd modules/stratum-mind
cargo build
cargo test
cargo clippy -- -D warnings

# Python modules
cd modules/stratum-brain
uv sync
uv run stratum-brain --help

# DB schemas
sqlite3 /tmp/test-mind.db < scripts/init-mind-db.sql
```

CI runs on every PR: `cargo check`, `cargo clippy`, `cargo test`, `cargo fmt --check`,
Python syntax validation, SQL schema validation, and JSON config validation.

---

## Pull Request Guidelines

1. Keep PRs small and focused — one fix or improvement per PR
2. Rust: code must pass `cargo fmt --check` and `cargo clippy -- -D warnings`
3. Python: no syntax errors (`python3 -m py_compile`)
4. If you're changing a module's behavior, update its `README.md`
5. If you're changing a path or config key, update `docs/configuration.md`

---

## Issues

Use GitHub Issues for bugs and questions. Be specific — "it doesn't work" isn't
actionable. Include:
- What you ran
- What you expected
- What actually happened (full error output)
- Your environment (OS, Rust version, Python version, OpenClaw version)

---

*Stratum is maintained as a spare-time project. Response times will vary.*
