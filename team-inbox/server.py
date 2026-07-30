"""MCP server that reads pending messages from Claude Code Agent-Teams inboxes."""

from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from inbox_state import (
    find_member_transcript,
    find_team_config,
    find_transcript,
    is_protocol_message,
    pending_from_paths,
    read_inbox,
)
from member_status import member_status_report

mcp = FastMCP("team-inbox")


def _format_entry(idx: int, m: dict) -> str:
    """Format one inbox entry for the agent-facing text block."""
    from_ = m.get("from", "?")
    ts = m.get("timestamp", "?")
    summary = m.get("summary")
    text = m.get("text", "")

    msg_type = "text"
    body = text
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict) and "type" in parsed:
            msg_type = parsed["type"]
            body = json.dumps(parsed, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass

    header = f"--- [{idx}] {from_} @ {ts} | type={msg_type} ---"
    if summary:
        header += f"\nsummary: {summary}"
    return f"{header}\n{body}"


@mcp.tool()
def fetch_unread(
    team_name: str,
    recipient: str = "team-lead",
    include_delivered: bool = False,
    include_protocol: bool = False,
    output_file: str | None = None,
) -> str:
    """Fetch pending teammate messages from a team inbox.

    Returns entries that have not yet appeared in the recipient's conversation
    transcript — messages the agent genuinely hasn't seen yet. Works mid-turn:
    if a teammate replies while the recipient is in a tool-call sequence, this
    surfaces that reply, even though the harness has already flipped
    `read: true` in the inbox file.

    The inbox file's `read` flag is ignored. The harness flips it at queue
    time, not delivery time (see anthropics/claude-code#58179), so it's an
    unreliable signal for "has the agent seen this." The recipient's JSONL
    transcript is parsed instead — a `<teammate-message>` block in the
    transcript is the only honest "delivered" signal.

    Args:
        team_name: Name of the team (directory under `~/.claude/teams/`).
        recipient: Inbox owner. Defaults to "team-lead". Non-lead recipients
            are also supported: their transcript is located by matching the
            spawn prompt (in team config) against the first
            `<teammate-message teammate_id="team-lead">` block in the JSONL
            files under their cwd's project dir.
        include_delivered: If True, return the full inbox (including already
            delivered entries). Defaults to False.
        include_protocol: If True, include structured protocol payloads
            (idle_notification, permission_request/response, shutdown_*,
            plan_approval_*). Defaults to False — these are handled by the
            harness/UI, not content the agent needs to read.
        output_file: If set, write the result there and return a short
            confirmation. Useful for large inboxes.

    Returns:
        Formatted text block, one entry per message, or "No undelivered
        messages." when the filtered set is empty.
    """
    config = find_team_config(team_name)
    if config is None:
        return f"Error: team config not found for '{team_name}'."

    if recipient == "team-lead":
        session_id = config.get("leadSessionId")
        if not session_id:
            return "Error: team config missing leadSessionId."
        transcript = find_transcript(session_id)
    else:
        transcript = find_member_transcript(team_name, recipient)
        if transcript is None:
            return (
                f"Error: could not locate transcript for non-lead recipient "
                f"'{recipient}'. Either the recipient is unknown, the team "
                f"config lacks the spawn-prompt field (older config shape), "
                f"or no JSONL in their cwd's project dir matches."
            )

    inbox = read_inbox(team_name, recipient)
    if include_delivered:
        selected = inbox if include_protocol else [m for m in inbox if not is_protocol_message(m)]
    else:
        selected = pending_from_paths(inbox, transcript, drop_protocol=not include_protocol)

    if not selected:
        return "No undelivered messages."

    formatted = "\n\n".join(_format_entry(i, m) for i, m in enumerate(selected, 1))

    if output_file is not None:
        out = Path(output_file)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(formatted)
        return (
            f"Written to {output_file} ({len(formatted)} chars, "
            f"{formatted.count(chr(10)) + 1} lines)"
        )

    return formatted


@mcp.tool()
def teammate_status(team_name: str, member: str | None = None) -> str:
    """Live status of team members: working/idle, background work, last activity.

    Answers "is this teammate stalled, finished, or waiting on a background
    job?" WITHOUT spawning a check-agent or pinging them. Use it when an
    idle notification arrives for a member whose task is still in_progress,
    or before assigning new work.

    Signals (all read-only): tmux pane title (working = mid-turn spinner /
    idle = at prompt), the member's in-flight background tasks from the
    inflight-tracker hook state (bash commands, agents, monitors, crons,
    with output-file start/last-write ages), transcript mtime (last
    activity of any kind), and undelivered inbox messages in BOTH directions:
    addressed to them, and sent by them but not yet seen by the recipient.
    The latter matters most before a shutdown_request — "idle" can mean
    "replied, and you haven't seen it yet", not "nothing left to say".

    Caveats: a member sleeping on a ScheduleWakeup timer shows as idle with
    no background work (wakeups live in-process only); pane state is a
    heuristic read of the title glyph.

    Args:
        team_name: Name of the team (directory under `~/.claude/teams/`).
        member: One member's name, or None for all members (lead included).

    Returns:
        One formatted block per member.
    """
    return member_status_report(team_name, member)


if __name__ == "__main__":
    mcp.run()
