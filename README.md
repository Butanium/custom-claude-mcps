# custom-claude-mcps

Small MCP servers for [Claude Code](https://code.claude.com), built for and battle-tested
in a heavily customized harness. Companion repos:
[claude-code-hooks](https://github.com/Butanium/claude-code-hooks) (lifecycle/PreToolUse
hooks — some import helpers from here, see below) and
[claude-code-patches](https://github.com/Butanium/claude-code-patches) (binary patches for
the `claude` CLI).

Each server is a self-contained `uv` project: one `server.py` on
[FastMCP](https://github.com/modelcontextprotocol/python-sdk), stdlib-only logic, no
external state. Python ≥ 3.10.

## Servers

### `team-inbox` — honest mail state for Agent-Teams

Two tools for leads (and teammates) running Claude Code Agent Teams:

- **`fetch_unread`** — messages in a team inbox that the recipient has *genuinely not
  seen*. The inbox file's `read` flag is flipped at queue time, not delivery time, so it
  lies; this parses the recipient's JSONL transcript instead — a `<teammate-message>`
  block in the transcript is the only honest "delivered" signal. Works mid-turn.
- **`teammate_status`** — is a teammate working, idle, or stalled, without pinging them:
  tmux pane title, in-flight background work (from the hooks repo's `inflight_tracker`
  state, if present), transcript mtime, and undelivered mail in **both directions** —
  messages addressed to them, and messages they sent that nobody has seen yet. The second
  direction exists because "idle" reads as "nothing to say" when it can mean "replied,
  and the reply hasn't reached you": we once watched a lead shutdown-request a reviewer
  whose findings sat undelivered in the lead's own inbox.

`inbox_state.py` (transcript-as-truth pending-mail resolution) is also imported by
`pending_message_guard.py` in the hooks repo, which *blocks* a `SendMessage` — including
a `shutdown_request` — while the recipient's unseen reply is pending. `tests/` covers
both the hook and the status report; the tests write a throwaway team under
`~/.claude/teams/` and clean up after themselves.

### `transcript-reader` — read agent transcripts without drowning

- **`read_agent_transcript`** — a filtered, human-readable trace of a Claude Code agent
  JSONL transcript (subagent or teammate): messages, tool calls, optionally truncated
  results. The alternative — `Read` on raw JSONL — dumps escaped noise several times
  larger than the useful content.
- **`get_tool_call_output`** — pull one specific tool call's full output by id.

### `big-read` — large read-only files in one call

- **`read`** — reads files past the built-in Read tool's 256KB cap, in one call. Declares
  `anthropic/maxResultSizeChars: 500000` (the hard ceiling the harness allows a meta-set
  MCP tool) so results aren't spilled to a preview-only file at the default 50k chars.
  The comment block in `server.py` documents the threshold mechanics.

### `paper-search-mcp` (submodule)

A [fork](https://github.com/Butanium/paper-search-mcp) of
[openags/paper-search-mcp](https://github.com/openags/paper-search-mcp) with local fixes
(e.g. multi-word arXiv query repair). Pulled in as a git submodule; clone with
`--recurse-submodules` if you want it.

## Installation

Clone wherever you like (`~/.claude/mcp` is where we mount it, as a submodule of the
`~/.claude` config repo), then register each server you want:

```bash
git clone --recurse-submodules https://github.com/Butanium/custom-claude-mcps ~/.claude/mcp
claude mcp add --scope user team-inbox -- \
  uv run --project ~/.claude/mcp/team-inbox python ~/.claude/mcp/team-inbox/server.py
```

(same shape for `transcript-reader` and `big-read`). `uv run --project` resolves each
server's own lockfile — no shared venv, no version skew between servers.
