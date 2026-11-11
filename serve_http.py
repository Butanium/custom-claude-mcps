"""Serve one of this repo's MCP servers over streamable HTTP on loopback.

Claude Code spawns every stdio MCP server once per session, so N concurrent
sessions cost N copies of each server. Running a server here instead makes it
one always-on process that every session connects to (`type: http` in the
Claude config). All servers in this repo are stateless with respect to the
caller — tools take explicit arguments and never read the session's environment
or cwd — so sharing one process is safe.

Usage (normally via the systemd unit in `systemd/`, see `install_http.sh`):

    python3 serve_http.py <name>            # e.g. team-inbox
    python3 serve_http.py <name> --port N   # override the registry port
    python3 serve_http.py --list            # "<name> <port>" per line

The first invocation re-execs itself through `uv run --project <dir>` so the
server runs inside its own venv; the registry below maps each server name to
its project directory, importable module, and loopback port.

`NODE_SERVERS` are third-party Node servers wrapped by a script in this repo
(same sharing argument, checked per server); those are exec'd with `node`.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# name -> (project dir under HERE, module exposing a FastMCP object named `mcp`, port)
SERVERS: dict[str, tuple[str, str, int]] = {
    "team-inbox": ("team-inbox", "server", 8871),
    "big-read": ("big-read", "server", 8872),
    "transcript-reader": ("transcript-reader", "server", 8873),
    "paper-search": ("paper-search-mcp", "paper_search_mcp.server", 8874),
}

# name -> (project dir under HERE, script run with node, port). The script takes
# --host/--port; `npm ci` in the project dir is install_http.sh's job.
NODE_SERVERS: dict[str, tuple[str, str, int]] = {
    "pdf": ("pdf-viewer", "serve.mjs", 8875),
}

STAGE_ENV = "CUSTOM_CLAUDE_MCPS_IN_VENV"


def exec_node_server(name: str, host: str, port: int | None) -> None:
    project_dir, script, default_port = NODE_SERVERS[name]
    project_path = HERE / project_dir
    if not (project_path / "node_modules").is_dir():
        sys.exit(f"[serve_http] {project_path}/node_modules is missing: run `npm ci` there (install_http.sh does)")
    node = os.environ.get("NODE", "node")
    cmd = [node, str(project_path / script), "--host", host, "--port", str(port or default_port)]
    os.execvp(cmd[0], cmd)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("name", nargs="?", choices=sorted({**SERVERS, **NODE_SERVERS}))
    ap.add_argument("--port", type=int, help="override the registry port")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--list", action="store_true", help="print '<name> <port>' per line")
    args = ap.parse_args()

    if args.list:
        for name, (_, _, port) in {**SERVERS, **NODE_SERVERS}.items():
            print(name, port)
        return
    if args.name is None:
        ap.error("name is required unless --list")
    if args.name in NODE_SERVERS:
        exec_node_server(args.name, args.host, args.port)

    project_dir, module_name, port = SERVERS[args.name]
    project_path = HERE / project_dir
    port = args.port or port

    if os.environ.get(STAGE_ENV) != "1":
        # Stage 1: hop into the server's own venv, then run this script again.
        env = {**os.environ, STAGE_ENV: "1"}
        cmd = [
            os.environ.get("UV", "uv"),
            "run",
            "--project",
            str(project_path),
            "python",
            str(Path(__file__).resolve()),
            args.name,
            "--host",
            args.host,
            "--port",
            str(port),
        ]
        os.execvpe(cmd[0], cmd, env)

    # Stage 2: inside the venv.
    sys.path.insert(0, str(project_path))
    mod = importlib.import_module(module_name)
    mcp = mod.mcp
    mcp.settings.host = args.host
    mcp.settings.port = port
    # Stateless: no server-side session table, so a client can reconnect at any
    # time (including after a service restart) without a stale-session 404.
    mcp.settings.stateless_http = True
    print(f"[serve_http] {args.name} -> http://{args.host}:{port}/mcp", file=sys.stderr)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
