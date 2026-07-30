"""MCP server that reads Claude Code agent JSONL transcripts and returns a filtered, human-readable trace."""

from __future__ import annotations

import json
from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("transcript-reader")


def _truncate(text: str, limit: int) -> str:
    """Truncate text to limit chars. -1 means no limit."""
    if limit < 0 or len(text) <= limit:
        return text
    return text[:limit] + f"... [{len(text) - limit} chars truncated]"


def _short_id(tool_id: str) -> str:
    """Last 4 chars of tool_use_id for matching calls to results."""
    return tool_id[-4:] if len(tool_id) >= 4 else tool_id


def _format_content_block(
    block: dict, tool_use_ids: dict[str, str], max_chars_per_input: int = 200
) -> str | None:
    """Format a single content block from an assistant message.

    Registers tool_use blocks in tool_use_ids mapping (id -> tool name) for
    linking tool results back to their calls.
    """
    if block.get("type") == "text":
        text = block.get("text", "").strip()
        if text:
            return f"[ASSISTANT] {text}"
    elif block.get("type") == "tool_use":
        name = block.get("name", "?")
        tool_id = block.get("id", "")
        if tool_id:
            tool_use_ids[tool_id] = name
        inp = block.get("input", {})
        inp_str = json.dumps(inp, ensure_ascii=False) if inp else ""
        sid = _short_id(tool_id)
        return f"[TOOL_CALL:{sid}] {name}({_truncate(inp_str, max_chars_per_input)})"
    return None


def _extract_tool_result_content(block: dict) -> str:
    """Extract text content from a tool_result block."""
    content = block.get("content", "")
    if isinstance(content, list):
        content = " ".join(
            c.get("text", "") if isinstance(c, dict) else str(c) for c in content
        )
    return str(content).strip()


def _format_tool_result(
    block: dict,
    include_bash_output: bool,
    tool_use_ids: dict[str, str],
    tool_use_result: dict | None,
    max_chars_per_tool: int = 500,
) -> str | None:
    """Format a single tool_result block from a user message."""
    tool_id = block.get("tool_use_id", "")
    tool_name = tool_use_ids.get(tool_id, "?")
    sid = _short_id(tool_id)
    tag = f"[TOOL_RESULT:{tool_name}:{sid}]"

    # Append structured metadata from toolUseResult if present
    meta_parts: list[str] = []
    if isinstance(tool_use_result, dict):
        if agent_id := tool_use_result.get("agentId"):
            meta_parts.append(f"agent={agent_id}")
        if output_file := tool_use_result.get("outputFile"):
            meta_parts.append(f"output={output_file}")
    meta_suffix = f" ({', '.join(meta_parts)})" if meta_parts else ""

    content = _extract_tool_result_content(block)
    if not content:
        return f"{tag}{meta_suffix} (empty)"

    if not include_bash_output and "exit code" in content.lower():
        first_line = content.split("\n", 1)[0]
        return f"{tag}{meta_suffix} {_truncate(first_line, max_chars_per_tool)}"
    return f"{tag}{meta_suffix} {_truncate(content, max_chars_per_tool)}"


def _parse_event(
    line: str,
    include_bash_output: bool,
    tool_use_ids: dict[str, str],
    max_chars_per_tool: int = 500,
    max_chars_per_input: int = 200,
) -> list[str]:
    """Parse a single JSONL line and return formatted output lines."""
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return []

    event_type = event.get("type")
    message = event.get("message", {})
    content = message.get("content")

    if event_type == "assistant":
        if isinstance(content, list):
            lines = []
            for block in content:
                formatted = _format_content_block(block, tool_use_ids, max_chars_per_input)
                if formatted:
                    lines.append(formatted)
            return lines

    elif event_type == "user":
        if isinstance(content, str):
            return [f"[USER] {_truncate(content, max_chars_per_tool)}"]
        elif isinstance(content, list):
            tool_use_result = event.get("toolUseResult")
            lines = []
            for block in content:
                if block.get("type") == "tool_result":
                    formatted = _format_tool_result(
                        block,
                        include_bash_output,
                        tool_use_ids,
                        tool_use_result,
                        max_chars_per_tool,
                    )
                    if formatted:
                        lines.append(formatted)
            return lines

    return []


@mcp.tool()
def read_agent_transcript(
    file_path: str,
    include_bash_output: bool = True,
    limit: int | None = None,
    offset: int = 0,
    max_chars_per_tool: int = 500,
    max_chars_per_input: int = 200,
    this_will_blow_my_context_i_know_what_i_am_doing_gimme_raw_jsonl: bool = False,
    output_file: str | None = None,
) -> str:
    """Read a Claude Code agent JSONL transcript and return a filtered, readable trace.

    Each tool call in the output is tagged with a short 4-char ID (e.g. [TOOL_CALL:ab12])
    that can be used with get_tool_call_output to retrieve the full untruncated result.

    IMPORTANT usage guidelines:
    - First call: leave all params at defaults. This gives a compact summary with truncated tool outputs.
    - Do NOT set max_chars_per_tool=-1 unless you know the transcript is small, as this can lead to pretty big outputs.
    If you need a specific tool's full output, use get_tool_call_output with the 4-char ID instead.
    - If the tool writes to output_file because the result is too large, do NOT try to Read/Grep that file. Retry with a smaller limit instead.

    Pagination via offset/limit:
    - Last 10 events:         offset=-10
    - 10 events before that:  limit=10, offset=-20
    - First 8 events:         limit=8
    - Events 20-29:           limit=10, offset=20
    - All events (default):   (no params)

    Args:
        file_path: Path to the agent JSONL transcript file.
        include_bash_output: If True (default), include full bash command output in tool results.
            If False, only show the first line of bash results.
        limit: Maximum number of events to return. None returns all (after offset).
        offset: Start position, 0-indexed. Negative values count from end (Python semantics).
        max_chars_per_tool: Maximum characters per tool result output (default 500, -1 for no limit).
            Increase to see more of each result, or use get_tool_call_output for a specific one.
        max_chars_per_input: Maximum characters per tool call input (default 200, -1 for no limit).
        this_will_blow_my_context_i_know_what_i_am_doing_gimme_raw_jsonl: If True, return the raw JSONL content unfiltered.
            Only use this for debugging — it WILL dump the entire transcript into your context.
        output_file: If set, write the result to this file path instead of returning it.
            Returns a short confirmation message instead of the full content.
    """
    path = Path(file_path)
    if not path.exists():
        return f"Error: file not found: {file_path}"

    raw_text = path.read_text()

    if this_will_blow_my_context_i_know_what_i_am_doing_gimme_raw_jsonl:
        lines = raw_text.splitlines()
        total = len(lines)
        start = max(total + offset, 0) if offset < 0 else min(offset, total)
        end = min(start + limit, total) if limit is not None else total
        lines = lines[start:end]
        result = f"[lines {start + 1}-{end} of {total}]\n" + "\n".join(lines)
    else:
        tool_use_ids: dict[str, str] = {}
        output_lines: list[str] = []
        for raw_line in raw_text.splitlines():
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            formatted = _parse_event(
                raw_line, include_bash_output, tool_use_ids, max_chars_per_tool, max_chars_per_input
            )
            output_lines.extend(formatted)

        total = len(output_lines)
        start = max(total + offset, 0) if offset < 0 else min(offset, total)
        end = min(start + limit, total) if limit is not None else total
        output_lines = output_lines[start:end]

        if not output_lines:
            return "No events found in transcript."

        result = f"[events {start + 1}-{end} of {total}]\n" + "\n".join(output_lines)

    if output_file is not None:
        out_path = Path(output_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(result)
        return f"Written to {output_file} ({len(result)} chars, {result.count(chr(10)) + 1} lines)"

    return result


@mcp.tool()
def get_tool_call_output(
    file_path: str,
    tool_call_id: str,
) -> str:
    """Get the full untruncated output of a specific tool call from a transcript.

    Use this when read_agent_transcript shows a truncated result (ending with "... [N chars truncated]")
    and you need the complete output. The short 4-char IDs shown in [TOOL_CALL:xxxx] and
    [TOOL_RESULT:name:xxxx] tags from read_agent_transcript can be used directly as tool_call_id.

    Args:
        file_path: Path to the agent JSONL transcript file.
        tool_call_id: The short ID (last 4 chars) or full tool_use_id to look up.
    """
    path = Path(file_path)
    if not path.exists():
        return f"Error: file not found: {file_path}"

    for raw_line in path.read_text().splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue

        if event.get("type") != "user":
            continue

        content = event.get("message", {}).get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if block.get("type") != "tool_result":
                continue
            full_id = block.get("tool_use_id", "")
            if full_id.endswith(tool_call_id) or full_id == tool_call_id:
                result_content = _extract_tool_result_content(block)
                meta = event.get("toolUseResult", {})
                meta_str = json.dumps(meta, indent=2) if meta else ""
                parts = [f"tool_use_id: {full_id}"]
                if meta_str:
                    parts.append(f"metadata: {meta_str}")
                parts.append(f"content:\n{result_content}")
                return "\n".join(parts)

    return f"No tool result found matching ID: {tool_call_id}"


if __name__ == "__main__":
    mcp.run()
