"""Check that one shared pdf-viewer process keeps sessions' viewers apart.

Starts `serve.mjs` on a free port with a temp directory as its only allowed
directory, then plays two Claude sessions (A and B) and their two viewers over
raw MCP-over-HTTP. Run after bumping the pinned server-pdf version:

    python3 test_shared.py
"""

from __future__ import annotations

import argparse
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent


def minimal_pdf() -> bytes:
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


class Client:
    def __init__(self, port: int):
        self.url = f"http://127.0.0.1:{port}/mcp"
        self._id = 0

    def call(self, tool: str, arguments: dict, timeout: float = 40.0, host: str | None = None) -> dict:
        self._id += 1
        body = json.dumps(
            {"jsonrpc": "2.0", "id": self._id, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        ).encode()
        headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
        if host:
            headers["Host"] = host
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
        # The stateless transport answers either as plain JSON or as one SSE "data:" line.
        payloads = [line[5:].strip() for line in raw.splitlines() if line.startswith("data:")] or [raw]
        msg = json.loads(payloads[-1])
        assert "result" in msg, msg
        return msg["result"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--node", default=shutil.which("node") or "node")
    args = ap.parse_args()

    port = free_port()
    with tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as outside:
        for d in (allowed, outside):
            for name in ("a.pdf", "b.pdf"):
                (Path(d) / name).write_bytes(minimal_pdf())
        proc = subprocess.Popen(
            [args.node, str(HERE / "serve.mjs"), "--port", str(port), allowed],
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stderr is not None
            banner = proc.stderr.readline()
            assert f"127.0.0.1:{port}" in banner, banner
            run_checks(port, Path(allowed), Path(outside))
        finally:
            proc.terminate()
            proc.wait(timeout=5)
    print("all checks passed")


def run_checks(port: int, allowed: Path, outside: Path) -> None:
    a, b = Client(port), Client(port)

    uuid_a = a.call("display_pdf", {"url": str(allowed / "a.pdf")})["structuredContent"]["viewUUID"]
    uuid_b = b.call("display_pdf", {"url": str(allowed / "b.pdf")})["structuredContent"]["viewUUID"]
    assert uuid_a != uuid_b
    print("ok  two sessions get two viewers")

    polls: dict[str, list] = {}

    def viewer(name: str, uuid: str) -> None:
        polls[name] = Client(port).call("poll_pdf_commands", {"viewUUID": uuid})["structuredContent"]["commands"]

    threads = {n: threading.Thread(target=viewer, args=(n, u), daemon=True) for n, u in (("a", uuid_a), ("b", uuid_b))}
    for t in threads.values():
        t.start()
    time.sleep(0.5)  # both long-polls are parked on the server

    a.call("interact", {"viewUUID": uuid_a, "commands": [{"action": "navigate", "page": 1}]})
    threads["a"].join(timeout=5)
    assert polls.get("a") == [{"type": "navigate", "page": 1}], polls
    time.sleep(1.5)
    assert "b" not in polls, f"B's viewer received A's command: {polls}"
    print("ok  A's command reaches A's viewer only")

    b.call("interact", {"viewUUID": uuid_b, "commands": [{"action": "navigate", "page": 1}]})
    threads["b"].join(timeout=5)
    assert polls.get("b") == [{"type": "navigate", "page": 1}], polls
    print("ok  B's command reaches B's viewer")

    refused = a.call("display_pdf", {"url": str(outside / "a.pdf")})
    assert refused.get("isError") is True, refused
    print("ok  a file outside the allowed directories is refused")

    try:
        a.call("list_pdfs", {}, host=f"evil.example:{port}")
        raise AssertionError("request with a foreign Host header was served")
    except urllib.error.HTTPError as e:
        assert e.code == 403, e.code
    print("ok  a foreign Host header is refused")


if __name__ == "__main__":
    main()
