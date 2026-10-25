"""Loop closer — restarts dead phases and raises incidents. Needs no agent.

This is the module that answers "what if I time out or stop on an error halfway": recovery
must not depend on the agent turn, the WebUI process, or a wake being delivered.
It is meant to run from a systemd user timer (``config/systemd/hawkeye-doctor.*``);
``hk doctor`` is also safe to call by hand at any time.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from . import store

INCIDENT_UNREACHABLE = "agent_unreachable"
INCIDENT_CRASH_LOOP = "phase_crash_loop"


def _prune_old_runs(cfg: dict) -> int:
    """Delete finished runs past ``retention_days``; incidents are evidence and stay.

    Housekeeping belongs here rather than in a user's memory: this sweep already runs every two
    minutes, it is the only thing guaranteed to run on a host nobody is watching, and without it
    the run cache grows for the life of the machine with no one able to tell which entries still
    matter.
    """
    days = float(cfg.get("retention_days", 0) or 0)
    if days <= 0:
        return 0
    removed = 0
    for rd in store.prunable_runs(store.run_root(), days):
        shutil.rmtree(rd, ignore_errors=True)
        removed += 1
    return removed


def _restart(cfg: dict, rd: Path, meta: dict) -> Optional[int]:
    """Restart a dead phase, but never in an unbounded loop.

    A phase that dies immediately would otherwise be respawned every doctor
    tick forever (2 min), burning CPU with zero progress and no signal. Past
    ``max_restarts`` we stop and let the incident speak instead.
    """
    entry = str(meta.get("entry") or "").strip()
    if not entry or not Path(entry).is_file():
        return None
    # meta.json was read at scan time; if the run was deleted or renamed since (hk gc, a test
    # removing its root), spawning a phase here would recreate the directory and poll forever.
    if not (rd / store.META).is_file():
        return None
    if _restart_count(rd) >= int(cfg.get("max_restarts", 5)):
        return None
    proc = subprocess.Popen([sys.executable, entry, "phase", "--run", rd.name],
                            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    store.append_event(rd, "phase_restart_by_doctor", pid=proc.pid)
    return proc.pid


def _restart_count(rd: Path) -> int:
    return len(store.read_events(rd, kind="phase_restart_by_doctor"))


def _crash_loop_hit(cfg: dict, rd: Path) -> bool:
    return _restart_count(rd) >= int(cfg.get("max_restarts", 5))


def _age_text(age: float) -> str:
    return "never leased" if age == float("inf") else "%ds ago" % int(age)


def _page(cfg: dict, rd: Path, state: dict, lease_age: float) -> None:
    """Optional external page. Off unless ``fallback_command`` is configured."""
    cmd = str(cfg.get("fallback_command") or "").strip()
    if not cmd or state.get("fallback_done"):
        return
    line = "[hawkeye] %s reported but nobody renewed the lease (%s), status in %s" % (
        rd.name, _age_text(lease_age), rd / store.REPORT)
    try:
        subprocess.run(cmd, shell=True, input=line.encode(), timeout=60, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        store.append_event(rd, "fallback_failed", error=str(exc))
        return
    store.write_json(rd / store.STATE, {**(store.read_json(rd / store.STATE, {}) or {}),
                                        "fallback_done": True})
    store.append_event(rd, "fallback_sent")


def _open_incident(cfg: dict, rd: Path, state: dict, lease_age: float) -> bool:
    return _open_incident_kind(
        cfg, rd, state, INCIDENT_UNREACHABLE,
        "sent %s reports and the lease was not renewed (%s); the agent is probably gone"
        % (int(state.get("reports") or 0), _age_text(lease_age)),
        lease_age=lease_age)


def _open_incident_kind(cfg: dict, rd: Path, state: dict, reason: str, detail: str,
                        lease_age: float | None = None) -> bool:
    """One incident file per run per reason; opening is idempotent."""
    existing = store.read_json(rd / store.INCIDENT, {}) or {}
    if existing.get("reason") == reason:
        return False
    store.write_json(rd / store.INCIDENT, {
        "reason": reason, "at": store.now_iso(), "detail": detail,
    })
    store.append_event(rd, "incident_open", reason=reason,
                       lease_age_s=None if lease_age in (None, float("inf")) else int(lease_age))
    if lease_age is not None:
        _page(cfg, rd, state, lease_age)
    return True


def scan(cfg: dict, fix: bool = False) -> list[dict[str, Any]]:
    root = store.run_root()
    rows: list[dict[str, Any]] = []
    if not root.is_dir():
        return rows
    if fix:
        pruned = _prune_old_runs(cfg)
        if pruned:
            print("[hawkeye] pruned %d finished run(s) older than %.0f days"
                  % (pruned, float(cfg.get("retention_days", 0))))
    for rd in sorted(root.iterdir()):
        if not rd.is_dir():
            continue
        meta = store.read_json(rd / store.META, {}) or {}
        if not meta:
            continue
        state = store.read_json(rd / store.STATE, {}) or {}
        result = store.read_json(rd / store.RESULT, {}) or {}
        sup_age = store.mtime_age(rd / store.SUP_BEAT)
        lease = store.read_json(rd / store.LEASE, {}) or {}
        seen = float(lease.get("agent_seen") or 0)
        lease_age = float("inf") if not seen else max(0.0, time.time() - seen)

        if state.get("finished"):
            status = "finished"
        elif sup_age is not None and sup_age < float(cfg["sup_stale_seconds"]):
            status = "running"
        else:
            status = "orphaned"

        action = ""
        if status == "orphaned" and fix:
            if _crash_loop_hit(cfg, rd):
                # Phase keeps crashing: stop restarting it, make it visible instead
                if _open_incident_kind(cfg, rd, state, INCIDENT_CRASH_LOOP,
                                       "supervision phase still dies after %d restarts, suspected crash loop"
                                       % _restart_count(rd)):
                    incident = True
                status = "crash_loop"
                action = "restart budget exhausted, stopped retrying and raised an incident"
            elif _restart(cfg, rd, meta):
                status, action = "restarted", "restarted the supervisor"
            else:
                action = "cannot restart (meta.entry missing)"

        incident = bool(store.read_json(rd / store.INCIDENT, {}))
        # A finished run whose report nobody consumed is the dangerous case, not
        # an exception: the work is done and the one person who should know is gone.
        if (int(state.get("reports") or 0) > 0
                and lease_age > float(cfg["wake_grace_seconds"])):
            if _open_incident(cfg, rd, state, lease_age):
                incident = True
                action = (action + "; " if action else "") + "incident raised"

        rows.append({
            "run": rd.name,
            "kind": meta.get("kind", "exec"),
            "status": status,
            "result": result.get("reason"),
            "attempts": int(state.get("attempts_done") or 0),
            "reports": int(state.get("reports") or 0),
            "sup_age_s": None if sup_age is None else int(sup_age),
            "lease_age_s": None if lease_age == float("inf") else int(lease_age),
            "incident": incident,
            "action": action,
        })
    return rows
