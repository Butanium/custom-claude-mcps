#!/usr/bin/env python3
"""turn_state on small fixture transcripts (tests/fixtures/turn_state, made by make_fixtures.py).

Run: cd mcp/team-inbox && uv run python tests/test_turn_state.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from turn_state import FIRST_CHUNK, turn_state  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "turn_state"

# fixture -> (ended, reason, pending tools)
EXPECT = {
    "idle_end_turn": (True, "end_turn", []),
    "open_tool_use": (False, "tool_use", ["Bash"]),
    "parallel_tools_one_result": (False, "tool_use", ["Bash"]),
    "tool_result_generating": (False, "generating", []),
    "message_no_reply": (False, "generating", []),
    "queued_command_after_end": (True, "end_turn", []),
    "queued_command_mid_turn": (False, "tool_use", ["Monitor"]),
    "compacted_idle": (True, "end_turn", []),
    "compacted_mid_turn": (False, "generating", []),
    "compacted_then_resumed": (True, "end_turn", []),
    "legacy_summary_rows": (False, "tool_use", ["Grep"]),
    "local_command_after_idle": (True, "end_turn", []),
    "meta_row_mid_turn": (False, "generating", []),
    "interrupted": (True, "interrupted", []),
    "api_error": (True, "api_error", []),
    "sidechain_rows_in_main": (False, "tool_use", ["Agent"]),
    "bookkeeping_only": (None, "no_rows", []),
    "subagents/agent-ax": (True, "end_turn", []),
    "subagent_rows_in_main": (None, "no_rows", []),
}

results: list[tuple[str, bool, str]] = []
for name, want in EXPECT.items():
    st = turn_state(FIX / f"{name}.jsonl")
    got = (st.ended, st.reason, st.pending_tools) if st else None
    results.append((name, got == want, f"got {got}, want {want}"))

results.append(("missing file -> None", turn_state(FIX / "nope.jsonl") is None, ""))
results.append(("no path -> None", turn_state(None) is None, ""))

with tempfile.TemporaryDirectory() as tmp:
    # Deciding row sits behind more than one tail chunk of bookkeeping rows.
    big = Path(tmp) / "big.jsonl"
    rows = [(FIX / "idle_end_turn.jsonl").read_text()]
    pad = json.dumps({"type": "attachment", "attachment": {"type": "file", "content": "x" * 1000}}) + "\n"
    rows.append(pad * (2 * FIRST_CHUNK // len(pad)))
    big.write_text("".join(rows))
    st = turn_state(big)
    results.append(("deciding row beyond first tail chunk",
                    st is not None and st.ended is True, f"got {st}"))

    # A half-written last line (Claude Code mid-append) is skipped, not fatal.
    partial = Path(tmp) / "partial.jsonl"
    partial.write_text((FIX / "open_tool_use.jsonl").read_text() + '{"type": "user", "message": {"con')
    st = turn_state(partial)
    results.append(("partial last line ignored",
                    st is not None and st.ended is False and st.pending_tools == ["Bash"], f"got {st}"))

fails = 0
for name, ok, detail in results:
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
sys.exit(1 if fails else 0)
