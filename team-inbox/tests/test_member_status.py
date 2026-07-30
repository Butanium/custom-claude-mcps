#!/usr/bin/env python3
"""Regression test for member_status_report's two-direction pending-mail lines.

Fixture: reviewer replied, lead hasn't seen it (the 2026-07-30 incident state).
Expects the lead row to show its own pending inbox, and the reviewer row to
show "sent but not yet seen by recipient" — including when the report is
filtered to the reviewer only (the filtered view is exactly what a lead checks
right before a shutdown_request).

Run: cd mcp/team-inbox && uv run python tests/test_member_status.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
TEAM = "guardtest-tmp"
TEAM_DIR = HOME / ".claude" / "teams" / TEAM
WORK = Path(tempfile.mkdtemp(prefix="statustest-"))

sys.path.insert(0, str(Path(__file__).parent.parent))
from member_status import member_status_report  # noqa: E402

shutil.rmtree(TEAM_DIR, ignore_errors=True)
(TEAM_DIR / "inboxes").mkdir(parents=True)
(TEAM_DIR / "config.json").write_text(json.dumps({
    "name": TEAM,
    "leadSessionId": "00000000-dead-beef-0000-000000000000",
    "members": [
        {"name": "team-lead", "agentType": "team-lead", "cwd": str(WORK), "model": "fable"},
        {"name": "fable-reviewer", "agentType": "claude", "model": "fable",
         "cwd": str(WORK), "prompt": "review the thing", "tmuxPaneId": "%999"},
    ],
}))
(TEAM_DIR / "inboxes" / "team-lead.json").write_text(json.dumps([
    {"from": "fable-reviewer", "text": "Verified your fix commit. One real interaction bug.",
     "summary": "One real interaction bug; ready to shut down",
     "timestamp": "2026-07-30T18:07:39Z", "read": True},
]))
(TEAM_DIR / "inboxes" / "fable-reviewer.json").write_text("[]")

full = member_status_report(TEAM)
filtered = member_status_report(TEAM, member="fable-reviewer")

shutil.rmtree(TEAM_DIR, ignore_errors=True)
shutil.rmtree(WORK, ignore_errors=True)

checks = [
    ("lead row: undelivered-to-them",
     "undelivered messages to them: 1 (from fable-reviewer)" in full),
    ("reviewer row: sent-but-unseen",
     "sent but not yet seen by recipient: 1 → team-lead" in full),
    ("gist included", "One real interaction bug" in full),
    ("filtered view still shows sent-but-unseen",
     "sent but not yet seen by recipient: 1 → team-lead" in filtered),
]
fails = 0
for name, ok in checks:
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
if fails:
    print("\n=== full report for debugging ===\n" + full)
sys.exit(1 if fails else 0)
