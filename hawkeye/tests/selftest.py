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

import contextlib
import json
import os
import re
import shutil
import signal
import sqlite3
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
# Shared by cfg() below and by the unreachable-agent test, which has to wait past
# this window explicitly. Keeping one constant means a tuning change cannot leave
# the test asserting against a grace value the product no longer uses.
WAKE_GRACE = 0.3


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
        "wake_grace_seconds": WAKE_GRACE,
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


def test_stall_zero_disables_the_check() -> None:
    """`stall_seconds = 0` is documented as "disable"; it used to mean "kill on the first tick".

    The knob exists for the opposite case — a long job that is quiet by design (a watcher that
    prints nothing for hours). Following the documented sentinel killed exactly that job in its
    first poll, which is the worst possible failure: the configuration that says "trust this
    process" is the one that destroys it.
    """
    conf = cfg("stall0", attempts=1, stall_seconds=0, attempt_timeout_seconds=30)
    job = script("stall0.sh", "#!/bin/bash\necho started\nsleep 3\necho done\n")
    rid = "stall0-1"
    started = time.time()
    hk(["run", "--id", rid, "--", "/bin/bash", str(job)], conf)
    elapsed = time.time() - started
    result = read(rid, "result.json") or {}
    failed = [e for e in events(rid) if e["kind"] == "attempt_failed"]
    check("stall0: a quiet job runs to completion when stall_seconds = 0",
          result.get("reason") == "done", str(result))
    check("stall0: nothing was killed as stalled", not failed, str(failed[:1]))
    check("stall0: it waited for the job instead of the first tick", elapsed >= 2.5,
          "%.1fs" % elapsed)


def test_skill_ships_every_file_it_points_at() -> None:
    """A skill that points at a file ships only if the installer copies it.

    Two defects in one class: the references existed nowhere in the tree, and then they existed
    in the tree but ``install.sh`` still copied a single file, so a fresh host got a SKILL.md
    whose three pointers led nowhere. Checking the tree alone would have missed the second half.
    """
    repo = Path(__file__).resolve().parents[1]
    skill_dir = repo / "skill" / "hermes-hawkeye-v2"
    text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
    refs = sorted(set(re.findall(r"references/[A-Za-z0-9._-]+\.md", text)))
    missing = [r for r in refs if not (skill_dir / r).is_file()]
    check("skill: it points at references, and every one of them exists in the tree",
          bool(refs) and not missing, "missing=%s" % (missing or "none found"))
    installer = (repo / "install.sh").read_text(encoding="utf-8")
    check("skill: install.sh copies the whole skill directory, not just SKILL.md",
          'cp -a "$REPO_DIR/skill/hermes-hawkeye-v2/."' in installer,
          "install.sh still copies SKILL.md alone")
    check("skill: install.sh fails loudly if a referenced file was not installed",
          "skill install incomplete" in installer, "no runtime assertion in install.sh")


def test_watch_file_exits_when_the_producer_writes() -> None:
    """The notification half must be dumb: no network, no work, exit only on a real write.

    This is the piece that lets a producer keep working when nobody is in the chat (cron) while
    the wake still lands in the session that armed the watcher. It has to be boringly reliable:
    sleep on a digest, exit on change, and come back with a question instead of silence when the
    window ends.
    """
    conf = cfg("watch")
    target = BASE / "inbox.md"
    target.write_text("v1\n")
    cmd = [sys.executable, str(HK), "watch-file", "--path", str(target),
           "--interval", "0.2", "--ttl", "10", "--name", "inbox"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            env={**ENV, "HAWKEYE_V2_CONFIG": str(conf)})
    time.sleep(0.8)
    check("watch-file: does not exit while the file is unchanged", proc.poll() is None,
          "exited early with %s" % proc.returncode)
    target.write_text("v2\n")                       # the producer writes: this is the signal
    try:
        out, _ = proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        out = ""
    check("watch-file: exits when the file changes", proc.returncode == 0, str(proc.returncode))
    last_line = "".join(out.strip().splitlines()[-1:]) or "no output"
    check("watch-file: the milestone names the change", "report changed" in out, last_line)

    quiet = subprocess.run([sys.executable, str(HK), "watch-file", "--path", str(target),
                            "--interval", "0.1", "--ttl", "0.4", "--name", "inbox"],
                           capture_output=True, text=True, timeout=20,
                           env={**ENV, "HAWKEYE_V2_CONFIG": str(conf)})
    check("watch-file: a window that ends comes back with a line, not silence",
          quiet.returncode == 0 and "no change for" in quiet.stdout, quiet.stdout[-120:])


# ── 3. agent stops renewing -> doctor raises an incident ─────────────────
def test_agent_unreachable() -> None:
    """A report that nobody acknowledges has to end in an incident.

    The product fires only when two conditions hold at once: a report exists, and
    the lease is older than ``wake_grace_seconds``. Neither is guaranteed by the
    time ``hk run`` returns -- on a loaded host the doctor process can start
    inside that window and legitimately see a fresh lease -- so this test waits
    for both conditions explicitly. Relying on "doctor startup happens to be
    slower than the grace window" made this the one flaky test in the suite
    (measured: 2 of 3 runs failed), and what it flaked on is the only alarm that
    tells you the agent is gone, i.e. the path most worth testing.
    """
    conf = cfg("unreach")
    job = script("ok.sh", "#!/bin/bash\necho ok\n")
    rid = "unreach-1"
    hk(["run", "--id", rid, "--", "/bin/bash", str(job)], conf)
    reported = wait_for(lambda: (read(rid, "state.json") or {}).get("reports"), 10)
    check("unreach: the job reported before the lease went stale", bool(reported))
    time.sleep(WAKE_GRACE + 0.4)        # deterministically past wake_grace_seconds
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


def test_phase_exits_when_the_run_disappears() -> None:
    """A phase whose run directory is deleted must exit, not poll a phantom (#4).

    ``store.run_lock()`` recreates the directory on every tick, so before this guard a phase
    whose run root was removed (hk gc, or a test deleting its temp root) kept polling forever:
    no output, ~30 MB RSS each, one per green suite run.
    """
    conf = cfg("vanish", poll_seconds=0.2)
    job = script("vanish.sh", "#!/bin/bash\nfor i in $(seq 1 20); do echo tick $i; sleep 0.5; done\n")
    rid = "vanish-1"
    env = {**ENV, "HAWKEYE_V2_CONFIG": str(conf)}
    phase = subprocess.Popen([sys.executable, str(HK), "run", "--id", rid, "--",
                              "/bin/bash", str(job)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    started = wait_for(lambda: (read(rid, "state.json") or {}).get("job_pid"), 10)
    check("vanish: the job is running before the run is deleted", bool(started))
    job_pid = (read(rid, "state.json") or {}).get("job_pid")
    shutil.rmtree(RUNS / rid, ignore_errors=True)
    exited = wait_for(lambda: phase.poll() is not None, 10)
    check("vanish: the phase exits once the run directory is gone", exited,
          "still running: pid %s" % phase.pid)
    if not exited:
        phase.kill()
    phase.wait(timeout=5)
    if job_pid:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(int(job_pid)), signal.SIGKILL)
    check("vanish: the run directory is not recreated by the phase",
          not (RUNS / rid / "meta.json").exists(), str(RUNS / rid))


def test_stop_kills_the_job_command() -> None:
    """``hk stop`` must kill the command, not just the ``hk job`` wrapper.

    The wrapper starts the command with ``start_new_session=True``, so the command is its own
    process group and ``killpg(getpgid(wrapper))`` never reaches it. Measured while restarting
    the in-session watcher: the wrapper died, the bash watcher kept running with a deleted run
    directory — the same "work with no supervisor" shape as #4, caused by the stop path itself.
    """
    conf = cfg("stopkill", poll_seconds=0.2)
    rid = "stop-1"
    env = {**ENV, "HAWKEYE_V2_CONFIG": str(conf)}
    phase = subprocess.Popen([sys.executable, str(HK), "run", "--id", rid, "--",
                              "/bin/bash", "-c", "sleep 91"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    started = wait_for(lambda: (read(rid, "state.json") or {}).get("job_pid"), 10)
    check("stop: the job is running before the stop", bool(started))
    # job.pid is written by the wrapper after it spawns the command, so it lands just after
    # state["job_pid"] — wait for it instead of racing it.
    wait_for(lambda: (RUNS / rid / "job.pid").is_file(), 10)
    try:
        cmd_pid = int((RUNS / rid / "job.pid").read_text().strip())
    except (OSError, ValueError):
        cmd_pid = None
    check("stop: job.pid records the command's pid", bool(cmd_pid), str(cmd_pid))
    hk(["stop", "--run", rid], conf)
    gone = wait_for(lambda: not os.path.exists("/proc/%d" % cmd_pid), 10) if cmd_pid else False
    check("stop: the command process is gone after the stop", gone,
          "command pid still alive: %s" % cmd_pid)
    if not gone and cmd_pid:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(cmd_pid), signal.SIGKILL)
    wait_for(lambda: phase.poll() is not None, 10)
    if phase.poll() is None:
        phase.kill()
    phase.wait(timeout=5)


def test_kanban_watcher_reads_default_board() -> None:
    """v1 died on string-joining `boards/<slug>/kanban.db`: the default board's db lives
    in <home>/kanban.db. This uses a copy of the real db to prove this version goes
    through the kernel's kanban_db_path()."""
    import sqlite3

    home = fixtures.make_hermes_home(BASE / "kbhome")
    fixtures.build_kanban_db(home / "kanban.db")
    conf = cfg("kb", kanban_quiet_seconds=0.6, poll_seconds=0.2)
    rid = "kb-1"
    # HAWKEYE_NO_KERNEL: the kernel's kanban_home() ignores HERMES_HOME by design (boards
    # are shared across profiles, so it uses HERMES_KANBAN_HOME or the machine root).
    # Without the opt-out this fixture test would resolve the real board and write to it.
    env = {**ENV, "HAWKEYE_V2_CONFIG": str(conf), "HERMES_HOME": str(home),
           "HAWKEYE_NO_KERNEL": "1"}
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
        phase.wait(timeout=5)
        out, err = "", ""
        check("kb: a new event triggers one report", False, "timeout: %s" % (err or "")[-300:])
    else:
        state = read(rid, "state.json") or {}
        report = RUNS / rid / "report.md"
        text = report.read_text() if report.is_file() else ""
        check("kb: a new event triggers one report",
              "board probe" in text or "t_kbprobe" in out, (text or out)[-300:])
        check("kb: cursor advances after the report", (state.get("cursor") or 0) == probe_id,
              "cursor=%s probe_id=%s" % (state.get("cursor"), probe_id))
    finally:
        # Stop the run. A watcher that has reported is NOT finished, so a doctor sweep later in
        # this suite treats it as orphaned and restarts it — and the restarted phase then polls a
        # run root the suite deletes (#4: one orphan per green run, invisible in the output).
        subprocess.run([sys.executable, str(HK), "stop", "--run", rid], env=env,
                       capture_output=True, timeout=60)
        if phase.poll() is None:
            phase.kill()
            phase.wait(timeout=5)
    leftover = subprocess.run(["pgrep", "-f", "hk[ ]phase --run %s" % rid],
                              capture_output=True, text=True)
    check("kb: no supervision phase is left behind", leftover.stdout.strip() == "",
          leftover.stdout)


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


def test_named_board_is_not_the_default_board() -> None:
    """`--board alt` has to watch alt's DB, not ``<home>/kanban.db``.

    Until this test existed the fallback resolver returned the default DB for every
    board, so a named-board watcher would report another board's cards with no error
    at all. The event below is inserted only into the named board: if the watcher
    reads the wrong DB it stays silent and the phase never reports.

    ``HAWKEYE_NO_KERNEL=1`` forces the portable path here on purpose. It used to be
    ``-S``, but booting through ``hermes_bootstrap`` made the kernel importable even
    without site-packages, so ``-S`` silently stopped testing the fallback.
    """
    home = fixtures.make_hermes_home(BASE / "altboardhome")
    alt_db = home / "kanban" / "boards" / "alt" / "kanban.db"
    fixtures.build_kanban_db(alt_db)
    fixtures.build_kanban_db(home / "kanban.db")
    conf = cfg("altboard", kanban_quiet_seconds=0.6, poll_seconds=0.2)
    rid = "altboard-1"
    env = {k: v for k, v in ENV.items() if k != "HAWKEYE_KANBAN_DB"}
    env.update({"HAWKEYE_V2_CONFIG": str(conf), "HERMES_HOME": str(home),
                "HAWKEYE_NO_KERNEL": "1"})
    phase = subprocess.Popen([sys.executable, str(HK), "kanban", "--id", rid, "--board", "alt"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
    ready = wait_for(lambda: (read(rid, "state.json") or {}).get("cursor_ready"), 15)
    check("alt: cursor aligned on the named board", bool(ready))

    conn = sqlite3.connect(alt_db)
    conn.execute("insert or replace into tasks (id,title,status,assignee,created_at)"
                 " values ('t_altprobe','alt probe','done','dev-coder',1790000000)")
    conn.execute("insert into task_events (task_id,run_id,kind,payload,created_at)"
                 " values ('t_altprobe',1,'completed','{}',1790000001)")
    conn.commit()
    conn.close()

    try:
        out, err = phase.communicate(timeout=25)
    except subprocess.TimeoutExpired:
        phase.kill()
        phase.communicate()
        check("alt: an event on the named board triggers a report", False,
              "no report within 25s: the watcher read another board's DB")
        return
    else:
        report = RUNS / rid / "report.md"
        text = report.read_text() if report.is_file() else ""
        check("alt: the named board's card is reported", "alt probe" in text, (text or out)[-300:])
    finally:
        # Same reason as the default-board test: a watcher that has reported is NOT finished, so
        # a later doctor sweep in this suite adopts it (spawning `hk phase --run altboard-1`).
        # On a green run BASE is deleted and the phase guard kills the adoptee; on a FAILING run
        # the harness keeps the root ("failure site kept:"), and the adoptee would poll a live
        # root with nothing to do, forever. Stopping the run removes the state that makes it
        # adoptable in the first place (#4, second half — reported by the same reviewer).
        subprocess.run([sys.executable, str(HK), "stop", "--run", rid], env=env,
                       capture_output=True, timeout=60)
        if phase.poll() is None:
            phase.kill()
            phase.wait(timeout=5)
    leftover = subprocess.run(["pgrep", "-f", "hk[ ]phase --run %s" % rid],
                              capture_output=True, text=True)
    check("alt: no adopted supervision phase is left behind", leftover.stdout.strip() == "",
          leftover.stdout)


def test_gate_check_reports_ungated_profiles() -> None:
    """``hk gate-check`` must name the profiles that are not gated, and exit non-zero.

    The gate plugin loads from a profile's own ``plugins/`` directory and "enabled" is
    per-profile config, so one install leaves every other profile's workers ungated with
    nothing reporting it. Fixture: ``default`` gated, ``dev-coder`` present but disabled,
    ``dev-tester`` absent. A stub CLI answers for each profile, because the real answer
    comes from ``hermes plugins list --json``.
    """
    home = fixtures.make_hermes_home(BASE / "gatehome")
    for rel, plugin_yaml in (
        ("plugins/hermes-hawkeye-gate", True),
        ("profiles/dev-coder/plugins/hermes-hawkeye-gate", True),
    ):
        d = home / rel
        d.mkdir(parents=True, exist_ok=True)
        if plugin_yaml:
            (d / "plugin.yaml").write_text("name: hermes-hawkeye-gate\n", encoding="utf-8")
    for name in ("dev-coder", "dev-tester"):
        (home / "profiles" / name).mkdir(parents=True, exist_ok=True)
        (home / "profiles" / name / "config.yaml").write_text("plugins:\n  enabled: []\n",
                                                              encoding="utf-8")

    stub = BASE / "fake-hermes"
    stub.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  *'-p dev-coder'*) echo '[{\"name\":\"hermes-hawkeye-gate\",\"status\":\"disabled\"}]' ;;\n"
        "  *'-p dev-tester'*) echo '[]' ;;\n"
        "  *) echo '[{\"name\":\"hermes-hawkeye-gate\",\"status\":\"enabled\"}]' ;;\n"
        "esac\n", encoding="utf-8")
    stub.chmod(0o755)

    env = {**ENV, "HERMES_HOME": str(home), "HAWKEYE_HERMES_BIN": str(stub)}
    proc = subprocess.run([sys.executable, str(HK), "gate-check", "--json"],
                          capture_output=True, text=True, timeout=120, env=env)
    try:
        payload = json.loads(proc.stdout)
    except ValueError:
        check("gate-check: emits JSON", False, (proc.stdout or proc.stderr)[-300:])
        return
    check("gate-check: names both ungated profiles",
          payload.get("ungated") == ["dev-coder", "dev-tester"], json.dumps(payload.get("ungated")))
    check("gate-check: exits non-zero when a profile is ungated", proc.returncode == 1,
          str(proc.returncode))
    rows = {r["profile"]: r for r in payload.get("rows", [])}
    check("gate-check: the gated profile is reported as gated",
          rows.get("default", {}).get("gated") is True, json.dumps(rows.get("default")))
    check("gate-check: a profile without the files is 'absent'",
          rows.get("dev-tester", {}).get("enabled") == "absent", json.dumps(rows.get("dev-tester")))


def test_kernel_branch_resolves_board_paths() -> None:
    """With a real checkout, board paths must come from ``kanban_db_path()``.

    Booting through ``hermes_bootstrap`` is what makes the kernel importable outside a
    Hermes process: ``hermes_cli.kanban_db`` has its own dependencies (measured:
    ``ruamel``) that live in the environment's site-packages, not in the interpreter.
    This test resolves paths only — it writes nothing. ``HERMES_KANBAN_HOME`` points at
    a temp directory because the kernel deliberately ignores ``HERMES_HOME`` for the
    board root.

    Skips when the checkout cannot be imported (a bare host): the fallback path is
    covered by the two tests above, and there is nothing to assert about a kernel that
    is not installed.
    """
    src = os.environ.get("HAWKEYE_KERNEL_SRC", "").strip()
    if not src:
        print("  SKIP kernel branch: HAWKEYE_KERNEL_SRC is not set")
        return
    home = BASE / "khome"
    code = (
        "import os, sys\n"
        "sys.path.insert(0, %r)\n"
        "from hawkeye import kanban\n"
        "p_alt = kanban.db_path('alt')\n"
        "p_def = kanban.db_path('default')\n"
        "print('KERNEL' if kanban._KERNEL not in (None, False) else 'FALLBACK')\n"
        "print(p_alt)\n"
        "print(p_def)\n"
    ) % str(HK.parents[1] / "lib")
    env = {**ENV, "HAWKEYE_KERNEL_SRC": src, "HERMES_KANBAN_HOME": str(home),
           "HERMES_HOME": str(home)}
    env.pop("HAWKEYE_NO_KERNEL", None)
    env.pop("HAWKEYE_KANBAN_DB", None)
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                          timeout=180, env=env)
    lines = proc.stdout.strip().splitlines()
    if not lines or lines[0] != "KERNEL":
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
        print("  SKIP kernel branch: not importable from %s (%s)" % (src, tail[0]))
        return
    check("kernel: named board comes from kanban_db_path()",
          lines[1] == str(home / "kanban" / "boards" / "alt" / "kanban.db"), lines[1])
    check("kernel: default board stays at <root>/kanban.db",
          lines[2] == str(home / "kanban.db"), lines[2])



def test_stop_stops_the_phase() -> None:
    """``hk stop`` must end the run, not just label it: no restart, no rewake, no verdict rewrite.

    Reported as #7, reproduced on a clean tree. ``cmd_stop`` writes finished/finish_reason into
    state.json from a different process, while the phase loop holds the copy it read in
    ``Phase.__init__``. The loop therefore started attempt 2 one second after ``run_stopped`` and
    its ``_set()`` wrote the stale copy back -- the stop became ``gaveup`` plus a report, i.e.
    exactly the wake it was meant to prevent.
    """
    conf = cfg("stopstop", attempts=2, attempt_timeout_seconds=1.5, backoff_seconds=[0.1],
               poll_seconds=0.2)
    rid = "stop-2"
    # The command records its own pid: while this test is red, attempt 2 survives the stop in a
    # session of its own (the bug), and job.pid is cleared with the failed attempt.
    marker = BASE / "stop2-cmd.pid"
    env = {**ENV, "HAWKEYE_V2_CONFIG": str(conf)}
    phase = subprocess.Popen([sys.executable, str(HK), "run", "--id", rid, "--",
                              "/bin/bash", "-c", "echo $$ > %s; sleep 121" % marker],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    started = wait_for(lambda: (read(rid, "state.json") or {}).get("job_pid"), 10)
    check("stop2: the job is running before the stop", bool(started))
    hk(["stop", "--run", rid], conf)
    # Two full attempt windows: the regression showed up one second after the stop as
    # attempt_start, and paid off as a report one attempt later.
    ended = wait_for(lambda: phase.poll() is not None, 4.0)
    if phase.poll() is None:
        phase.kill()
    phase.wait(timeout=5)
    check("stop2: the phase exits on its own after the stop", ended, "still polling")
    kinds = [e["kind"] for e in events(rid)]
    after = kinds[kinds.index("run_stopped") + 1:] if "run_stopped" in kinds else kinds
    check("stop2: nothing starts again after the stop", "attempt_start" not in after, str(after))
    check("stop2: no report is emitted after the stop", "report" not in after, str(after))
    state = read(rid, "state.json") or {}
    check("stop2: finish_reason stays stopped", state.get("finish_reason") == "stopped", str(state))
    check("stop2: the stop is not recorded as a failed attempt",
          int(state.get("attempts_done") or 0) == 0, str(state.get("attempts_done")))
    check("stop2: a stopped run leaves no result.json", not (RUNS / rid / "result.json").is_file())
    row = next(r for r in json.loads(hk(["status", "--all", "--json"], conf).stdout)
               if r["run"] == rid)
    check("stop2: status reports the run as stopped", row["status"] == "stopped", str(row))
    try:
        os.killpg(os.getpgid(int(marker.read_text().strip())), signal.SIGKILL)
    except (OSError, ValueError):
        pass


def test_stop_leaves_a_finished_run_alone() -> None:
    """Stop must not relabel a run that already reached its own verdict (#7, same invariant).

    The stop path writes finished/finish_reason; on a done run that would rewrite the record a
    successful job earned, so it only reaps and says what it found.
    """
    conf = cfg("stopdone", poll_seconds=0.2)
    rid = "stop-3"
    hk(["run", "--id", rid, "--", "/bin/bash", "-c", "exit 0"], conf, timeout=60)
    before = read(rid, "state.json") or {}
    check("stop3: the run finished as done before the stop",
          before.get("finish_reason") == "done", str(before))
    proc = hk(["stop", "--run", rid], conf, check=False)
    check("stop3: stopping a finished run succeeds", proc.returncode == 0, proc.stderr)
    after = read(rid, "state.json") or {}
    check("stop3: the verdict stays done", after.get("finish_reason") == "done", str(after))
    check("stop3: no run_stopped event is recorded",
          not [e for e in events(rid) if e["kind"] == "run_stopped"],
          str([e["kind"] for e in events(rid)]))
    check("stop3: the stop says the run was already finished",
          "already finished" in proc.stdout, proc.stdout.strip())


def test_installer_installs_every_reference() -> None:
    """A self-consistent tree is not an installed artifact that is complete (#6).

    The installer is the only thing a fresh host runs, so this test runs it with every path
    redirected into the scratch root and then reads the *installed* copy: SKILL.md names four
    references, and a copy step that forgets one leaves an agent following dead pointers while
    the repository still looks right.
    """
    try:
        managed = subprocess.run(["systemctl", "--user", "show-environment"],
                                 capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        managed = False
    if not managed:
        print("  SKIP install: no user systemd manager for the daemon-reload step")
        return
    home = BASE / "install-home"
    skill = BASE / "install-skill"
    installer = Path(__file__).resolve().parents[1] / "install.sh"
    proc = subprocess.run(["bash", str(installer), "--no-enable",
                           "--hermes-home", str(home), "--config", str(BASE / "install.toml"),
                           "--skill-dir", str(skill)],
                          capture_output=True, text=True, timeout=120,
                          env={**ENV, "HERMES_HOME": str(home),
                               "XDG_CONFIG_HOME": str(BASE / "install-xdg")})
    check("install: the installer succeeds with every path redirected", proc.returncode == 0,
          (proc.stderr or proc.stdout).strip()[-300:])
    entry = skill / "SKILL.md"
    check("install: SKILL.md lands in the skill directory", entry.is_file())
    named = sorted(set(re.findall(r"references/[A-Za-z0-9._-]+\.md", entry.read_text())))
    check("install: SKILL.md still names its references", bool(named), str(named))
    missing = [name for name in named if not (skill / name).is_file()]
    check("install: every reference SKILL.md names is installed", not missing, str(missing))


def main() -> int:
    if not HK.is_file():
        print("cannot find %s" % HK)
        return 2
    print("hawkeye-v2 selftest  root=%s" % RUNS)
    for test in (test_retry_then_success, test_stall_is_detected, test_stall_zero_disables_the_check,
                 test_skill_ships_every_file_it_points_at,
                 test_watch_file_exits_when_the_producer_writes,
                 test_agent_unreachable,
                 test_agent_responsive, test_phase_death_recovery,
                 test_milestone_and_wake_discipline, test_phase_exits_when_the_run_disappears,
                 test_stop_kills_the_job_command, test_stop_stops_the_phase,
                 test_stop_leaves_a_finished_run_alone,
                 test_installer_installs_every_reference,
                 test_kanban_watcher_reads_default_board,
                 test_named_board_is_not_the_default_board,
                 test_gate_check_reports_ungated_profiles,
                 test_kernel_branch_resolves_board_paths,
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
