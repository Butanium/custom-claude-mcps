#!/bin/bash
# Run every server in this repo as an always-on streamable-HTTP process (one
# systemd user unit each) and point Claude Code's user-scope config at them.
# Idempotent: re-run after pulling changes to restart the units with new code.
#
# Requires: systemd user session (`systemctl --user`), `uv`, `claude` on PATH.
# For the units to outlive your login shell: `loginctl enable-linger $USER`.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
UNIT_DIR="$HOME/.config/systemd/user"

mkdir -p "$UNIT_DIR"
cp "$HERE/systemd/claude-mcp@.service" "$UNIT_DIR/"
systemctl --user daemon-reload

mapfile -t rows < <(python3 "$HERE/serve_http.py" --list)
for row in "${rows[@]}"; do
  name=${row%% *}
  port=${row##* }
  systemctl --user enable "claude-mcp@$name" >/dev/null
  systemctl --user restart "claude-mcp@$name"
  # Replace whatever entry Claude has for this name (typically the stdio one)
  # with the HTTP endpoint. Tool names are unchanged because the server name is.
  claude mcp remove -s user "$name" >/dev/null 2>&1 || true
  claude mcp add -s user --transport http "$name" "http://127.0.0.1:$port/mcp" </dev/null
done

echo
systemctl --user --no-pager list-units 'claude-mcp@*'
echo
claude mcp list
