#!/usr/bin/env python3
"""Regression tests for hooks/pending_message_guard.py (shutdown_request coverage).

Recreates the 2026-07-30 tinkerscope incident state: a reviewer's reply is
sitting undelivered in the lead's inbox while the lead sends a dict-form
shutdown_request to that same reviewer. Runs the hook exactly as wired
(uv run --project ~/.claude/hooks, JSON on stdin) and checks deny/pass.

Writes a throwaway team to the real ~/.claude/teams/ (the hook resolves
TEAMS_ROOT from $HOME; fine on a personal box) and cleans it up.

Run: python3 tests/test_pending_message_guard.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
TEAM = "guardtest-tmp"
TEAM_DIR = HOME / ".claude" / "teams" / TEAM
WORK = Path(tempfile.mkdtemp(prefix="guardtest-"))
HOOK = HOME / ".claude" / "hooks" / "pending_message_guard.py"

REVIEWER_REPLY = (
    "Verified your fix commit independently before this reply. One real "
    "interaction bug: packSeen permanently swallows a cancelled pack link."
)


def setup(delivered: bool) -> Path:
    shutil.rmtree(TEAM_DIR, ignore_errors=True)
    (TEAM_DIR / "inboxes").mkdir(parents=True)
    (TEAM_DIR / "config.json").write_text(json.dumps({
        "name": TEAM,
        "leadAgentId": f"team-lead@{TEAM}",
        "leadSessionId": "00000000-dead-beef-0000-000000000000",
        "members": [
            {"name": "team-lead", "agentType": "team-lead", "cwd": str(WORK)},
            {"name": "fable-reviewer", "agentType": "claude", "model": "fable",
             "cwd": str(WORK), "prompt": "review the thing", "tmuxPaneId": "%999"},
        ],
    }))
    (TEAM_DIR / "inboxes" / "team-lead.json").write_text(json.dumps([
        {"from": "fable-reviewer", "text": REVIEWER_REPLY,
         "summary": "One real interaction bug; ready to shut down",
         "timestamp": "2026-07-30T18:07:39Z", "read": True},
    ]))
    (TEAM_DIR / "inboxes" / "fable-reviewer.json").write_text("[]")

    transcript = WORK / "fake-lead-transcript.jsonl"
    entries = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "working"}]}}]
    if delivered:
        entries.append({"type": "user", "message": {"content":
            f'<teammate-message teammate_id="fable-reviewer">\n{REVIEWER_REPLY}\n</teammate-message>'}})
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    return transcript


def run_hook(tool_input: dict, transcript: Path) -> tuple[bool, str]:
    """Returns (denied, reason)."""
    data = {
        "tool_name": "SendMessage",
        "tool_input": tool_input,
        "agent_id": f"team-lead@{TEAM}",
        "session_id": "00000000-dead-beef-0000-000000000000",
        "transcript_path": str(transcript),
    }
    r = subprocess.run(
        ["uv", "run", "--project", str(HOME / ".claude" / "hooks"), str(HOOK)],
        input=json.dumps(data), capture_output=True, text=True,
        env={"PATH": f"{HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin", "HOME": str(HOME)},
    )
    if r.returncode != 0:
        print(f"  HOOK CRASHED: {r.stderr}")
        sys.exit(1)
    if not r.stdout.strip():
        return False, ""
    out = json.loads(r.stdout)
    h = out.get("hookSpecificOutput", {})
    return h.get("permissionDecision") == "deny", h.get("permissionDecisionReason", "")


SHUTDOWN_REQ = {"to": "fable-reviewer",
                "message": {"type": "shutdown_request", "reason": "Session wrapping up."}}

CASES = [
    # (name, tool_input, reply_already_delivered, expect_deny)
    ("shutdown_request w/ pending reply from target", SHUTDOWN_REQ, False, True),
    ("shutdown_request w/ [ACK-PENDING] in reason",
     {"to": "fable-reviewer", "message": {"type": "shutdown_request",
      "reason": "[ACK-PENDING] saw your reply, closing."}}, False, False),
    ("shutdown_response (reply frame, must pass)",
     {"to": "team-lead", "message": {"type": "shutdown_response",
      "requestId": "x", "approve": True}}, False, False),
    ("string message w/ pending reply from target",
     {"to": "fable-reviewer", "summary": "ping", "message": "did you see my note?"},
     False, True),
    ("string message to member with nothing pending",
     {"to": "other-worker", "summary": "hi", "message": "status?"}, False, False),
    ("shutdown_request after reply was DELIVERED", SHUTDOWN_REQ, True, False),
]

failures = 0
for name, tool_input, delivered, expect_deny in CASES:
    transcript = setup(delivered=delivered)
    denied, reason = run_hook(tool_input, transcript)
    ok = denied == expect_deny
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}: denied={denied} (expected {expect_deny})")

shutil.rmtree(TEAM_DIR, ignore_errors=True)
shutil.rmtree(WORK, ignore_errors=True)
sys.exit(1 if failures else 0)
