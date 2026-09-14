# Engineering logs

Append-only. What changed and why; the README says how things work now.

## 2026-09-14 — serve every server over streamable HTTP as one shared process

**What changed.** `serve_http.py` (registry of name → project dir, module, loopback
port; re-execs itself through `uv run --project` so the server runs in its own venv,
then runs the FastMCP object with `transport="streamable-http"`, `stateless_http=True`).
`systemd/claude-mcp@.service` template unit, `install_http.sh` to enable one unit per
server and swap the user-scope Claude entries from stdio to `type: http`.

**Why.** Claude Code spawns stdio servers per session and has no sharing mechanism
(checked the 2.1.257 binary, the changelog through 2.1.271, the docs, and GitHub —
anthropics/claude-code#28860 asked for exactly this and was auto-closed as a duplicate
of a Windows bug that then went stale; #83771 from Aug 2026 reports the same pile-up
and is open with no reply). On the dev box with ~31 live sessions that was 248 server
processes and ~4.2 GB resident. HTTP servers are shared by construction.

**Decisions.**
- *Stateless HTTP* rather than the default stateful session table. With stateful mode a
  unit restart invalidates every client's session id and the client sees 404s until it
  reinitializes; stateless accepts any request at any time. Our tools carry no per-client
  state, so nothing is lost.
- *Two-stage exec via `uv run`* instead of calling each venv's python directly, so a
  wiped venv (cache prune, host rebuild) self-heals on the next unit restart rather than
  crash-looping. Cost: the unit needs `uv` on PATH, set explicitly in the unit since
  systemd user services don't source the shell profile.
- *One template unit, ports in the registry* instead of one unit per server with a port
  each: the unit only knows the name (`%i`), the script knows everything else, so adding
  a server is one registry line.
- *Loopback only, no auth.* The servers read local files and the box is single-user.
- *Tool names unchanged* by keeping the Claude-side server names, so existing permission
  allowlists (`mcp__team-inbox__fetch_unread`) and hook matchers
  (`mcp__paper-search__download_arxiv`) keep matching.

**Verified.** `claude mcp list` shows all four connected over HTTP; a fresh
`claude -p --allowedTools mcp__big-read__read` session read a file through the shared
unit (the unit's journal shows the `CallToolRequest`) with no permission denials.

**Gotcha.** `uv run` inside the unit honours `~/.config/uv/uv.toml`, not the shell's
env — on a host that relies on `UV_LINK_MODE=symlink` from `.bashrc`, put
`link-mode = "symlink"` in `uv.toml` too or the first sync from the unit copies files.
