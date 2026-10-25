#!/usr/bin/env python3
"""hawkeye-v2 self-test — real subprocesses, real files, no mocks.

Covers the three properties the design is built on:
  1. failures are retried in-phase (one report, not one wake per attempt);
  2. a hung job is detected and killed;
  3. exiting or SIGKILLing a supervision phase never kills the work, and
     de-risks the wake channel (no report before terminal state while nobody
     renews the lease).
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402  (local test helper, same directory)

HK = Path(__file__).resolve().parents[1] / "bin" / "hk"
BASE = Path(tempfile.mkdtemp(prefix="hkv2-selftest-"))
RUNS = BASE / "runs"
ENV = {**os.environ, "HAWKEYE_V2_ROOT": str(RUNS)}
FAILURES: list[str] = []


def _toml_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(value)


def cfg(name: str, **over):
    data = {
        "attempts": 3,
        "backoff_seconds": [0.1, 0.1],
        "attempt_timeout_seconds": 60,
        "stall_seconds": 60,
        "poll_seconds": 0.2,
        "report_interval_seconds": 999,
        "milestone_prefix": "HK-MS ",
        "milestone_flush_seconds": 0.3,
        "kanban_kinds": ["completed", "blocked"],
        "kanban_quiet_seconds": 1,
        "wake_grace_seconds": 0.3,
        "sup_stale_seconds": 1,
        "max_wakes": 6,
        "fallback_command": "",
    }
    data.update(over)
    path = BASE / ("%s.toml" % name)
    path.write_text("\n".join("%s = %s" % (k, _toml_value(v))
                              for k, v in data.items()) + "\n")
    return path


def hk(args, conf, timeout=90, check=True):
    env = {**ENV, "HAWKEYE_V2_CONFIG": str(conf)}
    proc = subprocess.run([sys.executable, str(HK)] + list(args),
                          capture_output=True, text=True, timeout=timeout, env=env)
    if check and proc.returncode != 0:
        raise AssertionError("hk %s rc=%d\n%s\n%s"
                             % (" ".join(args), proc.returncode, proc.stdout, proc.stderr))
    return proc


def read(rid: str, name: str):
    path = RUNS / rid / name
    return json.loads(path.read_text()) if path.is_file() else None


def events(rid: str) -> list[dict]:
    path = RUNS / rid / "events.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def check(label: str, ok: bool, detail: str = "") -> None:
    print("  %s %s%s" % ("PASS" if ok else "FAIL", label, "" if ok else "  <<< " + detail))
    if not ok:
        FAILURES.append(label)


def wait_for(predicate, timeout: float, step: float = 0.1) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return False


def script(name: str, body: str) -> Path:
    path = BASE / name
    path.write_text(body)
    return path


# ── 1. retries happen in-phase, one report only ─────────────────────────
def test_retry_then_success() -> None:
    conf = cfg("retry")
    counter = BASE / "retry.count"
    job = script("retry.sh", "#!/bin/bash\nn=$(cat %s 2>/dev/null || echo 0)\n"
                            "n=$((n+1)); echo $n > %s\necho \"attempt $n\"\n[ $n -ge 3 ]\n"
                             % (counter, counter))
    rid = "retry-1"
    proc = hk(["run", "--id", rid, "--", "/bin/bash", str(job)], conf)
    result = read(rid, "result.json") or {}
    state = read(rid, "state.json") or {}
    failed = [e for e in events(rid) if e["kind"] == "attempt_failed"]
    check("retry: eventually succeeds", result.get("reason") == "done", str(result))
    check("retry: first two failures recorded", len(failed) == 2 and state.get("attempts_done") == 2,
          "failed=%d done=%s" % (len(failed), state.get("attempts_done")))
    check("retry: phase exits once (one wake)", state.get("reports") == 1, str(state.get("reports")))
    check("retry: report text usable", "[hawkeye]" in proc.stdout and "lease:" in proc.stdout)


# ── 2. stall detection ───────────────────────────────────────────────────
def test_stall_is_detected() -> None:
    conf = cfg("stall", attempts=1, stall_seconds=0.8)
    job = script("stall.sh", "#!/bin/bash\necho started\nsleep 20\n")
    rid = "stall-1"
    started = time.time()
    hk(["run", "--id", rid, "--", "/bin/bash", str(job)], conf)
    result = read(rid, "result.json") or {}
    failed = [e for e in events(rid) if e["kind"] == "attempt_failed"]
    check("stall: judged stalled and gave up", result.get("reason") == "gaveup", str(result))
    check("stall: event carries reason=stalled",
          bool(failed) and failed[0].get("reason") == "stalled", str(failed[:1]))
    check("stall: finishes within 10s (does not sit out the full 20s)", time.time() - started < 10,
          "%.1fs" % (time.time() - started))
    check("stall: incident left behind", (RUNS / rid / "incident.json").is_file())


# ── 3. agent stops renewing -> doctor raises an incident ─────────────────
def test_agent_unreachable() -> None:
    conf = cfg("unreach")
    job = script("ok.sh", "#!/bin/bash\necho ok\n")
    rid = "unreach-1"
    hk(["run", "--id", rid, "--", "/bin/bash", str(job)], conf)
    proc = hk(["doctor", "--json"], conf, check=False)
    rows = json.loads(proc.stdout)
    row = [r for r in rows if r["run"] == rid][0]
    check("unreach: doctor spots the dead lease", row["incident"] is True, json.dumps(row))
    check("unreach: incident file written", (RUNS / rid / "incident.json").is_file())
    check("unreach: doctor exits non-zero (systemd can see it)", proc.returncode == 1,
          str(proc.returncode))


def test_agent_responsive() -> None:
    conf = cfg("respons")
    job = script("ok2.sh", "#!/bin/bash\necho ok\n")
    rid = "respons-1"
    hk(["run", "--id", rid, "--", "/bin/bash", str(job)], conf)
    hk(["lease", "--run", rid], conf)
    rows = json.loads(hk(["doctor", "--json"], conf, check=False).stdout)
    row = [r for r in rows if r["run"] == rid][0]
    check("respons: no incident after a lease renew", row["incident"] is False, json.dumps(row))


# ── 4. phase killed -> job keeps running + doctor restarts and finishes it ─
def test_phase_death_recovery() -> None:
    conf = cfg("recover", attempts=1, sup_stale_seconds=0.4)
    job = script("slow.sh", "#!/bin/bash\nfor i in 1 2 3 4 5 6; do echo tick $i; sleep 0.4; done\n")
    rid = "recover-1"
    env = {**ENV, "HAWKEYE_V2_CONFIG": str(conf)}
    phase = subprocess.Popen([sys.executable, str(HK), "run", "--id", rid, "--",
                              "/bin/bash", str(job)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    started = wait_for(lambda: (RUNS / rid / "state.json").is_file()
                       and (read(rid, "state.json") or {}).get("job_pid"), 10)
    check("recover: job started", started)
    phase.send_signal(signal.SIGKILL)          # simulate my turn being hard-killed
    phase.wait(timeout=5)
    job_pid = (read(rid, "state.json") or {}).get("job_pid")
    alive_after = os.path.exists("/proc/%d" % job_pid) if job_pid else False
    check("recover: job still running after the phase is killed", alive_after, "job_pid=%s" % job_pid)
    time.sleep(1.2)                            # wait for sup.heartbeat to go stale
    rows = json.loads(hk(["doctor", "--json", "--fix"], conf, check=False).stdout)
    row = [r for r in rows if r["run"] == rid][0]
    check("recover: doctor marks it orphaned and restarts", row["status"] in ("restarted", "running"),
          json.dumps(row))
    done = wait_for(lambda: (read(rid, "result.json") or {}).get("reason") is not None, 20)
    result = read(rid, "result.json") or {}
    check("recover: restarted phase picks up and finishes", done and result.get("reason") == "done", str(result))


# ── 5. milestone reports + no wake spam without a lease + work not held back ─
def test_milestone_and_wake_discipline() -> None:
    conf = cfg("ms", attempts=1, milestone_flush_seconds=0.3)
    job = script("ms.sh", "#!/bin/bash\n"
                          "echo 'HK-MS phase one 3/10'\nsleep 1\n"
                          "echo 'HK-MS phase two 6/10'\nsleep 2\n"
                          "echo finished\n")
    rid = "ms-1"
    first = hk(["run", "--id", rid, "--", "/bin/bash", str(job)], conf)
    state = read(rid, "state.json") or {}
    check("ms: phase exits for a milestone (one wake)", state.get("last_reason") == "milestone",
          str(state.get("last_reason")))
    check("ms: report carries the milestones", "milestones:" in first.stdout and "phase one" in first.stdout)
    job_pid = state.get("job_pid")
    check("ms: job still running after the phase exits (not interrupted)",
          bool(job_pid) and os.path.exists("/proc/%d" % job_pid), "job_pid=%s" % job_pid)

    second = hk(["phase", "--run", rid], conf)          # no lease: defer the report, wait for terminal
    state2 = read(rid, "state.json") or {}
    deferred = [e for e in events(rid) if e["kind"] == "report_deferred"]
    result = read(rid, "result.json") or {}
    check("ms: non-terminal report deferred while the lease is dead", bool(deferred), "deferred event missing")
    check("ms: deferral did not stop the work from finishing", result.get("reason") == "done", str(result))
    check("ms: final report carries both milestones", "phase two" in second.stdout, second.stdout[-200:])


def test_status_all() -> None:
    rows = json.loads(hk(["status", "--all", "--json"], cfg("status")).stdout)
    ids = {r["run"] for r in rows}
    check("status: lists every run", {"retry-1", "stall-1", "ms-1"} <= ids, str(sorted(ids)))
    check("status: finished state classified correctly",
          any(r["run"] == "retry-1" and r["status"] == "done" for r in rows), str(rows[:2]))


def test_kanban_watcher_reads_default_board() -> None:
    """v1 died on string-joining `boards/<slug>/kanban.db`: the default board's db lives
    in <home>/kanban.db. This uses a copy of the real db to prove this version goes
    through the kernel's kanban_db_path()."""
    import sqlite3

    home = fixtures.make_hermes_home(BASE / "kbhome")
    fixtures.build_kanban_db(home / "kanban.db")
    conf = cfg("kb", kanban_quiet_seconds=0.6, poll_seconds=0.2)
    rid = "kb-1"
    env = {**ENV, "HAWKEYE_V2_CONFIG": str(conf), "HERMES_HOME": str(home)}
    phase = subprocess.Popen([sys.executable, str(HK), "kanban", "--id", rid, "--board", "default"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    ready = wait_for(lambda: (read(rid, "state.json") or {}).get("cursor_ready"), 15)
    check("kb: first start aligns the cursor to the current max id (no history replay)", bool(ready))

    conn = sqlite3.connect(home / "kanban.db")
    conn.execute("insert or replace into tasks (id,title,status,assignee,created_at)"
                 " values ('t_kbprobe','board probe','done','dev-coder',1790000000)")
    conn.execute("insert into task_events (task_id,run_id,kind,payload,created_at)"
                 " values ('t_kbprobe',1,'completed','{}',1790000001)")
    probe_id = conn.execute("select max(id) from task_events").fetchone()[0]
    conn.commit()
    conn.close()

    try:
        out, err = phase.communicate(timeout=25)
    except subprocess.TimeoutExpired:
        phase.kill()
        out, err = phase.communicate()
        check("kb: a new event triggers one report", False, "timeout, err=%s" % err[-300:])
        return
    state = read(rid, "state.json") or {}
    check("kb: a new event triggers one report",
          "[hawkeye]" in out and "board:" in out, (out or err)[-300:])
    check("kb: cursor advances after the report", (state.get("cursor") or 0) == probe_id,
          "cursor=%s probe_id=%s" % (state.get("cursor"), probe_id))
    check("kb: report carries the task id", "t_kbprobe" in out, out[-300:])


def test_crash_loop_is_bounded() -> None:
    """On a crash loop the doctor must stop and raise an incident, not restart forever every 2 min."""
    conf = cfg("loop", sup_stale_seconds=0.2, max_restarts=3)
    rid = "loop-1"
    rd = RUNS / rid
    rd.mkdir(parents=True)
    (rd / "meta.json").write_text(json.dumps({
        "run_id": rid, "kind": "exec", "created_at": time.time(),
        "entry": str(HK), "argv": ["/bin/true"], "cwd": "/tmp"}))
    (rd / "state.json").write_text(json.dumps({"attempts_done": 0}))
    with open(rd / "events.jsonl", "a", encoding="utf-8") as fh:
        for _ in range(3):
            fh.write(json.dumps({"ts": time.time(), "at": "x",
                                 "kind": "phase_restart_by_doctor", "pid": 1}) + "\n")
    (rd / "sup.heartbeat").write_text("stale")
    old = time.time() - 600
    os.utime(rd / "sup.heartbeat", (old, old))

    rows = json.loads(hk(["doctor", "--json", "--fix"], conf, check=False).stdout)
    row = [r for r in rows if r["run"] == rid][0]
    check("crash_loop: no restart past max_restarts", row["status"] == "crash_loop", json.dumps(row))
    check("crash_loop: no new restart event", (rd / "events.jsonl").read_text().count("phase_restart") == 3)
    incident = json.loads((rd / "incident.json").read_text())
    check("crash_loop: incident reason is phase_crash_loop",
          incident.get("reason") == "phase_crash_loop", str(incident))


def test_gc_keeps_incidents() -> None:
    """gc only clears "expired and finished"; runs with an incident are kept by default (they are evidence)."""
    conf = cfg("gc")
    old = time.time() - 10 * 86400
    for rid, finished, incident in (("gc-old", True, False), ("gc-inc", True, True),
                                    ("gc-live", False, False)):
        rd = RUNS / rid
        rd.mkdir(parents=True)
        (rd / "meta.json").write_text(json.dumps({"run_id": rid, "kind": "exec",
                                                  "created_at": old, "entry": str(HK),
                                                  "argv": ["/bin/true"]}))
        (rd / "state.json").write_text(json.dumps({
            "finished": finished, "finished_at": old if finished else None}))
        if incident:
            (rd / "incident.json").write_text(json.dumps({"reason": "agent_unreachable"}))

    out = hk(["gc", "--days", "7"], conf)
    check("gc: removes expired runs without an incident", not (RUNS / "gc-old").exists())
    check("gc: keeps runs with an incident", (RUNS / "gc-inc").exists())
    check("gc: keeps unfinished runs", (RUNS / "gc-live").exists())
    check("gc: output reports the cleanup", "cleaned up" in out.stdout and "removed 1" in out.stdout, out.stdout)

    hk(["gc", "--days", "7", "--force"], conf)
    check("gc: only --force removes incidents too", not (RUNS / "gc-inc").exists())


def main() -> int:
    if not HK.is_file():
        print("cannot find %s" % HK)
        return 2
    print("hawkeye-v2 selftest  root=%s" % RUNS)
    for test in (test_retry_then_success, test_stall_is_detected, test_agent_unreachable,
                 test_agent_responsive, test_phase_death_recovery,
                 test_milestone_and_wake_discipline, test_kanban_watcher_reads_default_board,
                 test_crash_loop_is_bounded, test_gc_keeps_incidents, test_status_all):
        print("\n# %s" % test.__name__)
        try:
            test()
        except Exception as exc:                      # noqa: BLE001 - test harness
            print("  FAIL exception: %r" % exc)
            FAILURES.append(test.__name__)
    print("\nresult: %s" % ("all passed" if not FAILURES else "failed %d: %s"
                             % (len(FAILURES), FAILURES)))
    if FAILURES:
        print("failure site kept: %s" % BASE)
    else:
        shutil.rmtree(BASE, ignore_errors=True)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
