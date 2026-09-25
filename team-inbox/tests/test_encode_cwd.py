#!/usr/bin/env python3
"""_encode_cwd matches Claude Code's project-dir naming.

Expected values come from the CLI's own function (2.1.280, unpacked with
clisrc.py): `e.replace(/[^a-zA-Z0-9]/g,"-")`, and past 200 chars
`${r.slice(0,200)}-${Math.abs(hash(e)).toString(36)}` with the Java-style
`(h<<5)-h+charCodeAt|0` hash. Values generated with node from that code.

Run: cd mcp/team-inbox && uv run python tests/test_encode_cwd.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from inbox_state import _encode_cwd  # noqa: E402

CASES = {
    "/home/user/.claude": "-home-user--claude",
    "/home/user/my_project v2": "-home-user-my-project-v2",
    "/tmp/café": "-tmp-caf-",
    "/tmp/🦊": "-tmp---",
    "/a" * 120: "-a" * 100 + "-vbgiog",
}
fails = 0
for cwd, expected in CASES.items():
    got = _encode_cwd(cwd)
    ok = got == expected
    fails += not ok
    print(("PASS " if ok else "FAIL ") + repr(cwd[:40]) + ("" if ok else f"\n  expected {expected!r}\n  got      {got!r}"))
sys.exit(1 if fails else 0)
