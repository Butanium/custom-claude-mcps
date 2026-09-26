"""Live status of team members: working/idle, background work, last activity.

Answers the lead's recurring question "is this teammate stalled, finished, or
waiting on a background job?" without spawning a check-agent or pinging them.

Signals combined (all read-only, no teammate interaction):
- transcript turn state (turn_state.py): the last conversation row says
  whether the member's turn is open (a tool call awaiting its result, a
  message with no reply yet) or over (assistant row with a final stop_reason).
  This decides the headline working/idle whenever the transcript is found.
  Background work (Monitor, bg bash) doesn't keep a turn open, so it reads as
  "idle, background work in flight" instead of "working".
- tmux pane title glyph (tmux-backend members), secondary: a braille spinner
  frame while mid-turn, a leading sun mark (U+2733) at the prompt. Older
  Claude Code versions keep a spinner frame through background work. The
  pane is looked up on the tmux server the member process runs on (teammates
  live on a per-lead `claude-swarm-<pid>` socket, not the default server);
  headline source only when there is no transcript.
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
from collections import Counter
from pathlib import Path

import inbox_state
from inbox_state import (
    _encode_cwd,
    find_member_transcript,
    find_named_subagent_transcript,
    find_team_config,
    find_transcript,
    pending_from_paths,
    read_inbox,
)
from turn_state import TurnState, turn_state

STATE_ROOT = Path.home() / ".claude" / "state"
HOOKS_DIR = Path.home() / ".claude" / "hooks"

# Reuse the inflight tracker's reconcilers (same pattern as teammate_idle_nag).
sys.path.insert(0, str(HOOKS_DIR))
try:
    from inflight_tracker import gc_crons, sweep_state_from_transcript
except Exception:  # croniter may be absent in this venv; degrade gracefully
    sweep_state_from_transcript = None
    gc_crons = None

IDLE_MARK = "\u2733"  # Claude Code's idle-at-prompt title prefix
SILENT_AFTER_S = 1800  # open turn with no transcript row for this long: long tool call or hung


def _opt(args: list[str], flag: str) -> str | None:
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
        if a.startswith(flag + "="):
            return a[len(flag) + 1:]
    return None


def _team_processes(team_name: str) -> dict[str, dict] | None:
    """Live tmux-backend member processes of a team, keyed by agent name:
    {pid, parent_session_id, tmux_socket, tmux_pane}. Claude Code launches
    each with `--agent-name <n> --team-name <t> --parent-session-id <lead>`.
    None when /proc is unavailable (not Linux): liveness unknown."""
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    found: dict[str, dict] = {}
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            raw = (d / "cmdline").read_bytes()
        except OSError:
            continue
        if b"--team-name" not in raw:
            continue
        args = raw.decode(errors="replace").split("\0")
        name = _opt(args, "--agent-name")
        if _opt(args, "--team-name") != team_name or not name:
            continue
        env: dict[str, str] = {}
        try:
            for kv in (d / "environ").read_bytes().decode(errors="replace").split("\0"):
                k, _, v = kv.partition("=")
                if k in ("TMUX", "TMUX_PANE"):
                    env[k] = v
        except OSError:
            pass
        found[name] = {
            "pid": int(d.name),
            "parent_session_id": _opt(args, "--parent-session-id"),
            "tmux_socket": env.get("TMUX", "").split(",")[0] or None,
            "tmux_pane": env.get("TMUX_PANE"),
        }
    return found


def _pane_state(pane_id: str, socket: str | None = None) -> tuple[str, str]:
    """Return (state, title) for a tmux pane: working / idle / gone / unknown."""
    if not pane_id:
        return "unknown", ""
    cmd = ["tmux"] + (["-S", socket] if socket else [])
    try:
        out = subprocess.run(
            cmd + ["display-message", "-p", "-t", pane_id, "#{pane_id}\t#{pane_title}"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown", ""
    got_id, _, title = out.stdout.rstrip("\n").partition("\t")
    if out.returncode != 0 or got_id != pane_id:  # tmux prints nothing, rc 0, for a missing pane
        return "gone", ""
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


def _tool_list(names: list[str]) -> str:
    counts = Counter(names)
    return ", ".join(n if c == 1 else f"{n} x{c}" for n, c in counts.items())


def _status_line(
    ts: TurnState | None, pane: str, has_bg: bool, running: bool | None, now: float
) -> str:
    """Headline: working/idle from the transcript when there is one, else the pane."""
    if running is False:
        tail = ""
        if ts is not None and ts.ended is not None and ts.ts:
            tail = f"; transcript: turn {'ended' if ts.ended else 'left open'} {_age(now - ts.ts)} ago"
        return f"status: not running — no live process for this member{tail} [source: /proc]"
    if ts is not None and ts.ended is not None:
        quiet = now - ts.ts if ts.ts else 0.0
        if ts.ended:
            state = "idle, background work in flight" if has_bg else "idle"
            detail = f"turn ended {_age(quiet)} ago ({ts.reason})"
        else:
            what = f"waiting on {_tool_list(ts.pending_tools)}" if ts.pending_tools else "model generating"
            if quiet > SILENT_AFTER_S:
                state = f"working, silent {_age(quiet)} (long tool call or hung)"
                detail = what
            else:
                state = "working"
                detail = f"{what}, last row {_age(quiet)} ago"
        return f"status: {state} — {detail} [source: transcript]"
    why = "transcript not found" if ts is None else "no turn in transcript yet"
    if pane in ("working", "idle"):
        return f"status: {pane} [source: pane title; {why}]"
    return f"status: unknown [{why}; pane {pane}]"


def _lead_session_ids(config: dict, procs: dict[str, dict] | None) -> list[str]:
    """The lead's session id(s): the one recorded at team creation, plus the
    `--parent-session-id` live teammates were launched with. They differ once
    the lead resumes into a forked session; the team keeps its old name."""
    sids = [p.get("parent_session_id") for p in (procs or {}).values()]
    sids.append(config.get("leadSessionId"))
    return list(dict.fromkeys(s for s in sids if s))


def _lead_transcript(config: dict, sids: list[str]) -> Path | None:
    lead = next((m for m in config.get("members", []) if m.get("name") == "team-lead"), {})
    project_dir = inbox_state.PROJECTS_ROOT / _encode_cwd(lead.get("cwd", "")) if lead.get("cwd") else None
    found = []
    for sid in sids:
        t = project_dir / f"{sid}.jsonl" if project_dir else None
        if t is None or not t.exists():
            t = find_transcript(sid)
        if t is not None:
            found.append(t)
    return max(found, key=lambda t: t.stat().st_mtime) if found else None


def _subagent_report(config: dict, sids: list[str], name: str, now: float) -> str | None:
    """Status block for a subagent spawned by the lead with `name` (not a team member)."""
    lead = next((m for m in config.get("members", []) if m.get("name") == "team-lead"), {})
    if not lead.get("cwd"):
        return None
    hit = find_named_subagent_transcript(inbox_state.PROJECTS_ROOT / _encode_cwd(lead["cwd"]), sids, name)
    if hit is None:
        return None
    transcript, meta = hit
    lines = [
        f"{name} (subagent of team-lead, {meta.get('agentType', '?')}; not a team member)",
        "  " + _status_line(turn_state(transcript), "n/a", False, None, now),
        f"  last transcript write: {_age(now - transcript.stat().st_mtime)} ago",
    ]
    return "\n".join(lines)


def member_status_report(team_name: str, member: str | None = None) -> str:
    config = find_team_config(team_name)
    if config is None:
        return f"Error: team config not found for '{team_name}'."

    now = time.time()
    procs = _team_processes(team_name)
    lead_sids = _lead_session_ids(config, procs)

    members = config.get("members", [])
    display = members
    if member is not None:
        display = [m for m in members if m.get("name") == member]
        if not display:
            sub = _subagent_report(config, lead_sids, member, now)
            if sub is not None:
                return sub
            return f"Error: no member or named subagent '{member}' in team '{team_name}'."

    # Resolve transcripts + pending inboxes for the WHOLE team, even when a
    # member filter is set — a member's row also reports what they've sent
    # that nobody has seen yet, which lives in *other* members' inboxes.
    transcripts: dict[str, Path | None] = {}
    for m in members:
        n = m.get("name", "?")
        if n == "team-lead":
            transcripts[n] = _lead_transcript(config, lead_sids)
        else:
            transcripts[n] = find_member_transcript(team_name, n)
    pending_by_recipient = {
        n: pending_from_paths(read_inbox(team_name, n), t, drop_protocol=True)
        for n, t in transcripts.items()
    }

    blocks: list[str] = []
    for m in display:
        name = m.get("name", "?")
        is_lead = name == "team-lead"
        in_process = m.get("backendType") == "in-process" or m.get("tmuxPaneId") == "in-process"
        cwd = m.get("cwd", "")

        transcript = transcripts.get(name)
        subagent_format = transcript is not None and transcript.parent.name == "subagents"
        sid = transcript.stem if transcript is not None and not subagent_format else ""

        pane_state, title, running = "n/a", "", None
        if not is_lead and not in_process:
            proc = (procs or {}).get(name)
            if procs is not None and proc is None:
                running = False
            elif proc is not None:
                running = True
                pane_state, title = _pane_state(proc.get("tmux_pane") or m.get("tmuxPaneId", ""), proc.get("tmux_socket"))
            else:
                pane_state, title = _pane_state(m.get("tmuxPaneId", ""))

        work: list[str] = []
        if sid:
            work = _describe_inflight(_inflight(sid, transcript), cwd, sid)

        head = f"{name} ({m.get('model', '?')}{', in-process' if in_process and not is_lead else ''})"
        lines = [head, "  " + _status_line(turn_state(transcript), pane_state, bool(work), running, now)]
        if running:
            lines.append(f"  pane: {pane_state}" + (f' — "{title}"' if title else ""))
        if transcript is not None:
            lines.append(
                f"  last transcript write: {_age(now - transcript.stat().st_mtime)} ago"
            )
        else:
            lines.append("  last transcript write: transcript not found")

        if sid:
            if work:
                lines.append("  in-flight background work:")
                lines.extend(f"    - {w}" for w in work)
            else:
                lines.append("  in-flight background work: none tracked")
        elif subagent_format:
            lines.append("  in-flight background work: not tracked for in-process agents")

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
