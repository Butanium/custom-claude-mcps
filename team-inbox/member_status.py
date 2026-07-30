"""Live status of team members: working/idle, background work, last activity.

Answers the lead's recurring question "is this teammate stalled, finished, or
waiting on a background job?" without spawning a check-agent or pinging them.

Signals combined (all read-only, no teammate interaction):
- tmux pane title glyph (tmux-backend members): Claude Code sets the pane
  title to a braille spinner frame while mid-turn and a leading sun mark
  (U+2733) while idle at the prompt. Heuristic, but sampled live.
- ~/.claude/state/<session>/inflight.json: background bash/agents/monitors/
  crons, maintained by the inflight_tracker.py PostToolUse hook. Reconciled
  through the same sweep the TeammateIdle nag uses, so stale entries drop.
- /tmp/claude-<uid>/<project>/<session>/tasks/<id>.output: per-task output
  files; ctime ~ start, mtime = last write (a growing file = live work).
- transcript JSONL mtime: last activity of any kind.
- undelivered inbox messages TO the member (has my brief even landed?) and
  FROM the member (have they said something nobody has seen yet?). The FROM
  direction exists because "idle" reads as "nothing to say" when it can mean
  "said something that hasn't reached you" — a lead once shutdown-requested
  a reviewer whose findings sat undelivered in the lead's own inbox.

Known blind spot: ScheduleWakeup timers are in-process only — a member
sleeping on a wakeup shows as idle with no inflight work.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from inbox_state import (
    PROJECTS_ROOT,
    _encode_cwd,
    find_member_transcript,
    find_team_config,
    find_transcript,
    pending_from_paths,
    read_inbox,
)

STATE_ROOT = Path.home() / ".claude" / "state"
HOOKS_DIR = Path.home() / ".claude" / "hooks"

# Reuse the inflight tracker's reconcilers (same pattern as teammate_idle_nag).
sys.path.insert(0, str(HOOKS_DIR))
try:
    from inflight_tracker import gc_crons, sweep_state_from_transcript
except Exception:  # croniter may be absent in this venv; degrade gracefully
    sweep_state_from_transcript = None
    gc_crons = None

IDLE_MARK = "✳"  # ✳ — Claude Code's idle-at-prompt title prefix


def _pane_state(pane_id: str) -> tuple[str, str]:
    """Return (state, title) for a tmux pane: working / idle / gone / unknown."""
    if not pane_id:
        return "unknown", ""
    try:
        out = subprocess.run(
            ["tmux", "display-message", "-p", "-t", pane_id, "#{pane_title}"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown", ""
    if out.returncode != 0:
        return "gone", ""
    title = out.stdout.strip()
    first = title[:1]
    if first == IDLE_MARK:
        return "idle", title
    if first and 0x2800 <= ord(first) <= 0x28FF:  # braille spinner frame
        return "working", title
    return "unknown", title


def _age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _inflight(session_id: str, transcript: Path | None) -> dict:
    p = STATE_ROOT / session_id / "inflight.json"
    if not p.exists():
        return {}
    try:
        state = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}
    if sweep_state_from_transcript and transcript is not None:
        sweep_state_from_transcript(state, str(transcript))
    if gc_crons:
        gc_crons(state)
    return state


def _task_file_info(cwd: str, session_id: str, task_id: str) -> str:
    """Start/last-write ages for a background bash task's output file."""
    tasks_dir = Path(f"/tmp/claude-{os.getuid()}") / _encode_cwd(cwd) / session_id / "tasks"
    f = tasks_dir / f"{task_id}.output"
    if not f.exists():
        return ""
    st = f.stat()
    now = time.time()
    return (
        f" [started ~{_age(now - st.st_ctime)} ago, "
        f"output {st.st_size}B, last write {_age(now - st.st_mtime)} ago]"
    )


def _describe_inflight(state: dict, cwd: str, session_id: str) -> list[str]:
    lines: list[str] = []
    for e in state.get("bash_bg", []):
        cmd = e.get("command", "?")
        lines.append(
            f"bg bash {e.get('id', '?')}: {cmd}"
            + _task_file_info(cwd, session_id, e.get("id", ""))
        )
    for e in state.get("agents", []):
        lines.append(f"bg agent {e.get('id', '?')}: {e.get('description', '?')}")
    for e in state.get("monitor", []):
        lines.append(f"monitor {e.get('id', '?')}: {e.get('description', '?')}")
    for e in state.get("crons", []):
        kind = "recurring" if e.get("recurring") else "one-shot"
        lines.append(f"cron {e.get('id', '?')} ({kind}): {e.get('cron', '?')}")
    return lines


def member_status_report(team_name: str, member: str | None = None) -> str:
    config = find_team_config(team_name)
    if config is None:
        return f"Error: team config not found for '{team_name}'."

    members = config.get("members", [])
    display = members
    if member is not None:
        display = [m for m in members if m.get("name") == member]
        if not display:
            return f"Error: no member named '{member}' in team '{team_name}'."

    # Resolve transcripts + pending inboxes for the WHOLE team, even when a
    # member filter is set — a member's row also reports what they've sent
    # that nobody has seen yet, which lives in *other* members' inboxes.
    transcripts: dict[str, Path | None] = {}
    for m in members:
        n = m.get("name", "?")
        if n == "team-lead":
            lead_sid = config.get("leadSessionId", "")
            transcripts[n] = find_transcript(lead_sid) if lead_sid else None
        else:
            transcripts[n] = find_member_transcript(team_name, n)
    pending_by_recipient = {
        n: pending_from_paths(read_inbox(team_name, n), t, drop_protocol=True)
        for n, t in transcripts.items()
    }

    now = time.time()
    blocks: list[str] = []
    for m in display:
        name = m.get("name", "?")
        is_lead = name == "team-lead"
        cwd = m.get("cwd", "")

        transcript = transcripts.get(name)
        if is_lead:
            sid = config.get("leadSessionId", "")
        else:
            sid = transcript.stem if transcript is not None else ""

        pane_state, title = _pane_state(m.get("tmuxPaneId", "")) if not is_lead else ("n/a", "")

        head = f"{name} ({m.get('model', '?')})"
        lines = [head]
        if not is_lead:
            lines.append(
                f"  pane: {pane_state}" + (f' — "{title}"' if title else "")
            )
        if transcript is not None:
            lines.append(
                f"  last transcript write: {_age(now - transcript.stat().st_mtime)} ago"
            )
        else:
            lines.append("  last transcript write: transcript not found")

        if sid:
            work = _describe_inflight(_inflight(sid, transcript), cwd, sid)
            if work:
                lines.append("  in-flight background work:")
                lines.extend(f"    - {w}" for w in work)
            else:
                lines.append("  in-flight background work: none tracked")

        undelivered = pending_by_recipient.get(name, [])
        if undelivered:
            senders = ", ".join(sorted({u.get("from", "?") for u in undelivered}))
            lines.append(
                f"  undelivered messages to them: {len(undelivered)} (from {senders})"
            )

        unheard = [
            (rcpt, u)
            for rcpt, plist in pending_by_recipient.items()
            if rcpt != name
            for u in plist
            if u.get("from") == name
        ]
        if unheard:
            per_rcpt: dict[str, int] = {}
            for rcpt, _ in unheard:
                per_rcpt[rcpt] = per_rcpt.get(rcpt, 0) + 1
            detail = ", ".join(f"{n} → {r}" for r, n in sorted(per_rcpt.items()))
            line = f"  sent but not yet seen by recipient: {detail}"
            if len(unheard) == 1:
                u = unheard[0][1]
                gist = (u.get("summary") or u.get("text", "").replace("\n", " "))[:90]
                line += f' ("{gist}")'
            lines.append(line)

        blocks.append("\n".join(lines))

    return "\n\n".join(blocks) if blocks else "No members found."
