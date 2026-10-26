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
import signal
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


def cache_dir() -> Path:
    """Tool-private scratch space, in the layout Hermes itself uses: ``<home>/cache/<tool>/``.

    Runtime artefacts (run directories) are nobody's configuration and must not accumulate at
    the top of ``HERMES_HOME``: that directory is a documented list, so every extra entry there
    is a thing a user has to reason about when deciding what is safe to delete. ``cache/`` is
    the one place whose whole contract is "safe to lose".
    """
    return hermes_home() / "cache" / "hawkeye"


def run_root(base: str | None = None) -> Path:
    if base:
        return Path(base).expanduser()
    env = os.environ.get("HAWKEYE_V2_ROOT", "").strip()
    if env:
        return Path(env).expanduser()
    return cache_dir() / "runs"


def run_dir(run_id: str, base: str | None = None) -> Path:
    return run_root(base) / run_id


@contextlib.contextmanager
def file_lock(path: Path):
    """Serialise read-modify-write cycles on one JSON document across processes.

    Two processes legitimately write ``state.json``: the phase (progress) and ``hk stop`` (a
    verdict). "Has somebody finished this run" and the write that follows must be one step, or a
    stop landing between them is overwritten by the stale copy -- that is how a stopped run used
    to restart, fail, and report (#7's family).
    """
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


DEFAULT_DELIVERY_GRACE = 300.0


def delivery_grace(cfg: dict) -> float:
    """The one default for how long an undelivered report protects its run from the sweep.

    Measured from ``finished_at``, not from ``last_report_at``: a run that finished hours ago and
    reported seconds ago is pruned by both callers. That is deliberate -- the window exists to cover
    the delivery of the *final* report, and a run whose report was never consumed gets an incident,
    which keeps its directory past the window anyway.

    gc and doctor used to default this key to different numbers (300 and 0), so the sweep was the
    permissive side of a two-answer question. It only looked safe because config.load() merges
    DEFAULTS into every config: any caller handing the sweep a hand-built dict got the old split.
    """
    raw = cfg.get("delivery_grace_seconds")
    # An absent *or null* value means "use the default"; an explicit 0 means "no grace" and is
    # honoured, because that is a decision somebody wrote down.
    if raw is None:
        raw = DEFAULT_DELIVERY_GRACE
    return float(raw)


def incident_active(rd: Path) -> dict:
    """The open incident for a run, or {} when there is none (or it was withdrawn).

    Withdrawing keeps the file: it records that something looked wrong and what settled it, which
    is the difference between "this never happened" and "this was resolved".
    """
    doc = read_json(rd / INCIDENT, {}) or {}
    return {} if doc.get("withdrawn_at") else doc


def report_consumed(state: dict, lease: dict) -> bool:
    """True when a lease renewal happened at or after the last report.

    "Consumed" is relative to the report, not to the clock: a notifier reports, wakes the session,
    gets its lease and is done. Comparing the lease age to *now* therefore flags every healthy
    notifier one grace window later, which is #10. A run with no report has nothing to consume and
    is not "unconsumed" -- callers check ``reports > 0`` themselves.
    """
    last_report = float(state.get("last_report_at") or 0)
    seen = float((lease or {}).get("agent_seen") or 0)
    return bool(last_report) and seen >= last_report


def prune_verdict(rd: Path, days: float, *, grace_seconds: float = 0.0,
                  keep_incidents: bool = True, now: float | None = None) -> str | None:
    """Why run directory *rd* must stay, or ``None`` when it may be deleted.

    One answer to "may this directory go", so ``hk gc`` and the doctor sweep cannot disagree.
    A grace window applied by only one of them is not a grace window: the sweep runs unattended
    every two minutes and deleted a finished run whose wake was still queued, which is the loss
    ``hk gc`` had just been taught to avoid (#10 follow-on).

    Every reason it can return is a reason *not* to delete: an unfinished run's phase may still
    be alive and would keep writing into a directory that no longer exists, a report nobody has
    leased yet is still a wake on its way, and an incident is evidence.

    ``days`` is "finished at least this long ago"; a non-positive value means no age gate at all,
    which is what ``hk gc --days 0 --force`` asks for. ``prunable_runs`` keeps the *other*
    reading of zero ("retention disabled") because the doctor's sweep must be switchable off.
    """
    if not rd.is_dir() or not (rd / META).is_file():
        return "not a run"
    state = read_json(rd / STATE, {}) or {}
    if not state.get("finished"):
        return "not finished"
    now = time.time() if now is None else now
    finished_at = float(state.get("finished_at") or 0)
    if days > 0 and finished_at and (now - finished_at) < days * 86400:
        return "younger than %g days" % days
    if grace_seconds > 0 and int(state.get("reports") or 0) > 0:
        lease = read_json(rd / LEASE, {}) or {}
        if not report_consumed(state, lease) and (now - finished_at) < grace_seconds:
            return "report not delivered yet"
    if keep_incidents and (rd / INCIDENT).is_file():
        return "has incident"
    return None


def prunable_runs(root: Path, days: float, *, keep_incidents: bool = True,
                  grace_seconds: float = 0.0) -> list[Path]:
    """Finished runs older than *days* that may be deleted (see ``prune_verdict``).

    ``days <= 0`` disables retention here rather than meaning "no age gate": the doctor's sweep
    must be switchable off, and a sweep that deleted every finished run the moment the setting
    was zeroed would be the worst possible reading of "off".
    """
    if days <= 0 or not root.is_dir():
        return []
    now = time.time()
    return [rd for rd in sorted(root.iterdir())
            if prune_verdict(rd, days, grace_seconds=grace_seconds,
                             keep_incidents=keep_incidents, now=now) is None]


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
    """Merge *patch* into an existing JSON document, atomically against other writers."""
    with file_lock(path):
        data = read_json(path, {}) or {}
        merged = data if isinstance(data, dict) else {}
        merged.update(patch)
        write_json(path, merged)
        return merged


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


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def reap(targets: list[tuple[int, int | None]]) -> list[int]:
    """SIGTERM every live target, escalate to SIGKILL, and return the pids still alive.

    A shell waiting on a foreground child defers SIGTERM, so one signal is not a kill. Every
    caller holds pids of processes in their own session -- the ``hk job`` wrapper, and the command
    that wrapper started -- so each target is signalled as a group.
    """
    live = [(pid, ticks) for pid, ticks in targets if pid_alive(pid, ticks)]
    for pid, _ticks in live:
        _signal_group(pid, signal.SIGTERM)
    survivors: list[int] = []
    for pid, ticks in live:
        for _attempt in range(20):
            if not pid_alive(pid, ticks):
                break
            time.sleep(0.05)
        else:
            _signal_group(pid, signal.SIGKILL)
        for _attempt in range(20):
            if not pid_alive(pid, ticks):
                break
            time.sleep(0.05)
        else:
            survivors.append(pid)
    return survivors


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
