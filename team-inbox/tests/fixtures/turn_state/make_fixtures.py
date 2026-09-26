"""Regenerate the turn_state fixture transcripts (row shapes as Claude Code 2.1.280 writes them).

Run: cd mcp/team-inbox && python3 tests/fixtures/turn_state/make_fixtures.py
"""
import itertools
import json
from pathlib import Path

HERE = Path(__file__).parent
_clock = itertools.count()


def _ts() -> str:
    s = next(_clock)
    return f"2026-09-26T01:{s // 60:02d}:{s % 60:02d}.000Z"


def u(text, **kw):
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": text},
            "timestamp": _ts(), **kw}


def res(tid, text="ok", is_error=False, **kw):
    block = {"type": "tool_result", "tool_use_id": tid, "content": text, "is_error": is_error}
    return {"type": "user", "isSidechain": False, "message": {"role": "user", "content": [block]},
            "timestamp": _ts(), **kw}


def a(stop, *blocks, **kw):
    content = []
    for b in blocks:
        if isinstance(b, tuple):
            content.append({"type": "tool_use", "id": b[0], "name": b[1], "input": {}})
        elif b == "thinking":
            content.append({"type": "thinking", "thinking": "..."})
        else:
            content.append({"type": "text", "text": b})
    return {"type": "assistant", "isSidechain": False,
            "message": {"role": "assistant", "stop_reason": stop, "content": content},
            "timestamp": _ts(), **kw}


def sysrow(subtype):
    return {"type": "system", "subtype": subtype, "isSidechain": False, "timestamp": _ts()}


def att(kind, **extra):
    return {"type": "attachment", "isSidechain": False, "attachment": {"type": kind, **extra},
            "timestamp": _ts()}


BOOK = [{"type": "last-prompt", "lastPrompt": "x"}, {"type": "ai-title", "aiTitle": "x"}]
TEAMMATE_MSG = '<teammate-message teammate_id="team-lead">\nnext task\n</teammate-message>'

FIXTURES = {
    "idle_end_turn": [
        u("do the thing"), a("tool_use", ("t1", "Bash")), res("t1"), att("total_tokens_reminder"),
        a("end_turn", "done"), sysrow("stop_hook_summary"), sysrow("turn_duration"), *BOOK],
    "open_tool_use": [
        u("do the thing"), a("tool_use", "thinking"), a("tool_use", ("t1", "Bash")), *BOOK],
    "parallel_tools_one_result": [
        u("go"), a("tool_use", ("t1", "Read")), a("tool_use", ("t2", "Bash")), res("t1"),
        att("total_tokens_reminder")],
    "tool_result_generating": [
        u("go"), a("tool_use", ("t1", "Read")), res("t1"), a(None, "thinking")],
    "message_no_reply": [
        u("go"), a("end_turn", "done"), u(TEAMMATE_MSG)],
    "queued_command_after_end": [
        u("go"), a("end_turn", "done"), sysrow("turn_duration"),
        {"type": "queue-operation", "operation": "enqueue", "content": "hi"},
        att("queued_command", prompt="hi", commandMode="prompt")],
    "queued_command_mid_turn": [
        u("go"), a("tool_use", ("t1", "Monitor")),
        att("queued_command", prompt="<task-notification>x</task-notification>",
            commandMode="task-notification")],
    "compacted_idle": [
        u("go"), a("end_turn", "done"), sysrow("compact_boundary"),
        u("This session is being continued from a previous conversation...", isCompactSummary=True),
        att("task_status"), att("hook_success")],
    "compacted_mid_turn": [
        u("go"), a("tool_use", ("t1", "Bash")), res("t1"), sysrow("compact_boundary"),
        u("This session is being continued from a previous conversation...", isCompactSummary=True),
        att("file")],
    "compacted_then_resumed": [
        u("go"), a("tool_use", ("t1", "Bash")), res("t1"), sysrow("compact_boundary"),
        u("This session is being continued...", isCompactSummary=True),
        a("end_turn", "picked up where I left off")],
    "legacy_summary_rows": [
        {"type": "summary", "summary": "old session", "leafUuid": "x"},
        u("go"), a("tool_use", ("t1", "Grep"))],
    "local_command_after_idle": [
        u("go"), a("end_turn", "done"),
        u("<local-command-caveat>Caveat: ...</local-command-caveat>", isMeta=True),
        u("<command-name>/model</command-name>\n<command-message>model</command-message>\n"
          "<command-args></command-args>"),
        u("<local-command-stdout>Set model to Opus</local-command-stdout>"),
        u("<bash-input>ls</bash-input>"),
        u("<bash-stdout>a b</bash-stdout><bash-stderr></bash-stderr>")],
    "meta_row_mid_turn": [
        u("go"), a("tool_use", ("t1", "Skill")), res("t1", "Launching skill"),
        u("Base directory for this skill: ...", isMeta=True)],
    "interrupted": [
        u("go"), a("tool_use", ("t1", "Bash")), res("t1", "interrupted", is_error=True),
        u("[Request interrupted by user for tool use]")],
    "api_error": [
        u("go"), sysrow("model_refusal_no_fallback"),
        a("refusal", "API Error: flagged", isApiErrorMessage=True), sysrow("turn_duration")],
    "sidechain_rows_in_main": [
        u("go"), a("tool_use", ("t1", "Agent")),
        u("subagent prompt", isSidechain=True), a("end_turn", "subagent done", isSidechain=True)],
    "bookkeeping_only": [
        *BOOK, {"type": "permission-mode", "permissionMode": "default"}],
}

# A subagent / in-process teammate transcript: every row is a sidechain row.
SUBAGENT = [
    {"type": "fork-context-ref", "agentId": "ax", "parentSessionId": "p"},
    u(TEAMMATE_MSG, isSidechain=True),
    a("tool_use", ("t1", "Bash"), isSidechain=True), res("t1", isSidechain=True),
    a("end_turn", "done", isSidechain=True)]


def _write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


if __name__ == "__main__":
    for name, rows in FIXTURES.items():
        _write(HERE / f"{name}.jsonl", rows)
    _write(HERE / "subagents" / "agent-ax.jsonl", SUBAGENT)
    _write(HERE / "subagent_rows_in_main.jsonl", SUBAGENT)
