# Engineering logs

Append-only. What changed and why; the README says how things work now.

## 2026-11-11 — pdf-viewer: don't put the deny entry in managed settings

**Tried and reverted.** To keep the `deniedMcpServers` entry out of a `settings.json`
that is shared between machines, I moved it to
`/etc/claude-code/managed-settings.d/pdf-viewer-shared.json`. The deny worked, but any
managed settings file makes Claude Code (2.1.321) load its built-in
`cc-plugin-sec-default`, which seats itself before every user-installed plugin and
skipped their hooks (`<mod>: its classic.Stop hook was skipped. "cc-plugin-sec-default"
runs first and sent the event on past it`). Running sessions picked the file up live and
kept the plugin after the file was removed, until restarted. The entry is back in user
settings. The CLI's description of `prependPlugins` says setting it in managed settings
keeps that plugin out; not tested.

## 2026-11-11 — pdf-viewer: one shared copy of the plugin's PDF server

**Why.** The `pdf-viewer` plugin (synced from claude.ai) starts
`npx -y @modelcontextprotocol/server-pdf --stdio` in every session: an `npm`, a `sh` and
a `node` each time. With 52 sessions that was 156 processes holding 0.9 GB of RAM and
4.7 GB of swap.

**What changed.** `pdf-viewer/serve.mjs` runs the same package (pinned to 2.0.4) over
streamable HTTP on loopback; `serve_http.py` gained a `NODE_SERVERS` registry and execs
`node` for those; `install_http.sh` runs `npm ci` for them. Unit `claude-mcp@pdf`, port
8875, registered at user scope as `pdf`.

**Is sharing safe? Read from `dist/server.js` of 2.0.4.**
- Viewer state is safe. The command queue, the poll waiters, the source path and the
  form-field names are module-level maps keyed by the `randomUUID()` that `display_pdf`
  returns. Nothing is keyed by connection. The package's own HTTP entry already builds
  a new MCP server object per request and relies on those maps.
- The local-file allowlist is not. `allowedLocalDirs` is one process-wide set, and
  `refreshRoots` does `.clear()` on it before adding the connecting client's roots, so
  each session would replace the others' directories. Over stateless HTTP the roots
  request also runs on a server object that never saw `initialize`. So the wrapper
  never enables client roots and takes a fixed directory list instead. Consequence:
  any local process that can reach loopback (so every session) can open and save PDFs
  anywhere under those directories, where the stdio copy limited each session to its
  own working directory. Same exposure as `big-read`.
- `elicit_form_inputs` is skipped: it checks client capabilities on the per-request
  server object, which has none.

**Why a wrapper instead of `mcp-pdf-server` without `--stdio`.** That entry calls
`app.listen(port)` with no host, so it listens on every interface, and enables open
CORS. The wrapper binds 127.0.0.1, sends no CORS headers and rejects requests whose
`Host` header is not loopback. It imports `createServer` and `allowedLocalDirs` from the
package's `main` (`dist/server.js`); `dist/index.js` cannot be imported, it runs `main()`.

**Turning off the plugin's own copy.** Claude Code only merges a plugin server with a
user-configured one when the command or URL is identical (`Suppressing plugin MCP
server … duplicates manually-configured` in the CLI), so an HTTP entry never replaces a
stdio one. Editing the plugin's `.mcp.json` would be undone by the next sync. What works
is `deniedMcpServers` in user settings, by name (`plugin:pdf-viewer:pdf`) or by command.
Tested on 2.1.321 with one fresh `claude -p` session per form: the server is absent from
the session's `mcp_servers` and no process is started. Running sessions on recent
versions pick the setting up live and stop their copy.

**Verified.** `python3 pdf-viewer/test_shared.py`: two sessions open two viewers, each
session's `interact` command arrives only at its own viewer's poll, the viewer page
(`resources/read` of the `ui://` resource) and `read_pdf_bytes` are served, a file
outside the allowlist is refused, a foreign `Host` is refused. A fresh default-config session lists
only `mcp__pdf__*` and its `list_pdfs` call reached the unit. Not verified: the viewer
drawn by the desktop app through the shared server (terminal sessions never draw it;
the CLI only advertises MCP Apps support when the host sets `CLAUDE_CODE_MCP_APPS_HOST`).

**`list_pdfs` is replaced.** The package's version walks every allowed directory and
returns up to 500 paths. With a home directory allowed that was a 107 KB result made of
other projects' files, and the plugin's `/open` command tells the model to call it. The
wrapper swaps the handler (through the tool's own `update()`) for one that names the
allowed directories and tells the model to find the file itself. `serve.mjs` builds one
server at startup so a package version that drops `_registeredTools.list_pdfs` fails
there, not on a request.

**Gotchas.** The unit's PATH has no nvm directory, so it runs `/usr/bin/node`; `npm` is
only needed by `install_http.sh`. The deny entry lives in `settings.json`: on a machine
that shares that file but has no `claude-mcp@pdf` unit, sessions get no PDF tools.

## 2026-09-26 — team-inbox `teammate_status`: working/idle from the transcript

**What changed.** New `turn_state.py`: the last conversation row of a transcript says
whether the turn is open (tool_use awaiting a result, a message with no reply) or over
(assistant stop_reason other than tool_use/pause_turn/null, API-error row, user
interrupt). The `status:` headline now comes from it and names its source; the pane
title is a secondary line, and the headline source only when no transcript is found.
Same rule as itls's `_closes_turn`, ported rather than imported (itls is a separate,
private repo), plus rows itls doesn't step over: compaction summaries (after a manual
`/compact` at the prompt, itls reads the summary row as an open turn), local-command and
bash-mode rows (`<command-name>`, `<local-command-stdout>`, `<bash-input>`: typed at the
prompt, no reply follows), and sidechain rows, which count only in a
`subagents/agent-*.jsonl` file (every row there is a sidechain row).

**Why.** Pane title and registry `busy` both count background work as working: a
Claude Code 2.1.197 session kept a braille spinner frame in its title for weeks while
sitting at its prompt with a Monitor armed.

**Also fixed while verifying on a live team.**
- Every tmux teammate printed `pane: unknown`: teammates live on a per-lead tmux server
  (`/tmp/tmux-<uid>/claude-swarm-<lead pid>`), and the old code queried the default
  server, where `display-message -t %N` for a missing pane prints nothing with rc 0.
  The socket now comes from the member process's `TMUX` env var (process found in
  /proc by `--agent-name`/`--team-name`), and a pane whose `#{pane_id}` doesn't echo back
  is "gone". On 2.1.280 swarm panes all carry the lead's session title with an idle
  glyph, even mid-turn, so the pane line mostly shows that the pane exists.
- A tmux member with no live process reads "not running" instead of whatever its
  transcript last said.
- The lead's transcript was "not found" when the lead had resumed into a forked
  session: `leadSessionId` keeps the id from team creation. Teammates'
  `--parent-session-id` gives the live one.
- In-process teammates (`tmuxPaneId: "in-process"`) are found through
  `<lead session>/subagents/agent-*.meta.json` (`teamName` + `name`), get no pane lookup,
  and `member` also accepts a subagent the lead spawned with `Agent(name=...)`.

**Tests.** `tests/test_turn_state.py` over fixtures made by
`tests/fixtures/turn_state/make_fixtures.py`; `tests/test_member_status_turn.py` builds a
throwaway team with patched roots.

## 2026-09-23 — team-inbox `broadcast`

**What changed.** New `broadcast(team_name, sender, message, summary)` tool and
`inbox_write.py`, which appends an entry to a member's inbox file the way the CLI's
`writeToMailbox` does.

**Why.** `SendMessage` in 2.1.280 rejects `to: "*"` ("broadcast … is no longer
supported — send a message per recipient") and the delivery code behind that check
resolves one name and writes one inbox; there is no fan-out left to re-enable with a
binary patch. Writing the inbox files ourselves needs no patch.

**How it matches the CLI (2.1.280, `chunk-kc5318jd.js` `writeToMailbox`).** Exclusive
create of `[]` if the inbox is missing; `proper-lockfile` with
`lockfilePath: <inbox>.lock` (a mkdir lock; stale after 10 s); re-read, push
`{from, text, summary, timestamp, color, msgV: 1, msg_id: <uuid>, type: "message",
read: false}`; atomic write, `JSON.stringify(…, null, 2)`. The CLI's in-memory record
of the entry is the sender's bookkeeping, which recipients don't read.

**Verified.** Haiku lead + two tmux teammates idle at the prompt; the lead called
`broadcast` through the MCP; both woke, received an ordinary
`<teammate-message teammate_id="team-lead" summary="…">`, and replied via
`SendMessage`.

**Known risk.** The CLI has a second mailbox backend behind a flag (`N()` /
`storageV5` in `writeToMailbox`). If that becomes the default, inbox files stop being
read and `broadcast` will still report "Delivered". Re-run the check above after a
claude update that touches teams.

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

**Gotcha.** Units don't source the shell profile, so a host that sets uv options in
`.bashrc` (e.g. `UV_LINK_MODE=symlink`) must repeat them for the units. The unit reads
`~/.config/claude-mcp.env` if present for exactly this; first attempt put
`link-mode = "symlink"` in the global `uv.toml`, reverted because it changed uv's
behaviour for every non-shell context on the host to fix one.
