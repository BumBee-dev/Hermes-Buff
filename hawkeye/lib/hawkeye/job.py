"""Detached job execution — the work itself is the durable unit.

`hk job` runs ONE command, appends its output to ``work.log``, keeps a wrapper
heartbeat alive while it runs and writes the exit code to ``job.exit``. That
last file is the point: a finished job leaves its own verdict on disk even if
every supervisor, the agent turn, or the whole WebUI process died meanwhile.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from . import store


def _beat(path: Path, interval: float, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            path.write_text(store.now_iso())
        except OSError:
            pass
        stop.wait(interval)


def _wait(proc: subprocess.Popen) -> int:
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        code = proc.wait()
    # Popen reports death by signal as -N; the shell convention is 128+N.
    return code if code >= 0 else 128 - code


def run(rd: Path, argv: list[str], cwd: str | None, beat_seconds: float,
        attempt: int) -> int:
    """Execute argv once and return its exit code (124/127 on our own failures)."""
    rd.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    beater = threading.Thread(target=_beat, args=(rd / store.JOB_BEAT, beat_seconds, stop), daemon=True)
    beater.start()
    code = 127
    try:
        with open(rd / store.WORK_LOG, "ab") as log:
            log.write(("=== attempt %d started %s ===\n" % (attempt, store.now_iso())).encode())
            log.flush()
            try:
                proc = subprocess.Popen(
                    argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=log,
                    stderr=subprocess.STDOUT, start_new_session=True,
                )
            except OSError as exc:
                log.write(("spawn failed: %s\n" % exc).encode())
                log.flush()
            else:
                (rd / store.JOB_PID).write_text(str(proc.pid))
                code = _wait(proc)
    finally:
        stop.set()
        beater.join(timeout=1.0)
        (rd / store.JOB_EXIT).write_text(str(code))
    return code


def exit_code(rd: Path) -> int | None:
    raw = ""
    try:
        raw = (rd / store.JOB_EXIT).read_text().strip()
    except OSError:
        return None
    try:
        return int(raw)
    except ValueError:
        return None
