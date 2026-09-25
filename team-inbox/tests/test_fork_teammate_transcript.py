#!/usr/bin/env python3
"""find_member_transcript locates a forked teammate through its launcher sidecar.

A fork's transcript opens with the lead's inherited history, so the spawn
prompt sits past the 50-line head scan. The sidecar
(`teams/<team>/fork-teammates/<name>.json`) points at the transcript and the
line where the fork's own messages start. A sidecar whose transcript does not
carry the member's current spawn prompt at that line must be ignored.

Run: cd mcp/team-inbox && uv run python tests/test_fork_teammate_transcript.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

HOME = Path.home()
TEAM = "forktest-tmp"
TEAM_DIR = HOME / ".claude" / "teams" / TEAM
WORK = Path(tempfile.mkdtemp(prefix="forktest-"))

sys.path.insert(0, str(Path(__file__).parent.parent))
from inbox_state import find_member_transcript  # noqa: E402

PROMPT = "summarize the canary"
INHERITED = 120

transcript = WORK / "fork-session.jsonl"
with transcript.open("w") as f:
    for i in range(INHERITED):
        f.write(json.dumps({"type": "user", "message": {"role": "user", "content": f"lead turn {i}"}}) + "\n")
    f.write(json.dumps({"type": "user", "message": {"role": "user", "content":
        f'<teammate-message teammate_id="team-lead">\n{PROMPT}\n</teammate-message>'}}) + "\n")


def setup(member_prompt: str) -> None:
    shutil.rmtree(TEAM_DIR, ignore_errors=True)
    (TEAM_DIR / "fork-teammates").mkdir(parents=True)
    (TEAM_DIR / "config.json").write_text(json.dumps({
        "name": TEAM,
        "members": [
            {"name": "team-lead", "agentType": "team-lead", "cwd": str(WORK)},
            {"name": "forky", "cwd": str(WORK), "prompt": member_prompt, "tmuxPaneId": "%999"},
        ],
    }))
    (TEAM_DIR / "fork-teammates" / "forky.json").write_text(json.dumps({
        "sessionId": "fork-session", "transcript": str(transcript), "forkLine": INHERITED,
    }))


try:
    setup(PROMPT)
    found = find_member_transcript(TEAM, "forky")
    setup("a later, unrelated spawn prompt")
    stale = find_member_transcript(TEAM, "forky")
finally:
    shutil.rmtree(TEAM_DIR, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)

checks = [
    ("sidecar resolves the fork transcript", found == transcript),
    ("stale sidecar is ignored", stale is None),
]
fails = 0
for name, ok in checks:
    print(("PASS " if ok else "FAIL ") + name)
    fails += not ok
sys.exit(1 if fails else 0)
