"""Is a Claude Code session mid-turn or back at its prompt, read from its transcript.

The deciding row is the last user/assistant row that is part of the model
conversation. The turn is over when that row is an assistant message whose
stop_reason ends the turn (end_turn, stop_sequence, max_tokens, refusal, ...),
an API-error row, or a user "[Request interrupted by user" row. Anything else
(an assistant tool_use, a tool_result or a fresh message with no reply yet) is
an open turn. Same rule as itls (`itlslib/transcript.py` `_closes_turn`), plus
the rows it has to step over here:

- bookkeeping rows (system, attachment incl. queued_command, queue-operation,
  titles, snapshots): mid-turn deliveries and metadata, never a boundary;
- isMeta user rows (skill bodies, caveats) and compaction summaries: after a
  compaction the row before the boundary still says whether a turn was open;
- local command / bash-mode rows (`<command-name>`, `<local-command-stdout>`,
  `<bash-input>` ...): typed at the prompt, no model reply follows;
- sidechain rows in a main transcript. A subagent / in-process teammate
  transcript (`<session>/subagents/agent-*.jsonl`) is all sidechain rows, so
  they count there.

Stdlib only.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

OPEN_STOP_REASONS = (None, "tool_use", "pause_turn")
LOCAL_COMMAND_PREFIXES = (
    "<command-name>", "<command-message>", "<command-args>",
    "<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>",
    "<bash-input>", "<bash-stdout>", "<bash-stderr>",
)
INTERRUPT_PREFIX = "[Request interrupted by user"
FIRST_CHUNK = 256 * 1024


@dataclass
class TurnState:
    ended: bool | None  # None: no conversation row yet (fresh session)
    reason: str  # closed: the stop_reason, api_error or interrupted; open: tool_use or generating; no_rows
    ts: float | None  # timestamp of the deciding row
    pending_tools: list[str] = field(default_factory=list)  # tool_use names without a result yet


def _parse_ts(s) -> float | None:
    if not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _blocks(row: dict) -> list:
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [b for b in content or [] if isinstance(b, dict)]


def _user_text(row: dict) -> str:
    return " ".join(b.get("text") or "" for b in _blocks(row) if b.get("type") == "text").lstrip()


def _is_conversation_row(row: dict, subagent_file: bool) -> bool:
    if row.get("type") not in ("user", "assistant"):
        return False
    if row.get("isSidechain") and not subagent_file:
        return False
    if row.get("isMeta") or row.get("isCompactSummary"):
        return False
    if row.get("type") == "user":
        if any(b.get("type") == "tool_result" for b in _blocks(row)):
            return True
        text = _user_text(row)
        if text.startswith(LOCAL_COMMAND_PREFIXES):
            return False
    return True


def _closes_turn(row: dict) -> str | None:
    """Why this conversation row ends the turn, or None if the turn stays open."""
    if row.get("type") == "assistant":
        if row.get("isApiErrorMessage"):
            return "api_error"
        stop = (row.get("message") or {}).get("stop_reason")
        return None if stop in OPEN_STOP_REASONS else stop
    if _user_text(row).startswith(INTERRUPT_PREFIX):
        return "interrupted"
    return None


def _rows(chunk: bytes):
    for raw in chunk.split(b"\n"):
        if not raw:
            continue
        try:
            row = json.loads(raw)
        except ValueError:
            continue
        if isinstance(row, dict):
            yield row


def _conversation_tail(path: Path, subagent_file: bool) -> list[dict]:
    """Conversation rows of the smallest file tail that contains at least one."""
    size = path.stat().st_size
    want = FIRST_CHUNK
    with open(path, "rb") as f:
        while True:
            start = max(0, size - want)
            f.seek(start)
            chunk = f.read(size - start)
            if start > 0:
                chunk = chunk[chunk.find(b"\n") + 1:]
            rows = [r for r in _rows(chunk) if _is_conversation_row(r, subagent_file)]
            if rows or start == 0:
                return rows
            want *= 4


def turn_state(path: Path | None) -> TurnState | None:
    """TurnState of the transcript at `path`, or None if it doesn't exist."""
    if path is None:
        return None
    subagent_file = path.parent.name == "subagents"
    try:
        rows = _conversation_tail(path, subagent_file)
    except OSError:
        return None
    if not rows:
        return TurnState(ended=None, reason="no_rows", ts=None)

    pending: dict[str, str] = {}
    for row in rows:
        if _closes_turn(row):
            pending.clear()
            continue
        for b in _blocks(row):
            if b.get("type") == "tool_use":
                pending[b.get("id", "")] = b.get("name", "?")
            elif b.get("type") == "tool_result":
                pending.pop(b.get("tool_use_id", ""), None)

    last = rows[-1]
    ts = _parse_ts(last.get("timestamp"))
    closed = _closes_turn(last)
    if closed:
        return TurnState(ended=True, reason=closed, ts=ts)
    tools = list(pending.values())
    return TurnState(ended=False, reason="tool_use" if tools else "generating", ts=ts, pending_tools=tools)
