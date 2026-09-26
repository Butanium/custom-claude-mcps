#!/usr/bin/env python3
"""member_status_report headlines: transcript decides working/idle, pane is secondary.

Builds a throwaway team + projects tree in a tempdir (inbox_state roots patched)
with a tmux member mid-tool-call, an in-process teammate that ended its turn,
a member with no live process, a member whose transcript doesn't exist yet, a
named (non-team) subagent, and a lead whose recorded session id is stale.

Run: cd mcp/team-inbox && uv run python tests/test_member_status_turn.py
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import inbox_state  # noqa: E402
import member_status  # noqa: E402
from inbox_state import _encode_cwd  # noqa: E402

FIX = HERE / "fixtures" / "turn_state"
TEAM = "turntest"
tmp = Path(tempfile.mkdtemp(prefix="turntest-"))
teams, projects = tmp / "teams", tmp / "projects"
inbox_state.TEAMS_ROOT, inbox_state.PROJECTS_ROOT = teams, projects
LEAD_CWD, WORK_CWD = str(tmp / "lead"), str(tmp / "work")
lead_proj, work_proj = projects / _encode_cwd(LEAD_CWD), projects / _encode_cwd(WORK_CWD)


def brief(text: str) -> dict:
    return {"type": "user", "isSidechain": False, "timestamp": "2026-09-26T00:00:00.000Z",
            "message": {"role": "user",
                        "content": f'<teammate-message teammate_id="team-lead">\n{text}\n</teammate-message>'}}


def transcript(path: Path, first: dict | None, fixture: str, sidechain: bool = False) -> None:
    rows = [json.loads(l) for l in (FIX / f"{fixture}.jsonl").read_text().splitlines()]
    if first is not None:
        rows.insert(0, first)
    for r in rows:
        if sidechain and "isSidechain" in r:
            r["isSidechain"] = True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def member(name: str, prompt: str, backend: str, pane: str, cwd: str) -> dict:
    return {"name": name, "model": "opus", "prompt": prompt, "backendType": backend,
            "tmuxPaneId": pane, "cwd": cwd}


(teams / TEAM / "inboxes").mkdir(parents=True)
(teams / TEAM / "config.json").write_text(json.dumps({
    "name": TEAM, "leadSessionId": "stale-sid",
    "members": [
        {"name": "team-lead", "backendType": "in-process", "tmuxPaneId": "leader", "cwd": LEAD_CWD},
        member("worker", "brief W", "tmux", "%99991", WORK_CWD),
        member("helper", "brief H", "in-process", "in-process", LEAD_CWD),
        member("ghost", "brief G", "tmux", "%99992", WORK_CWD),
        member("newbie", "brief N", "tmux", "%99993", WORK_CWD),
    ],
}))
transcript(lead_proj / "lead-sid.jsonl", None, "idle_end_turn")
transcript(work_proj / "w-sid.jsonl", brief("brief W"), "open_tool_use")
transcript(work_proj / "g-sid.jsonl", brief("brief G"), "idle_end_turn")
sub = lead_proj / "lead-sid" / "subagents"
transcript(sub / "agent-ahelper-1.jsonl", {**brief("brief H"), "isSidechain": True}, "idle_end_turn", sidechain=True)
(sub / "agent-ahelper-1.meta.json").write_text(json.dumps(
    {"name": "helper", "teamName": TEAM, "taskKind": "in_process_teammate", "agentType": "claude"}))
transcript(sub / "agent-ascout-2.jsonl", None, "open_tool_use", sidechain=True)
(sub / "agent-ascout-2.meta.json").write_text(json.dumps({"name": "scout", "agentType": "fork"}))

# worker and newbie have live processes (launched by lead-sid); ghost has none.
member_status._team_processes = lambda team: {
    n: {"pid": 1, "parent_session_id": "lead-sid", "tmux_socket": None, "tmux_pane": p}
    for n, p in (("worker", "%99991"), ("newbie", "%99993"))
}

try:
    report = member_status.member_status_report(TEAM)
    blocks = {b.split(" ", 1)[0]: b for b in report.split("\n\n")}
    scout = member_status.member_status_report(TEAM, "scout")
    missing = member_status.member_status_report(TEAM, "nobody")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

checks = [
    ("lead: stale leadSessionId, found via teammates' --parent-session-id",
     "status: idle — turn ended" in blocks["team-lead"] and "[source: transcript]" in blocks["team-lead"]),
    ("tmux member mid-tool-call: working, names the tool",
     "status: working, silent" in blocks["worker"] and "waiting on Bash" in blocks["worker"]),
    ("tmux member keeps a pane line", "  pane: gone" in blocks["worker"]),
    ("in-process teammate: transcript via subagents meta, idle",
     "status: idle — turn ended" in blocks["helper"] and "[source: transcript]" in blocks["helper"]),
    ("in-process teammate: no pane line, tagged", "pane:" not in blocks["helper"] and "in-process" in blocks["helper"]),
    ("no live process: not running", "status: not running" in blocks["ghost"]),
    ("no transcript yet: unknown, says why",
     "status: unknown [transcript not found; pane gone]" in blocks["newbie"]),
    ("named subagent resolved from lead's subagents dir",
     "subagent of team-lead" in scout and "status: working" in scout),
    ("unknown name errors", missing.startswith("Error: no member or named subagent")),
]
fails = 0
for name, ok in checks:
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}")
if fails:
    print("\n=== report ===\n" + report + "\n\n" + scout)
sys.exit(1 if fails else 0)
