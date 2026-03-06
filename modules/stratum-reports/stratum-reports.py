import os
#!/usr/bin/env python3
"""
stratum-reports — Stratum unified report pipeline CLI
Consolidates: clawd-report-runbook, deep-report-validator, clawd-report-ingest, report-timeline

Usage:
    stratum-reports runbook [args...]     — report pipeline orchestration
    stratum-reports validate [args...]    — post-run quality gate
    stratum-reports ingest [args...]      — extract insights from reports
    stratum-reports timeline [args...]    — report timeline tracker
    stratum-reports status                — dashboard for all report tools
"""
import argparse, subprocess, sys
from pathlib import Path

HOME = Path.home()
BINS = {
    "runbook":  HOME / ".local/bin/clawd-report-runbook",
    "validate": HOME / ".local/bin/stratum-reports",  # self-validates
    "ingest":   HOME / ".local/bin/clawd-report-ingest",
    "timeline": HOME / ".local/bin/report-timeline",
}


def delegate(name: str, extra_args: list[str]) -> int:
    bin_path = BINS[name]
    if not bin_path.exists():
        # Try report-timeline as report_timeline etc.
        alt = HOME / f".local/bin/{name}"
        if alt.exists():
            bin_path = alt
        else:
            print(f"{name} not found at {bin_path}", file=sys.stderr)
            return 1
    r = subprocess.run([str(bin_path)] + extra_args)
    return r.returncode


def cmd_status() -> int:
    print("=== stratum-reports status ===")
    reports_dir = Path(os.environ.get("STRATUM_WORKSPACE", str(HOME / "clawd"))) / "reports/markdown"
    if reports_dir.exists():
        reports = sorted(reports_dir.glob("*.md"))
        print(f"Reports: {len(reports)} markdown files in {reports_dir}")
        if reports:
            print(f"  Latest: {reports[-1].name}")
    else:
        print("Reports dir not found")

    # Ingest status
    r = subprocess.run([str(BINS["ingest"]), "--status"],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        print(r.stdout.strip())

    # Timeline status
    r = subprocess.run([str(BINS["timeline"]), "status"],
                       capture_output=True, text=True)
    if r.returncode == 0 and r.stdout.strip():
        for line in r.stdout.strip().splitlines()[:5]:
            print(line)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Stratum unified report pipeline")
    sub = p.add_subparsers(dest="cmd", required=True)

    for name, help_text in [
        ("runbook",  "Report pipeline orchestration"),
        ("validate", "Post-run quality gate"),
        ("ingest",   "Extract insights from reports → research + LEARNING.md"),
        ("timeline", "Report timeline tracker"),
    ]:
        sp = sub.add_parser(name, help=help_text, add_help=False)
        sp.add_argument("args", nargs=argparse.REMAINDER)

    sub.add_parser("status", help="Dashboard for all report tools")

    args = p.parse_args()
    if args.cmd == "status":
        return cmd_status()
    return delegate(args.cmd, getattr(args, "args", []))


if __name__ == "__main__":
    raise SystemExit(main())
