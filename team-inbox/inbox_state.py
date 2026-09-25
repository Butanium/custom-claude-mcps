"""Shared logic for resolving inbox delivery state from the recipient's transcript.

Used by both the team-inbox MCP server (`fetch_unread`) and the
`pending_message_guard` PreToolUse hook on `SendMessage`. Stdlib only.

The recipient's JSONL transcript is the source of truth for "what has the
agent actually seen" — far more reliable than the inbox file's `read` flag,
which the harness flips at queue-for-delivery time rather than at actual
delivery time.
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

TEAMS_ROOT = Path.home() / ".claude" / "teams"
PROJECTS_ROOT = Path.home() / ".claude" / "projects"

TEAMMATE_MSG_PATTERN = re.compile(
    r'<teammate-message[^>]*teammate_id="([^"]+)"[^>]*>\n?(.*?)\n?</teammate-message>',
    re.DOTALL,
)


def find_team_config(team_name: str) -> dict | None:
    """Read a team's config.json, or None if missing/unreadable."""
    p = TEAMS_ROOT / team_name / "config.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return None


def find_team_by_lead_session(session_id: str) -> tuple[str | None, dict | None]:
    """Find the team where this session is the lead.

    Returns (team_name, config) on match, (None, None) otherwise.
    """
    if not TEAMS_ROOT.exists():
        return None, None
    for team_dir in TEAMS_ROOT.iterdir():
        if not team_dir.is_dir():
            continue
        cfg_path = team_dir / "config.json"
        if not cfg_path.exists():
            continue
        try:
            cfg = json.loads(cfg_path.read_text())
        except json.JSONDecodeError:
            continue
        if cfg.get("leadSessionId") == session_id:
            return team_dir.name, cfg
    return None, None


def find_transcript(session_id: str) -> Path | None:
    """Locate a session's JSONL transcript by globbing ~/.claude/projects/."""
    for p in PROJECTS_ROOT.rglob(f"{session_id}.jsonl"):
        return p
    return None


def _encode_cwd(cwd: str) -> str:
    """Encode an absolute cwd to the form used as a project dir name.

    Mirrors Claude Code: every non-alphanumeric UTF-16 code unit becomes `-`
    (`/home/user/.claude` -> `-home-user--claude`), and a result over 200
    chars is cut to 200 plus `-<base36 of |Java-style 32-bit hash of cwd|>`.
    """
    units = cwd.encode("utf-16-le")
    codes = [int.from_bytes(units[i:i + 2], "little") for i in range(0, len(units), 2)]
    slug = "".join(chr(c) if chr(c).isascii() and chr(c).isalnum() else "-" for c in codes)
    if len(slug) <= 200:
        return slug
    h = 0
    for c in codes:
        h = ((h << 5) - h + c) & 0xFFFFFFFF
    h = abs(h - (1 << 32) if h >= 1 << 31 else h)
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    b36 = ""
    while True:
        h, r = divmod(h, 36)
        b36 = digits[r] + b36
        if h == 0:
            break
    return f"{slug[:200]}-{b36}"


def _first_lead_message(transcript_path: Path, start: int = 0) -> str | None:
    """Inner text of the first `<teammate-message teammate_id="team-lead">` block.

    Scans ~50 lines of the JSONL from line `start` — the spawn prompt arrives
    near the top of every teammate's transcript, or right after the inherited
    history for a forked teammate.
    """
    try:
        with transcript_path.open() as f:
            for i, line in enumerate(f):
                if i < start:
                    continue
                if i > start + 50:
                    return None
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "user":
                    continue
                content = entry.get("message", {}).get("content")
                if not isinstance(content, str):
                    continue
                if not content.lstrip().startswith("<teammate-message"):
                    continue
                for m in TEAMMATE_MSG_PATTERN.finditer(content):
                    if m.group(1) == "team-lead":
                        return m.group(2)
    except OSError:
        return None
    return None


def _fork_teammate_transcript(team_name: str, member_name: str, prompt: str) -> Path | None:
    """Transcript of a teammate forked from the lead's context, via the sidecar
    the fork-teammate launcher writes (`teams/<team>/fork-teammates/<name>.json`).

    A fork's transcript opens with the lead's whole history, so its spawn prompt
    sits at line `forkLine`, not near the top. Matching the prompt there keeps a
    stale sidecar from shadowing a later non-fork teammate of the same name.
    """
    sidecar = TEAMS_ROOT / team_name / "fork-teammates" / f"{member_name}.json"
    try:
        info = json.loads(sidecar.read_text())
        transcript = Path(info["transcript"])
        start = int(info["forkLine"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    first = _first_lead_message(transcript, start=start)
    if first is not None and first.strip() == prompt:
        return transcript
    return None


def find_member_transcript(team_name: str, member_name: str) -> Path | None:
    """Locate a non-lead member's JSONL transcript via spawn-prompt match.

    The team config records each member's `cwd` and `prompt` (the spawn
    prompt the lead sent at TeamCreate/Agent time). That prompt arrives in
    the teammate's transcript as the first `<teammate-message
    teammate_id="team-lead">` block. We glob the project dir for the
    member's cwd and return the JSONL whose first such block matches.

    Returns None if the team config lacks the `prompt` field (older config
    shape) or no matching transcript is found.
    """
    config = find_team_config(team_name)
    if config is None:
        return None
    member = next(
        (m for m in config.get("members", []) if m.get("name") == member_name),
        None,
    )
    if member is None:
        return None
    cwd = member.get("cwd")
    prompt = member.get("prompt")
    if not cwd or not prompt:
        return None
    target = prompt.strip()
    fork = _fork_teammate_transcript(team_name, member_name, target)
    if fork is not None:
        return fork
    project_dir = PROJECTS_ROOT / _encode_cwd(cwd)
    if not project_dir.exists():
        return None
    for jsonl in project_dir.glob("*.jsonl"):
        first = _first_lead_message(jsonl)
        if first is not None and first.strip() == target:
            return jsonl
    return None


def delivered_by_sender(transcript_path: Path | None) -> dict[str, Counter]:
    """Parse a transcript JSONL for delivered <teammate-message> blocks.

    Returns a dict {sender_name: Counter(text -> count)}. Counts handle the
    case where the same teammate sends the same text twice.
    """
    result: dict[str, Counter] = defaultdict(Counter)
    if not transcript_path or not transcript_path.exists():
        return result
    with transcript_path.open() as f:
        for line in f:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") != "user":
                continue
            content = entry.get("message", {}).get("content")
            if not isinstance(content, str):
                continue
            if not content.lstrip().startswith("<teammate-message"):
                continue
            for m in TEAMMATE_MSG_PATTERN.finditer(content):
                sender, text = m.group(1), m.group(2)
                result[sender][text] += 1
    return result


def inbox_path(team_name: str, recipient: str) -> Path:
    return TEAMS_ROOT / team_name / "inboxes" / f"{recipient}.json"


def read_inbox(team_name: str, recipient: str) -> list[dict]:
    p = inbox_path(team_name, recipient)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return []


def is_protocol_message(m: dict) -> bool:
    """True if entry text is a structured protocol payload (JSON dict with `type`).

    Covers harness-emitted notifications (idle_notification, permission_request/
    response) and inter-agent control payloads (shutdown_request/response/
    approved, plan_approval_request/response). These are handled by the harness
    or via pane-level UI affordances, not by the agent reading content — so
    they should not gate the pending-message guard or surface in fetch_unread.
    """
    text = m.get("text", "")
    if not isinstance(text, str):
        return False
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(parsed, dict) and "type" in parsed


def pending_from_paths(
    inbox: list[dict],
    transcript_path: Path | None,
    sender_filter: str | None = None,
    drop_protocol: bool = False,
) -> list[dict]:
    """Inbox entries not yet appearing in `transcript_path`.

    Args:
        inbox: Inbox entries list (already loaded — caller decides where from).
        transcript_path: Path to the recipient's JSONL transcript, or None.
        sender_filter: If set, only return entries whose `from` matches.
            Filtering happens after the delivered-counter is consumed, so
            duplicates from other senders are still accounted for correctly.
        drop_protocol: If True, skip structured protocol payloads (see
            `is_protocol_message`) — they are not user-readable content.
    """
    delivered = delivered_by_sender(transcript_path)
    counters = {s: Counter(c) for s, c in delivered.items()}

    pending: list[dict] = []
    for m in inbox:
        sender = m.get("from", "")
        text = m.get("text", "")
        c = counters.setdefault(sender, Counter())
        if c[text] > 0:
            c[text] -= 1
            continue
        if sender_filter is not None and sender != sender_filter:
            continue
        if drop_protocol and is_protocol_message(m):
            continue
        pending.append(m)
    return pending


def pending_messages(
    team_name: str,
    recipient: str,
    recipient_session_id: str,
    sender_filter: str | None = None,
    drop_protocol: bool = False,
) -> list[dict]:
    """Inbox entries for `recipient` that are not yet in their transcript.

    Convenience wrapper that resolves inbox + transcript from team_name and
    sessionId. For the hook path (which gets `transcript_path` directly from
    its input), prefer `pending_from_paths`.
    """
    return pending_from_paths(
        inbox=read_inbox(team_name, recipient),
        transcript_path=find_transcript(recipient_session_id),
        sender_filter=sender_filter,
        drop_protocol=drop_protocol,
    )
