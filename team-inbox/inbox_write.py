"""Append a message to a teammate's inbox the way the claude CLI does.

The CLI's `writeToMailbox` (2.1.280): ensure `<team>/inboxes/`, create the inbox
as `[]` if missing (exclusive create), take a `proper-lockfile` lock on it with
`lockfilePath: <inbox>.lock`, re-read the array, push
`{...message, msgV: 1, msg_id: <uuid>, type: "message", read: false}`, write it
back atomically with 2-space JSON, release. proper-lockfile's lock is a
directory created with mkdir; one whose mtime is older than `stale` (10 s by
default) is considered abandoned and removed. Recipients poll their inbox file,
so an entry written here is delivered like any SendMessage.

Stdlib only.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from inbox_state import TEAMS_ROOT

LOCK_STALE_S = 10.0
LOCK_WAIT_S = 5.0


class InboxWriteError(RuntimeError):
    pass


def _acquire(lock: Path) -> None:
    deadline = time.monotonic() + LOCK_WAIT_S
    delay = 0.005
    while True:
        try:
            os.mkdir(lock)
            return
        except FileExistsError:
            pass
        try:
            if time.time() - lock.stat().st_mtime > LOCK_STALE_S:
                os.rmdir(lock)
                continue
        except FileNotFoundError:
            continue
        if time.monotonic() > deadline:
            raise InboxWriteError(f"inbox lock {lock} still held after {LOCK_WAIT_S:.0f} s")
        time.sleep(delay)
        delay = min(delay * 2, 0.1)


def iso_now() -> str:
    """JS `new Date().toISOString()`: UTC, milliseconds, trailing Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def append_message(team_name: str, recipient: str, message: dict) -> str:
    """Append `message` ({from, text, summary?, timestamp, color?}) to the inbox; return its msg_id."""
    inboxes = TEAMS_ROOT / team_name / "inboxes"
    inboxes.mkdir(parents=True, exist_ok=True)
    path = inboxes / f"{recipient}.json"
    try:
        with open(path, "x") as f:
            f.write("[]")
    except FileExistsError:
        pass

    entry = {**message, "msgV": 1, "msg_id": str(uuid.uuid4()), "type": "message", "read": False}
    lock = path.with_name(path.name + ".lock")
    _acquire(lock)
    try:
        try:
            messages = json.loads(path.read_text() or "[]")
        except json.JSONDecodeError as e:
            raise InboxWriteError(f"{path} is not valid JSON ({e}); left untouched") from e
        if not isinstance(messages, list):
            raise InboxWriteError(f"{path} is not a JSON array; left untouched")
        messages.append(entry)
        fd, tmp = tempfile.mkstemp(dir=inboxes, prefix=f".{recipient}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(messages, f, indent=2, ensure_ascii=False)
            os.chmod(tmp, path.stat().st_mode & 0o777)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
    finally:
        try:
            os.rmdir(lock)
        except FileNotFoundError:
            pass
    return entry["msg_id"]
