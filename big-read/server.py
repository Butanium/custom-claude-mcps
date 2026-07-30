"""MCP server exposing a single tool to read large read-only files in one call."""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("big-read")


# `anthropic/maxResultSizeChars` raises this tool's harness spill-to-file threshold.
# MCP tools default to a 50,000-char ceiling (min of the 100k base maxResultSizeChars and
# the 50k default persistenceThresholdCeiling) — below that, large results get offloaded to
# a tool-results file with only a 2KB preview. Setting this meta overrides maxResultSizeChars
# AND lifts the persistence ceiling, so the effective threshold becomes min(value, 500000).
# Since big-read's whole purpose is returning large content, we set it to 500,000 — the
# HARD CEILING the harness allows for a meta-set MCP tool (the `np6` constant; values above
# it clamp at best, get rejected at worst). 500k chars is ~125k tokens; going past it would
# require an Anthropic-side per-tool Statsig override, which isn't settable from here.
@mcp.tool(meta={"anthropic/maxResultSizeChars": 500000})
def read(
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
) -> str:
    """Read a large read-only file in a single call, bypassing the built-in Read tool's 256KB byte cap.

    **When to use this tool:**
    - You need to load a single file (or a slice of one) that the built-in Read tool refuses
      because it exceeds the 256KB byte cap, AND your purpose is to *read/analyze* the
      content holistically — e.g. a dump of model samples, a transcript, a long log file,
      a corpus of generated outputs you want to qualitatively analyze.

    **When NOT to use this tool:**
    - You want to edit the file. The Edit tool requires the *built-in* Read tool to have
      run on the file first; an MCP read does NOT satisfy that check. For editing, use the
      built-in Read (and slice the file with start_line/end_line if needed there).
    - The file fits within 256KB. Use the built-in Read tool — it gives line numbers in
      a format Edit understands and is strictly cheaper.
    - You only need to find a specific string. Use Grep instead.
    - You want to skim a huge codebase. Spawn an Explore subagent or use Grep — loading
      whole files into your own context burns tokens you'll need for thinking.

    Output is gated by the MAX_MCP_OUTPUT_TOKENS environment variable (the MCP-tool
    output cap, separate from the Read tool's caps). If the requested slice exceeds that,
    output will be truncated by the harness — pass start_line/end_line to read in chunks.

    Args:
        path: Absolute path to the file to read.
        start_line: 1-indexed line to start at (inclusive). None = start of file.
        end_line: 1-indexed line to end at (inclusive). None = end of file.

    Returns:
        The requested lines, prefixed `cat -n` style: "<line_number>\\t<line>". A
        leading header reports the file path, total line count, byte size, and the
        slice returned. NOTE: these line numbers are NOT recognized by the Edit tool;
        re-Read with the built-in tool before editing.
    """
    p = Path(path)
    if not p.exists():
        return f"Error: file not found: {path}"
    if not p.is_file():
        return f"Error: not a regular file: {path}"

    try:
        text = p.read_text()
    except UnicodeDecodeError as e:
        return f"Error: file is not valid UTF-8 ({e}). big-read only supports text files."

    lines = text.splitlines()
    total = len(lines)
    size = p.stat().st_size

    lo = 1 if start_line is None else max(1, start_line)
    hi = total if end_line is None else min(total, end_line)
    if lo > hi:
        return f"Error: start_line ({lo}) > end_line ({hi}); file has {total} lines."

    width = len(str(hi))
    body = "\n".join(f"{i:>{width}}\t{lines[i - 1]}" for i in range(lo, hi + 1))

    header = (
        f"# {path}\n"
        f"# total lines: {total}, size: {size:,} bytes\n"
        f"# returning lines {lo}..{hi} ({hi - lo + 1} lines)\n"
    )
    return header + body


if __name__ == "__main__":
    mcp.run()
