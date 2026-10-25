"""hawkeye-v2 store — run directories, atomic JSON, append-only events, locks.

Everything durable about a run lives here. No module keeps state in memory
across phases: a phase may be killed at any moment and the next one must be
able to resume from these files alone.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Iterator

META = "meta.json"
STATE = "state.json"
RESULT = "result.json"
LEASE = "lease.json"
INCIDENT = "incident.json"
EVENTS = "events.jsonl"
REPORT = "report.md"
WORK_LOG = "work.log"
SUP_BEAT = "sup.heartbeat"
JOB_PID = "job.pid"
JOB_EXIT = "job.exit"
JOB_BEAT = "job.heartbeat"
LOCK = ".lock"


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def fmt_dur(seconds: float) -> str:
    total = int(max(0.0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%dh%02dm" % (hours, minutes)
    if minutes:
        return "%dm%02ds" % (minutes, secs)
    return "%ds" % secs


def hermes_home() -> Path:
    env = os.environ.get("HERMES_HOME", "").strip()
    return Path(env).expanduser() if env else Path.home() / ".hermes"


def run_root(base: str | None = None) -> Path:
    if base:
        return Path(base).expanduser()
    env = os.environ.get("HAWKEYE_V2_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    return hermes_home() / "hawkeye-v2" / "runs"


def run_dir(run_id: str, base: str | None = None) -> Path:
    return run_root(base) / run_id


def new_run_id(kind: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    return "%s-%s-%04x" % (kind, stamp, os.getpid() & 0xFFFF)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name("%s.tmp%d" % (path.name, os.getpid()))
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, default=str)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def update_json(path: Path, patch: dict) -> dict:
    data = read_json(path, {}) or {}
    with_data = data if isinstance(data, dict) else {}
    with_data.update(patch)
    write_json(path, with_data)
    return with_data


def append_event(rd: Path, name: str, **detail: Any) -> dict:
    rec = {"ts": time.time(), "at": now_iso(), "kind": name}
    rec.update({k: v for k, v in detail.items() if v is not None})
    payload = (json.dumps(rec, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    fd = os.open(str(rd / EVENTS), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, payload)
    finally:
        os.close(fd)
    return rec


def read_events(rd: Path, kind: str | None = None, limit: int | None = None) -> list[dict]:
    out: list[dict] = []
    try:
        with open(rd / EVENTS, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if kind is None or rec.get("kind") == kind:
                    out.append(rec)
    except OSError:
        return out
    return out[-limit:] if limit else out


def tail(path: Path, max_bytes: int = 4096) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            return fh.read().decode("utf-8", "replace")
    except OSError:
        return ""


def mtime_age(path: Path) -> float | None:
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def proc_stat(pid: int) -> tuple[str, int] | None:
    """(state, starttime) from /proc/<pid>/stat — state 'Z' means a zombie."""
    try:
        raw = Path("/proc/%d/stat" % pid).read_text(encoding="utf-8", errors="replace")
        fields = raw.rsplit(")", 1)[1].split()
        return fields[0], int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def proc_start_ticks(pid: int) -> int | None:
    """Field 22 of /proc/<pid>/stat: guards against a recycled pid looking alive."""
    stat = proc_stat(pid)
    return stat[1] if stat else None


def pid_alive(pid: int | None, start_ticks: int | None = None) -> bool:
    """True only for a live non-zombie process with the expected starttime.

    ``os.kill(pid, 0)`` is not enough: an unreaped child stays a zombie and still
    answers to signal 0, which made a finished job read as "still running".
    """
    if not pid or pid <= 1:
        return False
    stat = proc_stat(int(pid))
    if stat is None:
        return False
    state, ticks = stat
    if state == "Z":
        return False
    return start_ticks is None or ticks == start_ticks


@contextlib.contextmanager
def run_lock(rd: Path) -> Iterator[bool]:
    """Non-blocking exclusive lock; False means another phase already owns the run."""
    rd.mkdir(parents=True, exist_ok=True)
    fh = open(rd / LOCK, "a+")
    try:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        yield True
    finally:
        fh.close()
