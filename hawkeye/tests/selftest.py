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
import ast
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


def hk(args, conf, timeout=90, check=True, root=None):
    env = {**ENV, "HAWKEYE_V2_CONFIG": str(conf)}
    if root is not None:
        env["HAWKEYE_V2_ROOT"] = str(root)
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


def procs_running(marker: str) -> list[int]:
    """Pids whose argv contains *marker* -- the only way to see a reparented command.

    A survivor is reparented to init, so no pid the run recorded is trustworthy any more; the
    question is whether any process still runs this argv.
    """
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = ((entry / "cmdline").read_bytes()
                    .replace(b"\0", b" ")
                    .decode("utf-8", "replace"))
        except OSError:
            continue
        if marker in argv:
            found.append(int(entry.name))
    return found


def test_hang_kill_reaches_the_job_command() -> None:
    """A timeout/stall kill must kill the command, not just the ``hk job`` wrapper (#8).

    Two process groups are involved: the phase spawns the wrapper with start_new_session=True and
    the wrapper spawns the command the same way, so a kill aimed at state[job_pid] reaches the
    wrapper alone. Measured before the fix: the run reported gaveup while ``sleep`` kept running
    reparented to init, with job.pid already deleted -- a report about a job that is still alive
    and no way left on disk to find it.
    """
    # The marker is unique per suite instance: the leftover scan reads /proc, so two suites on one
    # host sharing "sleep 9773" make each other's live job look like an orphan and fail at random.
    cases = [("timeout", cfg("timeoutkill", attempts=1, attempt_timeout_seconds=2,
                             poll_seconds=0.2, stall_seconds=0), "sleep 9773.%d" % os.getpid()),
             ("stalled", cfg("stallkill", attempts=1, attempt_timeout_seconds=60,
                             poll_seconds=0.2, stall_seconds=0.8), "sleep 9774.%d" % os.getpid())]
    for why, conf, marker in cases:
        rid = "hangkill-%s" % why
        started = time.time()
        hk(["run", "--id", rid, "--", "/bin/bash", "-c", marker], conf, timeout=60)
        elapsed = time.time() - started
        result = read(rid, "result.json") or {}
        killed = [e for e in events(rid) if e["kind"] == "job_killed"]
        leftovers = procs_running(marker)
        check("%s: the run gave up on the hang" % why, result.get("reason") == "gaveup",
              str(result))
        check("%s: the kill names the wrapper and the command" % why,
              bool(killed) and killed[0].get("pid") and killed[0].get("wrapper")
              and killed[0]["pid"] != killed[0]["wrapper"], str(killed[:1]))
        check("%s: no process still runs the command after the run ended" % why, not leftovers,
              "still alive: %s" % leftovers)
        check("%s: the give-up does not wait longer than the hang" % why, elapsed < 15,
              "%.1fs" % elapsed)
        for pid in leftovers:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(os.getpgid(pid), signal.SIGKILL)


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


def test_nothing_is_written_at_the_hermes_home_root() -> None:
    """HERMES_HOME's top level is a short, documented list — a tool must not extend it.

    A user deciding what is safe to delete reads that directory; every extra entry is a decision
    they have to make twice (is this Hermes', is it mine, does deleting it break something).
    Runtime artefacts therefore go where Hermes itself puts tool caches: cache/<tool>/. The
    contract is one line — delete cache/hawkeye and the host is clean again.
    """
    home = BASE / "home-root"
    home.mkdir()
    conf = cfg("home")
    env = {**ENV, "HERMES_HOME": str(home), "HAWKEYE_V2_CONFIG": str(conf)}
    env.pop("HAWKEYE_V2_ROOT", None)                       # exercise the real default
    job = script("home.sh", "#!/bin/bash\necho hi\n")
    proc = subprocess.run([sys.executable, str(HK), "run", "--id", "home-1", "--", "/bin/bash", str(job)],
                          capture_output=True, text=True, timeout=90, env=env)
    top = sorted(p.name for p in home.iterdir())
    check("home root: hk writes nothing at the top of HERMES_HOME", top == ["cache"], str(top))
    check("home root: the run directory is under cache/hawkeye/runs",
          (home / "cache" / "hawkeye" / "runs" / "home-1" / "result.json").is_file(),
          proc.stdout[-200:] + proc.stderr[-200:])

    paths = subprocess.run([sys.executable, str(HK), "paths", "--json"],
                           capture_output=True, text=True, timeout=30, env=env)
    info = json.loads(paths.stdout or "{}")
    check("home root: hk paths reports the same directories it writes",
          info.get("runs") == str(home / "cache" / "hawkeye" / "runs"), paths.stdout[:200])


def test_config_resolution_prefers_xdg_and_keeps_legacy_working() -> None:
    """Config is the user's, not runtime state: it must not need a directory inside HERMES_HOME.

    The legacy rung exists so an existing install does not silently fall back to defaults (which
    would look like every tuning knob had been reset), not so new installs keep using it.
    """
    home = BASE / "home-cfg"
    xdg = BASE / "xdg"
    (home / "hawkeye-v2").mkdir(parents=True)
    (xdg / "hawkeye").mkdir(parents=True)
    legacy = home / "hawkeye-v2" / "hawkeye.toml"
    legacy.write_text("attempts = 2\n")
    xdg_conf = xdg / "hawkeye" / "hawkeye.toml"
    xdg_conf.write_text("attempts = 1\n")
    base = {"HERMES_HOME": str(home), "XDG_CONFIG_HOME": str(xdg)}

    def resolved(**over):
        env = {**ENV, **base}
        env.pop("HAWKEYE_V2_CONFIG", None)                  # clear the suite's own override
        env.update(over)                                    # ... unless the test passes one
        out = subprocess.run([sys.executable, str(HK), "paths", "--json"],
                             capture_output=True, text=True, timeout=30, env=env).stdout
        return json.loads(out or "{}").get("config")

    check("config: the XDG copy wins over a legacy file", resolved() == str(xdg_conf), resolved())
    xdg_conf.unlink()
    check("config: a legacy file still works, so existing tuning is not reset",
          resolved() == str(legacy), resolved())
    legacy.unlink()
    check("config: with neither present the XDG path is reported as the target",
          resolved() == str(xdg_conf), resolved())
    check("config: an explicit HAWKEYE_V2_CONFIG still wins everything",
          resolved(HAWKEYE_V2_CONFIG=str(BASE / "explicit.toml")) == str(BASE / "explicit.toml"))


def test_doctor_prunes_old_runs_but_keeps_evidence() -> None:
    """Housekeeping cannot depend on a human remembering to run `hk gc`.

    The run cache lives in HERMES_HOME/cache now, which is exactly the kind of directory that
    grows for the life of the host while nobody looks. The doctor sweep already runs every two
    minutes on hosts with no one watching, so retention belongs there -- incidents stay, because
    they are the evidence someone will come back for.
    """
    conf = cfg("prune", retention_days=7)
    now = time.time()
    def fake_run(name: str, finished: bool, age_days: float, incident: bool = False) -> Path:
        rd = RUNS / name
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "meta.json").write_text(json.dumps({"kind": "exec"}))
        state = {"finished": finished, "finished_at": now - age_days * 86400, "reports": 0}
        (rd / "state.json").write_text(json.dumps(state))
        if incident:
            (rd / "incident.json").write_text(json.dumps({"kind": "attempts_exhausted"}))
        return rd

    old = fake_run("prune-old", True, 10)
    old_incident = fake_run("prune-old-incident", True, 10, incident=True)
    young = fake_run("prune-young", True, 1)
    live = fake_run("prune-live", False, 30)
    # doctor exits 1 when an incident exists (that is its contract); the assertions below
    # are about the directories, not the exit code.
    hk(["doctor", "--fix"], conf, check=False)
    check("prune: a finished run past retention is removed", not old.exists())
    check("prune: an incident is kept as evidence", old_incident.exists())
    check("prune: a young run is kept", young.exists())
    check("prune: an unfinished run is never removed", live.exists())

    off = cfg("prune-off", retention_days=0)
    surviving = fake_run("prune-off-old", True, 99)
    hk(["doctor", "--fix"], off, check=False)
    check("prune: retention_days = 0 disables the sweep", surviving.exists())
    for rd in (old_incident, young, live, surviving):
        shutil.rmtree(rd, ignore_errors=True)


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


def test_report_creates_a_child_session() -> None:
    """A report must land where the user looks, not only in a file.

    The WebUI already renders child sessions (``parent_session_id``) nested under their parent --
    that is how subagent work appears -- so hawkeye writes background reports there through the
    kernel's own ``SessionDB``. Skips on a host without the checkout, like the other kernel test:
    there is nothing to assert about a kernel that is not installed.
    """
    src = os.environ.get("HAWKEYE_KERNEL_SRC", "").strip()
    if not src:
        print("  SKIP report: HAWKEYE_KERNEL_SRC is not set")
        return
    home = BASE / "report-home"
    home.mkdir()
    env = {**ENV, "HAWKEYE_KERNEL_SRC": src, "HERMES_HOME": str(home),
           "HAWKEYE_V2_CONFIG": str(cfg("report"))}

    seed = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import hermes_bootstrap\n"
        "from hermes_state import SessionDB\n"
        "db = SessionDB()\n"
        "db.create_session('parent-1', source='webui', display_name='parent')\n"
        "db.append_message('parent-1', 'user', 'do the work')\n"
        "db.close()\n"
        "print('SEEDED')\n" % src
    )
    # A throwaway HERMES_HOME makes hermes_bootstrap treat the install as stale and *rebuild* the
    # isolated runtime: measured 220 s and 1.7 GB per run on a host where the fixture home is not
    # current, which turned this test into a timeout FAIL (and left 1.3 G in the kept failure site)
    # instead of the SKIP the branch below is meant to provide (#12). The variable makes the import
    # side-effect free -- 0.57 s, 336 K -- and the timeout stays as a tripwire that SKIPs.
    env = {**env, "HERMES_DISABLE_LAZY_INSTALLS": "1"}
    try:
        seeded = subprocess.run([sys.executable, "-c", seed], capture_output=True, text=True,
                                env=env, timeout=180)
    except subprocess.TimeoutExpired:
        shutil.rmtree(home, ignore_errors=True)
        print("  SKIP report: seeding a session db timed out")
        return
    if "SEEDED" not in seeded.stdout:
        shutil.rmtree(home, ignore_errors=True)
        tail = (seeded.stderr or "").strip().splitlines()[-1:] or ["no output"]
        print("  SKIP report: cannot seed a session db (%s)" % tail[0])
        return

    body = BASE / "report-body.md"
    body.write_text("# report\nall good\n")
    proc = subprocess.run([sys.executable, str(HK), "report", "--parent", "parent-1",
                           "--file", str(body), "--title", "child report"],
                          capture_output=True, text=True, env=env, timeout=180)
    check("report: hk report succeeds", proc.returncode == 0, (proc.stderr or "")[-200:])

    reader = (
        "import json, sqlite3\n"
        "c = sqlite3.connect(%r)\n"
        "rows = c.execute(\"select id, parent_session_id, display_name from sessions "
        "where parent_session_id='parent-1'\").fetchall()\n"
        "msgs = c.execute(\"select count(*) from messages where session_id in "
        "(select id from sessions where parent_session_id='parent-1')\").fetchone()[0]\n"
        "print(json.dumps({'rows': rows, 'msgs': msgs}))\n" % str(home / "state.db")
    )
    out = subprocess.run([sys.executable, "-c", reader], capture_output=True, text=True,
                         timeout=60).stdout
    info = json.loads(out or "{}")
    rows = info.get("rows") or []
    check("report: a child session is created under the parent", len(rows) == 1, str(info))
    check("report: the child carries the report body", info.get("msgs") == 1, str(info))
    check("report: the child is named after the title",
          bool(rows) and rows[0][2] == "child report", str(rows[:1]))

    # A report is always a child. There is no mode that mints a top-level session, because an
    # unattended run that does that fills the sidebar with windows the user has to triage by hand.
    bad = subprocess.run([sys.executable, str(HK), "report", "--parent", "no-such-session",
                          "--file", str(body), "--title", "orphan attempt"],
                         capture_output=True, text=True, env=env, timeout=120)
    reader_after = (
        "import sqlite3\n"
        "c = sqlite3.connect(%r)\n"
        "print(c.execute(\"select count(*) from sessions\").fetchone()[0])\n"
        % str(home / "state.db")
    )
    total = subprocess.run([sys.executable, "-c", reader_after], capture_output=True,
                           text=True, timeout=60).stdout.strip()
    check("report: a missing parent is refused instead of becoming a new session",
          bad.returncode != 0 and total == "2", "rc=%s sessions=%s" % (bad.returncode, total))


def test_interpreter_probe_ignores_a_hostile_pythonpath() -> None:
    """The probe must judge the interpreter, not the caller's shell.

    Reported upstream against `f4bbaea`: a `PYTHONPATH` containing a `sitecustomize` that reports an
    old version made *every* candidate look unusable, `find()` returned None, and a host with a
    perfectly good Hermes runtime reported "found nothing usable". Cron wrappers and CI steps are
    exactly where a stray PYTHONPATH lives, so the fix is to launch the probe with the caller's
    environment minus the variables that change who the interpreter is.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "hk_interp", str(Path(__file__).resolve().parents[1] / "bin" / "_hk_interp.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    fake = BASE / "hostile-pythonpath"
    fake.mkdir()
    (fake / "sitecustomize.py").write_text(
        "import sys\nsys.version_info = (3, 7, 0, 'final', 0)\n")

    # Control: the fake really does skew version reporting in a child that inherits PYTHONPATH,
    # otherwise the assertions below would pass for the wrong reason.
    skewed = subprocess.run(
        [sys.executable, "-c", "import sys;print('%d.%d' % sys.version_info[:2])"],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(fake)}, timeout=60)
    check("interp probe: the hostile PYTHONPATH really skews a child that inherits it",
          skewed.stdout.strip() == "3.7", skewed.stdout.strip() + (skewed.stderr or "")[-120:])

    prev = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = str(fake)
    try:
        check("interp probe: a usable interpreter stays usable under a hostile PYTHONPATH",
              mod.usable(sys.executable) is True, "usable(%s) -> False" % sys.executable)
        check("interp probe: find() still returns a candidate",
              bool(mod.find()), "find() returned None")
    finally:
        if prev is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = prev


def test_issue_watch_reports_replies_on_closed_issues() -> None:
    """A reporter's follow-up lands on a *closed* issue, so `state=open` hides most conversation.

    This is the miss that made a real reply invisible: #14 was confirmed and closed, the reporter
    answered again on the closed issue, and the watcher reported "nothing new" for hours. Open
    issues keep their old rule (any movement is news); a closed issue is news only when somebody
    replied, because closing is the maintainer's own act and a never-seen closed issue is history.
    """
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "issue_watch", str(Path(__file__).resolve().parents[1] / "config" / "scripts" / "issue-watch.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    closed_with_reply = {"number": 14, "state": "closed", "title": "t", "html_url": "u",
                         "user": {"login": "reporter"}, "created_at": "a", "updated_at": "b",
                         "body": ""}
    check("issue-watch: a closed issue with a new reply is news",
          mod._is_news(closed_with_reply, {"updated_at": "older"}, [{"id": 2}]) is True)
    check("issue-watch: a closed issue nobody replied to is not news",
          mod._is_news(closed_with_reply, {"updated_at": "older"}, []) is False)
    check("issue-watch: a never-seen closed issue is history, not news",
          mod._is_news(closed_with_reply, {}, []) is False)
    check("issue-watch: a never-seen open issue is news",
          mod._is_news({**closed_with_reply, "state": "open"}, {}, []) is True)

    # Behaviour, not source: drive collect() with a fake API where the only closed issue moved.
    issues = [closed_with_reply, {**closed_with_reply, "number": 7, "state": "open"}]
    comments = {14: [{"id": 1, "user": {"login": "eSun-ws"}}, {"id": 2, "user": {"login": "reporter"}}],
                7: [{"id": 1, "user": {"login": "eSun-ws"}}]}

    def fake_get(url: str, token: str):
        if "/issues?" in url:
            return issues if "page=1" in url else []
        number = int(url.split("/issues/")[1].split("/")[0])
        return comments[number]

    real_get = mod._get
    mod._get = fake_get
    try:
        state = {"issues": {"14": {"updated_at": "older", "last_comment_id": 1},
                            "7": {"updated_at": closed_with_reply["updated_at"], "last_comment_id": 1}}}
        items = mod.collect("o/r", "tok", state, {"esun-ws"})
    finally:
        mod._get = real_get
    numbers = [i["number"] for i in items]
    check("issue-watch: the closed issue's reply reaches the report", 14 in numbers, str(numbers))
    check("issue-watch: an untouched open issue stays quiet", 7 not in numbers, str(numbers))
    check("issue-watch: the reply is carried in new_comments",
          bool(items) and any(c["id"] == 2 for c in items[0]["new_comments"]), str(items[:1])[:200])


def test_hk_survives_a_lying_pythonpath_across_the_reexec() -> None:
    """End-to-end: the *re-executed* hk must work under a hostile PYTHONPATH, not just the probe.

    `_probe_env()` cleaned the probe child, which made the module-level checks green while the CLI
    was still red: `_re_exec()` used `os.execv`, so the caller's environment went straight to the new
    interpreter and the re-executed hk rejected itself ("needs Python >= 3.11 (found 3.7)") on a host
    whose interpreter was fine. Reported with that exact asymmetry, hence an end-to-end assertion:
    run bin/hk as a subprocess with the lying PYTHONPATH and require rc=0.
    """
    fake = BASE / "lying-pythonpath"
    fake.mkdir()
    (fake / "sitecustomize.py").write_text(
        "import sys\nsys.version_info = (3, 7, 0, 'final', 0)\n")

    env = {**ENV, "PYTHONPATH": str(fake)}
    for key in ("HAWKEYE_PYTHON", "HAWKEYE_PY_REEXEC"):
        env.pop(key, None)

    proc = subprocess.run([sys.executable, str(HK), "gate-check"], capture_output=True,
                          text=True, env=env, timeout=180)
    out = (proc.stdout or "") + (proc.stderr or "")
    check("reexec: hk gate-check still runs under a lying PYTHONPATH", proc.returncode == 0,
          "rc=%s %s" % (proc.returncode, out[-300:]))
    check("reexec: the re-executed process does not reject itself",
          "needs Python >= 3.11" not in out, out[-300:])
    check("reexec: it says which interpreter it moved to", "which is too old; using" in out
          or "clean environment" in out, out[-300:])

    # The explicit override takes the same path (`_re_exec`), so it must survive the same lie.
    override = {**env, "HAWKEYE_PYTHON": sys.executable}
    proc2 = subprocess.run(["/usr/bin/python3", str(HK), "gate-check"], capture_output=True,
                           text=True, env=override, timeout=180)
    out2 = (proc2.stdout or "") + (proc2.stderr or "")
    check("reexec: an explicit HAWKEYE_PYTHON override survives the same lie",
          proc2.returncode == 0, "rc=%s %s" % (proc2.returncode, out2[-300:]))


def test_report_can_append_to_one_ledger_child() -> None:
    """A recurring producer must be able to keep writing into the *same* child session.

    Reporting is child-only, which fixed the sidebar clutter -- but a watcher that woke every five
    minutes and reported each cycle as a new child would put that clutter back one level down. A
    ledger is one child session that keeps accumulating entries, so the fifth update is still in the
    place the user learned to look. Appending to a top-level conversation is refused: that is a
    conversation the user owns, not an output sink.
    """
    src = os.environ.get("HAWKEYE_KERNEL_SRC", "").strip()
    if not src:
        print("  SKIP ledger: HAWKEYE_KERNEL_SRC is not set")
        return
    home = BASE / "ledger-home"
    home.mkdir()
    env = {**ENV, "HAWKEYE_KERNEL_SRC": src, "HERMES_HOME": str(home),
           "HAWKEYE_V2_CONFIG": str(cfg("ledger")), "HERMES_DISABLE_LAZY_INSTALLS": "1"}

    seed = (
        "import sys\n"
        "sys.path.insert(0, %r)\n"
        "import hermes_bootstrap\n"
        "from hermes_state import SessionDB\n"
        "db = SessionDB()\n"
        "db.create_session('parent-1', source='webui', display_name='parent')\n"
        "db.create_session('ledger-1', source='hawkeye', parent_session_id='parent-1',"
        " display_name='issue ledger')\n"
        "db.append_message('ledger-1', 'assistant', 'ledger opened')\n"
        "db.close()\n"
        "print('SEEDED')\n" % src
    )
    try:
        seeded = subprocess.run([sys.executable, "-c", seed], capture_output=True, text=True,
                                env=env, timeout=180)
    except subprocess.TimeoutExpired:
        shutil.rmtree(home, ignore_errors=True)
        print("  SKIP ledger: seeding a session db timed out")
        return
    if "SEEDED" not in seeded.stdout:
        shutil.rmtree(home, ignore_errors=True)
        tail = (seeded.stderr or "").strip().splitlines()[-1:] or ["no output"]
        print("  SKIP ledger: cannot seed a session db (%s)" % tail[0])
        return

    def append(where: str, text: str, title: str) -> subprocess.CompletedProcess:
        body = BASE / ("ledger-%s-%s.md" % (where, title))
        body.write_text(text)
        return subprocess.run([sys.executable, str(HK), "report", "--into", where,
                               "--file", str(body), "--title", title],
                              capture_output=True, text=True, env=env, timeout=180)

    first = append("ledger-1", "first finding\n", "cycle-a")
    second = append("ledger-1", "second finding\n", "cycle-b")
    check("ledger: appending to a child succeeds", first.returncode == 0 and second.returncode == 0,
          (first.stderr or second.stderr or "")[-200:])

    # One JSON line, not line-separated prints: message content contains newlines, so splitlines()
    # would hand back the first line of the third value and silently truncate the assertion input.
    reader = (
        "import json, sqlite3\n"
        "c = sqlite3.connect(%r)\n"
        "print(json.dumps({\n"
        "  'kids': c.execute(\"select count(*) from sessions where parent_session_id='parent-1'\")"
        ".fetchone()[0],\n"
        "  'msgs': [r[0] for r in c.execute(\"select content from messages where "
        "session_id='ledger-1' order by id\")],\n"
        "}))\n" % str(home / "state.db")
    )
    info2 = json.loads(subprocess.run([sys.executable, "-c", reader], capture_output=True,
                                      text=True, timeout=60).stdout or "{}")
    kids = info2.get("kids")
    entries = info2.get("msgs") or []
    blob = "\n".join(entries)
    check("ledger: two cycles did not create a second child", kids == 1, "children=%s" % kids)
    check("ledger: both entries live in the same session", len(entries) == 3,
          "messages=%s" % len(entries))
    check("ledger: entries are kept in order and stamped",
          "first finding" in blob and "second finding" in blob
          and blob.index("first finding") < blob.index("second finding")
          and "cycle-a" in blob and "cycle-b" in blob
          # each entry carries its own UTC stamp, so two cycles are told apart by more than order
          and len(re.findall(r"\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\]", blob)) == 2, blob[:200])

    top = append("parent-1", "should not land here\n", "top-attempt")
    after = subprocess.run([sys.executable, "-c",
                            "import sqlite3;c=sqlite3.connect(%r);"
                            "print(c.execute(\"select count(*) from messages where session_id='parent-1'\")"
                            ".fetchone()[0])" % str(home / "state.db")],
                           capture_output=True, text=True, timeout=60).stdout.strip()
    check("ledger: appending to a top-level session is refused",
          top.returncode != 0 and after == "0", "rc=%s msgs=%s" % (top.returncode, after))

    gone = append("no-such-session", "nowhere\n", "missing-attempt")
    check("ledger: appending to a missing session is refused", gone.returncode != 0,
          "rc=%s" % gone.returncode)
    shutil.rmtree(home, ignore_errors=True)


def test_state_writes_cannot_resurrect_a_finished_run() -> None:
    """Progress writes and the stop verdict must not interleave into a resurrected run.

    The phase and `hk stop` are different processes writing the same document. Without one lock
    around "may I write?" plus the write, a stop landing in that gap is overwritten by a stale
    copy: the run comes back, hits its timeout, and reports -- the wake the operator cancelled
    (#7). Hammering both writers is what makes the window big enough to see.
    """
    import importlib
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
    store = importlib.import_module("hawkeye.store")

    rd = BASE / "state-race"
    rd.mkdir()
    (rd / "meta.json").write_text(json.dumps({"kind": "exec"}))
    store.write_json(rd / "state.json", {"finished": False, "attempts_done": 0})

    writer = (
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "from hawkeye import store\n"
        "rd = __import__('pathlib').Path(%r)\n"
        "end = time.time() + 1.2\n"
        "n = 0\n"
        "while time.time() < end:\n"
        "    n += 1\n"
        "    store.update_json(rd / 'state.json', {'attempts_done': n})\n"
        "print(n)\n" % (str(Path(__file__).resolve().parents[1] / "lib"), str(rd))
    )
    # A test that cannot fail is worse than a missing one: this writer used to call a function that
    # does not exist, so the child died before its first write, no concurrency ever happened, and
    # both assertions passed on a state file nobody had touched (#11). The child's own liveness is
    # therefore part of the assertion: it must exit 0, write silently, and have looped many times.
    proc = subprocess.Popen([sys.executable, "-c", writer], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    time.sleep(0.4)
    store.update_json(rd / "state.json", {"finished": True, "finish_reason": "stopped"})
    out, err = proc.communicate(timeout=30)
    writes = int(out.strip() or 0) if out.strip().isdigit() else 0
    check("state race: the writer really ran", proc.returncode == 0 and not err.strip()
          and writes > 100,
          "rc=%s writes=%s stderr=%r" % (proc.returncode, writes, err.strip()[:200]))

    final = store.read_json(rd / "state.json", {}) or {}
    check("state race: the stop verdict survives concurrent progress writes",
          final.get("finished") is True and final.get("finish_reason") == "stopped", str(final))
    store.update_json(rd / "state.json", {"attempts_done": 99})
    after = store.read_json(rd / "state.json", {}) or {}
    check("state race: a write after the verdict keeps the verdict",
          after.get("finished") is True and after.get("finish_reason") == "stopped", str(after))


def test_per_run_report_interval_overrides_the_host_setting() -> None:
    """A quiet watcher must not inherit a build job's progress cadence.

    ``report_interval_seconds`` is a host knob (30 minutes in a production config): a job that
    prints steadily wants it, a watcher that prints nothing for hours does not -- it turns into
    "wake the session every 30 minutes to say nothing happened", and every one of those wakes also
    ends the phase and orphans the job. ``hk run --report-interval`` scopes the knob to the run,
    and 0 disables the periodic report entirely.
    """
    conf = cfg("ri", report_interval_seconds=999, attempts=1)
    slow = script("ri.sh", "#!/bin/bash\nsleep 6\n")
    rid = "ri-fast"
    hk(["run", "--id", rid, "--report-interval", "0.6", "--", "/bin/bash", str(slow)], conf)
    # A "running" report is not a terminal state, so there is no result.json: the evidence is the
    # report it emitted and the counter it bumped.
    state = read(rid, "state.json") or {}
    report_path = RUNS / rid / "report.md"
    emitted = report_path.read_text() if report_path.is_file() else ""
    check("interval: a per-run override reports early",
          int(state.get("reports") or 0) == 1 and "running" in emitted, str(state))

    rid2 = "ri-off"
    env = {**ENV, "HAWKEYE_V2_CONFIG": str(conf)}
    proc = subprocess.Popen([sys.executable, str(HK), "run", "--id", rid2,
                             "--report-interval", "0", "--", "/bin/bash", str(slow)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    time.sleep(2.0)
    still_running = proc.poll() is None
    check("interval: 0 disables the periodic report", still_running,
          "the phase exited on the timer instead of staying quiet")
    hk(["stop", "--run", rid2], conf)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


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
    # attempt_start, and paid off as a report one attempt later. The window is generous on
    # purpose -- a loaded host once made 4s too tight and produced a false red, and a flaky
    # assertion here is worse than a slow one: it either hides a regression or teaches people
    # to re-run the suite until it passes.
    ended = wait_for(lambda: phase.poll() is not None, 10.0)
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


def test_redirected_install_keeps_the_host_units_and_migrates_runs() -> None:
    """Two silent ways an install/upgrade turns the doctor off (#9).

    (1) `UNIT_DIR` used to ignore `--hermes-home`/`--config`, so a verification install rewrote the
    *production* unit to point at a fixture -- the timer then fired 203/EXEC every two minutes for
    an hour with nothing noticing. (2) The layout migration moved the config but not
    `<home>/hawkeye-v2/runs`, so a phase that was already running became invisible to `status`
    and `doctor` -- the one run that needs supervision.

    Both are asserted in one install: the host unit dir stays untouched, and a finished legacy run
    moves while a live one is left where it is and named in the output.
    """
    try:
        managed = subprocess.run(["systemctl", "--user", "show-environment"],
                                 capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        managed = False
    if not managed:
        print("  SKIP install-migrate: no user systemd manager for the daemon-reload step")
        return

    home = BASE / "mig-home"
    xdg = BASE / "mig-xdg"
    legacy = home / "hawkeye-v2" / "runs"
    done = legacy / "old-done"
    live = legacy / "old-live"
    done.mkdir(parents=True)
    live.mkdir(parents=True)
    (done / "meta.json").write_text(json.dumps({"kind": "exec", "entry": "/bin/true"}))
    (done / "state.json").write_text(json.dumps({"finished": True, "finished_at": time.time()}))
    (live / "meta.json").write_text(json.dumps({"kind": "exec", "entry": "/bin/true"}))
    (live / "state.json").write_text(json.dumps({"finished": False}))

    installer = Path(__file__).resolve().parents[1] / "install.sh"
    proc = subprocess.run(["bash", str(installer), "--no-enable", "--skip-skill",
                           "--hermes-home", str(home), "--config", str(home / "hawkeye.toml")],
                          capture_output=True, text=True, timeout=120,
                          env={**ENV, "HERMES_HOME": str(home), "XDG_CONFIG_HOME": str(xdg)})
    out = proc.stdout + proc.stderr
    check("install: a redirected install succeeds", proc.returncode == 0, out.strip()[-300:])
    check("install: the host unit dir is untouched",
          not (xdg / "systemd" / "user" / "hawkeye-doctor.service").exists(),
          str(xdg / "systemd" / "user"))
    check("install: units land under the redirected home",
          (home / "systemd" / "user" / "hawkeye-doctor.service").is_file())
    check("install: a finished legacy run is migrated",
          (home / "cache" / "hawkeye" / "runs" / "old-done" / "meta.json").is_file())
    check("install: a live legacy run is left in place", live.is_dir())
    check("install: the output names the live run it left", "old-live" in out, out[-300:])


def test_doctor_does_not_flag_a_consumed_report() -> None:
    """A notifier's normal lifecycle must not look like an abandoned report (#10).

    The check was "a report exists and the lease is old", which flags every healthy notifier one
    grace window after it ends: report at T, agent leases at T+3s, and 15 minutes later the lease
    *is* old. "Consumed" has to mean the lease is at or after the report, not that it is recent.
    """
    conf = cfg("consumed", wake_grace_seconds=0.3)
    now = time.time()
    rd = RUNS / "consumed-1"
    rd.mkdir(parents=True, exist_ok=True)
    (rd / "meta.json").write_text(json.dumps({"kind": "exec"}))
    (rd / "state.json").write_text(json.dumps({
        "finished": True, "finished_at": now - 900, "reports": 1,
        "wakes": 1, "last_report_at": now - 905, "last_reason": "done"}))
    (rd / "lease.json").write_text(json.dumps({"agent_seen": now - 902, "by": "agent"}))
    result = hk(["doctor", "--fix", "--json"], conf, check=False)
    row = next((r for r in json.loads(result.stdout or "[]") if r["run"] == "consumed-1"), {})
    check("consumed: a report with a later lease raises no incident", row.get("incident") is False,
          str(row))
    check("consumed: no incident file is written", not (rd / "incident.json").is_file())

    missed = RUNS / "missed-1"
    missed.mkdir(parents=True, exist_ok=True)
    (missed / "meta.json").write_text(json.dumps({"kind": "exec"}))
    (missed / "state.json").write_text(json.dumps({
        "finished": True, "finished_at": now - 900, "reports": 1,
        "wakes": 1, "last_report_at": now - 905, "last_reason": "done"}))
    (missed / "lease.json").write_text(json.dumps({"agent_seen": now - 4000, "by": "agent"}))
    result = hk(["doctor", "--fix", "--json"], conf, check=False)
    row = next((r for r in json.loads(result.stdout or "[]") if r["run"] == "missed-1"), {})
    check("consumed: a report nobody leased after still raises an incident",
          row.get("incident") is True, str(row))


def test_gc_waits_for_a_report_to_be_delivered() -> None:
    """The wake is queued and arrives after the run ends, so the sweep must not win that race.

    Measured in the field: a notifier reported, `hk gc --days 0 --force` removed the directory
    eleven seconds later, and the notification then pointed at nothing.
    """
    conf = cfg("deliver", delivery_grace_seconds=300)
    now = time.time()
    fresh = RUNS / "deliver-fresh"
    fresh.mkdir(parents=True, exist_ok=True)
    (fresh / "meta.json").write_text(json.dumps({"kind": "exec"}))
    (fresh / "state.json").write_text(json.dumps({
        "finished": True, "finished_at": now - 11, "reports": 1, "last_report_at": now - 11}))
    (fresh / "lease.json").write_text(json.dumps({"agent_seen": 0}))
    hk(["gc", "--days", "0", "--force"], conf)
    check("deliver: a report that has not been acknowledged is not swept", fresh.is_dir())

    old = RUNS / "deliver-old"
    old.mkdir(parents=True, exist_ok=True)
    (old / "meta.json").write_text(json.dumps({"kind": "exec"}))
    (old / "state.json").write_text(json.dumps({
        "finished": True, "finished_at": now - 4000, "reports": 1, "last_report_at": now - 4000}))
    (old / "lease.json").write_text(json.dumps({"agent_seen": 0}))
    hk(["gc", "--days", "0", "--force"], conf)
    check("deliver: once the window has passed, the sweep proceeds", not old.is_dir())


def test_the_sweep_shares_the_gc_predicate() -> None:
    """A grace window only one caller applies is not a grace window (#10, follow-on).

    ``store.prunable_runs`` is documented as one answer to "may this directory go", and
    ``doctor --fix`` prunes through it -- but the delivery grace was added inside ``cmd_gc``, so
    the two callers disagreed. With ``retention_days`` at its 7-day default the disagreement
    cannot bite, which is why this is latent rather than live; the sweep is still the path that
    runs every two minutes on a host nobody is watching, and it deleted the one run whose report
    was still queued.
    """
    shared = BASE / "predicate-runs"
    shared.mkdir(parents=True, exist_ok=True)
    conf = cfg("predicate", retention_days=0.0007, delivery_grace_seconds=300)
    now = time.time()

    def undelivered() -> Path:
        rd = shared / "undelivered"
        rd.mkdir(parents=True, exist_ok=True)
        (rd / "meta.json").write_text(json.dumps({"kind": "exec"}))
        (rd / "state.json").write_text(json.dumps({
            "finished": True, "finished_at": now - 120, "reports": 1,
            "last_report_at": now - 120}))
        (rd / "lease.json").write_text(json.dumps({"agent_seen": 0}))
        return rd

    rd = undelivered()
    gc_out = hk(["gc", "--days", "0.0007", "--force"], conf, root=shared)
    check("predicate: gc keeps a report that was never delivered", rd.is_dir(), gc_out.stdout)
    check("predicate: gc names the reason", "not delivered" in gc_out.stdout, gc_out.stdout)

    doctor_out = hk(["doctor", "--fix"], conf, root=shared, check=False).stdout
    check("predicate: the doctor sweep keeps the same directory", rd.is_dir(),
          "the sweep deleted what gc had just refused to delete: %r" % doctor_out.strip())
    check("predicate: the sweep does not report a zero-day prune",
          "older than 0 days" not in doctor_out, doctor_out.strip())

    # A grace that never expires is not a grace: a run that finished long ago and whose report
    # *was* leased must still be swept, or "delivery grace" has quietly become "never delete".
    late = shared / "delivered"
    late.mkdir(parents=True, exist_ok=True)
    (late / "meta.json").write_text(json.dumps({"kind": "exec"}))
    (late / "state.json").write_text(json.dumps({
        "finished": True, "finished_at": now - 4000, "reports": 1,
        "last_report_at": now - 4000}))
    (late / "lease.json").write_text(json.dumps({"agent_seen": now - 3990}))
    hk(["doctor", "--fix"], conf, root=shared, check=False)
    check("predicate: past the window the sweep still proceeds", not late.is_dir(),
          "the grace turned the sweep off instead of delaying it")
    check("predicate: the kept run is still there to be audited", rd.is_dir())


def test_a_spelled_out_default_path_is_not_a_redirect() -> None:
    """`--config` at the *default* path must not move the host's units (#9, follow-on).

    "Any path-moving flag marks the install as redirected" read a documented one-liner that names
    the real config -- it lives outside HERMES_HOME on this host, so the flag is always passed --
    as a fixture install, and derived the unit dir under HERMES_HOME. The doctor then stops being
    started and nothing says why: the same failure mode as #9, one flag further in.

    The baseline is now this shell's own HOME/XDG rather than the values the script honours, so a
    *non-default home* is a redirect whoever moved it (flag or environment) -- which means this
    test supplies a fake HOME and spells the defaults out, instead of relying on a non-default
    HERMES_HOME being treated as ordinary.
    """
    installer = Path(__file__).resolve().parents[1] / "install.sh"
    fake_home = BASE / "spelled-home"
    xdg = BASE / "spelled-xdg"
    fresh_home = fake_home / ".hermes"
    fresh_home.mkdir(parents=True, exist_ok=True)
    default_conf = xdg / "hawkeye" / "hawkeye.toml"
    env = {**ENV, "HOME": str(fake_home), "XDG_CONFIG_HOME": str(xdg)}
    env.pop("HERMES_HOME", None)
    env.pop("HAWKEYE_V2_CONFIG", None)
    env.pop("HAWKEYE_KERNEL_SRC", None)   # a caller's checkout would make the probe refuse

    spelled = subprocess.run(["bash", str(installer), "--print-only", "--no-enable",
                              "--skip-skill", "--hermes-home", str(fresh_home),
                              "--config", str(default_conf)],
                             capture_output=True, text=True, timeout=120, env=env)
    out = spelled.stdout + spelled.stderr
    check("spelled: the spelled-out default install succeeds", spelled.returncode == 0,
          out.strip()[-300:])
    check("spelled: units stay in the default unit dir",
          "units     : %s/systemd/user" % xdg in out, out.strip()[-400:])
    check("spelled: nothing claims to be redirected", "non-default" not in out,
          out.strip()[-400:])

    moved = subprocess.run(["bash", str(installer), "--print-only", "--no-enable",
                            "--skip-skill", "--hermes-home", str(fresh_home),
                            "--config", str(BASE / "other" / "hawkeye.toml")],
                           capture_output=True, text=True, timeout=120, env=env)
    out = moved.stdout + moved.stderr
    check("spelled: a config outside the default still redirects the units",
          "units     : %s/systemd/user" % fresh_home in out and "non-default" in out,
          out.strip()[-400:])


def test_env_driven_install_is_also_a_redirect() -> None:
    """A fixture exported into the environment is still a fixture (#9, second round).

    The redirect test used to compare a *flag* against the value the environment had already
    supplied, so an install driven by `HERMES_HOME=... ./install.sh` never looked redirected and
    wrote fixture paths into the host's real unit dir -- #9's failure with no flag involved, which
    is exactly how a harness sets paths up.

    And because `systemd-analyze --user unit-paths` is authoritative, enabling a timer from a
    directory it does not read is refused instead of installing something that never runs.
    """
    installer = Path(__file__).resolve().parents[1] / "install.sh"
    home = BASE / "env-home"
    xdg = BASE / "env-xdg"
    cfg_path = BASE / "env-cfg" / "hawkeye.toml"
    env = {**ENV, "HERMES_HOME": str(home), "HAWKEYE_V2_CONFIG": str(cfg_path),
           "XDG_CONFIG_HOME": str(xdg)}
    env.pop("HAWKEYE_KERNEL_SRC", None)

    printed = subprocess.run(["bash", str(installer), "--print-only", "--no-enable", "--skip-skill"],
                             capture_output=True, text=True, timeout=120, env=env)
    out = printed.stdout + printed.stderr
    check("env install: units follow the redirected home",
          "units     : %s/systemd/user" % home in out, out.strip()[-400:])
    check("env install: the host unit dir is not named",
          "units     : %s/systemd/user" % xdg not in out, out.strip()[-400:])

    enabled = subprocess.run(["bash", str(installer), "--skip-skill"],
                             capture_output=True, text=True, timeout=120, env=env)
    check("env install: enabling from a dir systemd does not read is refused",
          enabled.returncode != 0 and "unit-paths" in (enabled.stdout + enabled.stderr),
          (enabled.stdout + enabled.stderr).strip()[-300:])
    check("env install: the refused install wrote no unit into the host dir",
          not (xdg / "systemd" / "user" / "hawkeye-doctor.service").exists())


def test_set_cannot_overwrite_a_stop_verdict() -> None:
    """A progress write must not resurrect a run somebody just stopped (#7's family).

    The field artefact was `attempt_failed -> run_stopped -> attempt_start -> gaveup -> report`
    with `finish_reason=gaveup`: the stop's verdict was overwritten by the phase's in-memory copy,
    because `_set` checked "is it finished?" and then wrote, as two steps. Forcing that
    interleaving here is a child process hammering `_set` while the verdict lands; the check and
    the write now share the lock `hk stop` uses, so the verdict cannot be lost.
    """
    import importlib
    lib = str(Path(__file__).resolve().parents[1] / "lib")
    sys.path.insert(0, lib)
    store = importlib.import_module("hawkeye.store")

    conf = cfg("setrace")
    rid = "setrace-1"
    rd = RUNS / rid
    rd.mkdir(parents=True)
    (rd / "meta.json").write_text(json.dumps({"kind": "exec", "entry": "/bin/true",
                                              "created_at": time.time()}))
    store.write_json(rd / "state.json", {"finished": False, "attempts_done": 0})

    child = (
        "import sys, time\n"
        "sys.path.insert(0, %r)\n"
        "from hawkeye import config as cm, phase as pm\n"
        "cfg = cm.load(%r)\n"
        "p = pm.Phase(cfg, %r)\n"
        "n = 0\n"
        "try:\n"
        "    end = time.time() + 2.0\n"
        "    while time.time() < end:\n"
        "        n += 1\n"
        "        p._set(attempts_done=n)\n"
        "except pm._Stopped:\n"
        "    print('STOPPED', n)\n"
        "else:\n"
        "    print('LOOPED', n)\n"
        % (lib, str(conf), rid)
    )
    proc = subprocess.Popen([sys.executable, "-c", child], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, env=ENV)
    time.sleep(0.5)
    store.update_json(rd / "state.json", {"finished": True, "finish_reason": "stopped"})
    out, err = proc.communicate(timeout=30)
    final = store.read_json(rd / "state.json", {}) or {}
    # A hand-made run must not outlive this test: the suite counts and scans the shared root.
    shutil.rmtree(rd, ignore_errors=True)

    check("set race: the stop verdict survives a hammering progress writer",
          final.get("finished") is True and final.get("finish_reason") == "stopped", str(final))
    check("set race: the writer exited cleanly and reported what it saw",
          proc.returncode == 0 and out.strip().split()[:1] in (["STOPPED"], ["LOOPED"]),
          "rc=%s out=%r err=%r" % (proc.returncode, out.strip()[:80], err.strip()[:160]))
    check("set race: a writer that saw the stop raised instead of continuing",
          out.strip().startswith("STOPPED"), "out=%r err=%r" % (out.strip()[:80], err.strip()[:200]))


def test_status_shows_a_withdrawn_incident_as_withdrawn() -> None:
    """`status` must not report a resolved incident as open (#14).

    `d28b8e1` withdraws an `agent_unreachable` in place, and doctor stops counting it; `status` kept
    reading the raw file, so the one command a human reads first disagreed with the one that had
    already closed the incident. The note now says "(withdrawn)" and the row carries an explicit
    `incident_open`, while the raw document stays in the row because `withdrawn_at` is the audit
    trail.

    Also pins the deliberate other half: a run whose incident was withdrawn is still kept as
    evidence by the sweep, since the file is the record of a resolved alarm.
    """
    conf = cfg("status14")
    root = BASE / "status14-runs"
    root.mkdir()
    now = time.time()
    rd = root / "handled"
    rd.mkdir()
    (rd / "meta.json").write_text(json.dumps({"kind": "exec", "entry": "/bin/true"}))
    # A *finished* run, so the sweep's reason is "has incident" rather than "not finished": the
    # question here is whether a withdrawn incident still counts as evidence.
    (rd / "state.json").write_text(json.dumps({
        "finished": True, "finished_at": now - 600, "finish_reason": "done",
        "reports": 1, "last_report_at": now - 600, "wakes": 1}))
    (rd / "lease.json").write_text(json.dumps({"agent_seen": now - 597, "by": "agent"}))
    (rd / "incident.json").write_text(json.dumps({
        "reason": "agent_unreachable", "at": "2026-01-01T00:00:00Z", "detail": "opened too early"}))

    hk(["doctor", "--fix"], conf, check=False, root=root)
    rows = json.loads(hk(["status", "--all", "--json"], conf, root=root).stdout)
    row = next((r for r in rows if r["run"] == "handled"), {})
    check("status: a withdrawn incident is not reported as open", row.get("incident_open") is False,
          str(row)[:200])
    check("status: the note says so in words too",
          "(withdrawn)" in (row.get("note") or ""), str(row.get("note")))
    check("status: the raw document is kept as the audit trail",
          bool((row.get("incident") or {}).get("withdrawn_at")), str(row.get("incident"))[:160])

    text = hk(["status", "--all"], conf, root=root).stdout
    check("status: the table shows the withdrawn marker", "incident=agent_unreachable (withdrawn)" in text,
          text[-200:])

    gc_out = hk(["gc", "--days", "0"], conf, root=root).stdout
    check("gc: a withdrawn incident still keeps the run as evidence",
          rd.is_dir() and "has incident" in gc_out, gc_out[-160:])


def test_hk_finds_a_modern_interpreter_for_an_old_host() -> None:
    """hk must run on a host whose system python is 3.6 (Ubuntu 18.04, Debian 10).

    The entry point alone cannot save those hosts: it is started by ``/usr/bin/env python3``, so it
    has to *run* under the old interpreter to say anything. That makes two requirements, both
    guarded here -- the launcher and its helper stay parseable by 3.6, and the search finds a usable
    interpreter inside Hermes, by launching each candidate rather than trusting the path.
    """
    import importlib.util
    repo = Path(__file__).resolve().parents[1]
    for name in ("bin/hk", "bin/_hk_interp.py"):
        src = (repo / name).read_text()
        try:
            ast.parse(src, feature_version=(3, 6))
            ok, why = True, ""
        except SyntaxError as exc:
            ok, why = False, str(exc)
        check("%s stays parseable by python 3.6" % name, ok, why)

    spec = importlib.util.spec_from_file_location("hkip", repo / "bin" / "_hk_interp.py")
    assert spec is not None and spec.loader is not None
    interp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(interp)

    home = BASE / "old-host-home"
    good = home / "tools" / "python-3.14.9" / "bin" / "python3"
    good.parent.mkdir(parents=True)
    good.symlink_to("/usr/bin/python3")
    broken = home / "installs" / "x" / "environments" / "y" / "venv" / "bin" / "python3"
    broken.parent.mkdir(parents=True)
    broken.symlink_to("/nonexistent/python3")

    env = {**os.environ, "HERMES_HOME": str(home)}
    listed = interp.candidates(env)
    check("interp: the tools interpreter is considered first",
          listed and listed[0] == str(good), str(listed))
    check("interp: a broken venv is listed but rejected",
          str(broken) in listed and not interp.usable(str(broken)), str(broken))

    picked = interp.find(env, exclude=sys.executable)
    same_python = os.path.realpath("/usr/bin/python3") == os.path.realpath(sys.executable)
    if same_python:
        print("  SKIP interp: /usr/bin/python3 is the running interpreter; nothing to pick")
    else:
        check("interp: a usable candidate is returned, not just the newest path",
              picked == str(good), str(picked))
    check("interp: the search names both locations in the failure text",
          any("tools/python-*" in line for line in interp.describe_searched(env))
          and any("installs/*/environments" in line for line in interp.describe_searched(env)))


def test_report_grace_is_measured_from_the_report() -> None:
    """A long-running run's report must not look like an abandoned one two seconds later (#13).

    The grace answers "how long may this report sit unclaimed". Measuring it from the lease meant a
    watcher that had legitimately been quiet for hours (lease_age in the thousands) tripped
    `agent_unreachable` the moment it reported -- before the wake turn could possibly lease -- and
    the incident then kept doctor at exit 1 on a run that was handled.
    """
    conf = cfg("grace13", wake_grace_seconds=2.0)
    root = BASE / "grace13-runs"
    root.mkdir()
    now = time.time()

    def fixture(name: str, last_report_age: float, lease_age: float) -> Path:
        rd = root / name
        rd.mkdir()
        (rd / "meta.json").write_text(json.dumps({"kind": "exec", "entry": "/bin/true"}))
        (rd / "state.json").write_text(json.dumps({
            "finished": False, "reports": 1, "last_report_at": now - last_report_age,
            "wakes": 1, "last_reason": "running"}))
        (rd / "lease.json").write_text(json.dumps({"agent_seen": now - lease_age, "by": "agent"}))
        return rd

    fresh = fixture("fresh-report", last_report_age=1, lease_age=5000)
    stale = fixture("stale-report", last_report_age=100, lease_age=5000)
    result = hk(["doctor", "--fix", "--json"], conf, check=False, root=root)
    rows = {r["run"]: r for r in json.loads(result.stdout or "[]")}
    check("grace from report: a long-quiet run that just reported raises nothing",
          rows.get("fresh-report", {}).get("incident") is False, str(rows.get("fresh-report")))
    check("grace from report: the same run does raise once the report itself is stale",
          rows.get("stale-report", {}).get("incident") is True, str(rows.get("stale-report")))
    check("grace from report: no incident file for the fresh one", not (fresh / "incident.json").is_file())
    check("grace from report: an incident file for the stale one", (stale / "incident.json").is_file())


def test_a_consumed_report_withdraws_the_incident() -> None:
    """Once the report reaches its agent, an open incident describes a moment that has passed."""
    conf = cfg("withdraw13", wake_grace_seconds=0.3)
    root = BASE / "withdraw13-runs"
    root.mkdir()
    now = time.time()
    rd = root / "handled"
    rd.mkdir()
    (rd / "meta.json").write_text(json.dumps({"kind": "exec", "entry": "/bin/true"}))
    (rd / "state.json").write_text(json.dumps({
        "finished": False, "reports": 1, "last_report_at": now - 600, "wakes": 1}))
    (rd / "lease.json").write_text(json.dumps({"agent_seen": now - 597, "by": "agent"}))
    (rd / "incident.json").write_text(json.dumps({
        "reason": "agent_unreachable", "at": "2026-01-01T00:00:00Z", "detail": "opened too early"}))

    result = hk(["doctor", "--fix", "--json"], conf, check=False, root=root)
    rows = {r["run"]: r for r in json.loads(result.stdout or "[]")}
    doc = json.loads((rd / "incident.json").read_text())
    check("withdraw: the row no longer reports an incident", rows.get("handled", {}).get("incident") is False,
          str(rows.get("handled")))
    check("withdraw: the file is kept and marked withdrawn",
          bool(doc.get("withdrawn_at")) and doc.get("reason") == "agent_unreachable", json.dumps(doc)[:160])
    check("withdraw: doctor exits 0 once nothing is open", result.returncode == 0, str(result.returncode))


def test_the_sweep_and_gc_share_one_delivery_grace() -> None:
    """#10(a): one key, one default, one reader -- measured on a root of its own.

    The test prunes, so it must not run in the shared run root: `gc --days 0.0007 --force` there
    swept the finished fixtures that `status` asserts on, and the failure looked like a status bug
    rather than a test that deleted its neighbours' data.
    """
    import importlib
    lib = str(Path(__file__).resolve().parents[1] / "lib")
    sys.path.insert(0, lib)
    store = importlib.import_module("hawkeye.store")

    check("grace: the default is the documented 300 s", store.delivery_grace({}) == 300.0,
          repr(store.delivery_grace({})))
    check("grace: a configured value wins", store.delivery_grace({"delivery_grace_seconds": 60}) == 60.0)
    check("grace: a null value falls back instead of raising",
          store.delivery_grace({"delivery_grace_seconds": None}) == 300.0)
    owners = [p.name for p in (Path(lib) / "hawkeye").glob("*.py")
              if '"delivery_grace_seconds"' in p.read_text() or "'delivery_grace_seconds'" in p.read_text()]
    check("grace: only store.py and config.py name the key", sorted(owners) == ["config.py", "store.py"],
          str(owners))

    conf = cfg("sweep", retention_days=0.0007, delivery_grace_seconds=300)   # ~60s retention
    sweep_root = BASE / "sweep-runs"
    sweep_root.mkdir()
    rd = sweep_root / "sweep-undelivered"
    rd.mkdir()
    now = time.time()
    (rd / "meta.json").write_text(json.dumps({"kind": "exec", "entry": "/bin/true"}))
    (rd / "state.json").write_text(json.dumps({
        "finished": True, "finished_at": now - 120, "reports": 1, "last_report_at": now - 120}))
    (rd / "lease.json").write_text(json.dumps({"agent_seen": 0}))

    hk(["doctor", "--fix"], conf, check=False, root=sweep_root)
    check("sweep: doctor does not prune a report that is still undelivered", rd.is_dir())

    hk(["gc", "--days", "0.0007", "--force"], conf, root=sweep_root)
    check("sweep: gc agrees with the sweep", rd.is_dir())

    # The first scan wrote incident.json for the undelivered report, and incidents are kept as
    # evidence -- so this leg has to bypass that keeper (--force) to isolate the grace itself.
    no_grace = cfg("sweep2", retention_days=0.0007, delivery_grace_seconds=0)
    hk(["gc", "--days", "0.0007", "--force"], no_grace, root=sweep_root)
    check("sweep: with the grace disabled, the sweep proceeds", not rd.is_dir())


def test_decision_inbox_round_trip() -> None:
    """Items that need a human call must outlive the session that produced them.

    A session todo list is deliberately session-scoped (a list shared across projects is how an
    nginx decision gets confused with an Android one), so anything that has to wait for a person
    to come back lives in a file, and the todo list is only the view. This exercises the file half:
    add, render for the UI, answer, and the open-only view the wake path uses.
    """
    script = Path(__file__).resolve().parents[1] / "config" / "scripts" / "decisions.py"
    store = BASE / "decisions.jsonl"

    def run(*argv) -> str:
        return subprocess.run([sys.executable, str(script), *argv],
                              capture_output=True, text=True, timeout=60).stdout

    run("--file", str(store), "add", "--title", "enable monitoring?", "--why", "nobody is woken",
        "--options", "yes|no", "--default", "no")
    run("--file", str(store), "add", "--title", "rotate at?", "--options", "200KB|1MB",
        "--default", "200KB")
    listing = run("--file", str(store), "list")
    check("decisions: both ids are visible", "d1" in listing and "d2" in listing, listing)
    check("decisions: options and defaults render", "yes | no" in listing and "default=no" in listing,
          listing)

    todo = json.loads(run("--file", str(store), "todo") or "[]")
    check("decisions: todo rendering carries the id, options and default",
          len(todo) == 2 and todo[0]["id"] == "d1" and "options:" in todo[0]["content"]
          and "default:" in todo[0]["content"], str(todo)[:200])

    run("--file", str(store), "answer", "d1", "--answer", "yes", "--note", "observe a day")
    open_only = run("--file", str(store), "list", "--open-only")
    check("decisions: an answered item leaves the open view", "d1" not in open_only and "d2" in open_only,
          open_only)

    run("--file", str(store), "add", "--title", "third")
    ids = json.loads(run("--file", str(store), "list", "--json") or "[]")
    check("decisions: ids never reuse an answered one",
          sorted(r["id"] for r in ids) == ["d1", "d2", "d3"], str([r["id"] for r in ids]))

    # Regression: a flag given before the subcommand used to be overwritten by the subparser's
    # default, so the two spellings silently addressed different files.
    other = BASE / "decisions-other.jsonl"
    run("--file", str(other), "add", "--title", "via global position")
    check("decisions: --file works before and after the subcommand",
          "via global position" in run("list", "--file", str(other))
          and "via global position" not in run("--file", str(store), "list"), "path drift")


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
                 test_hang_kill_reaches_the_job_command,
                 test_skill_ships_every_file_it_points_at,
                 test_watch_file_exits_when_the_producer_writes,
                 test_nothing_is_written_at_the_hermes_home_root,
                 test_config_resolution_prefers_xdg_and_keeps_legacy_working,
                 test_doctor_prunes_old_runs_but_keeps_evidence,
                 test_agent_unreachable,
                 test_agent_responsive, test_phase_death_recovery,
                 test_milestone_and_wake_discipline, test_phase_exits_when_the_run_disappears,
                 test_stop_kills_the_job_command, test_stop_stops_the_phase,
                 test_stop_leaves_a_finished_run_alone,
                 test_redirected_install_keeps_the_host_units_and_migrates_runs,
                 test_doctor_does_not_flag_a_consumed_report,
                 test_gc_waits_for_a_report_to_be_delivered,
                 test_the_sweep_shares_the_gc_predicate,
                 test_env_driven_install_is_also_a_redirect,
                 test_set_cannot_overwrite_a_stop_verdict,
                 test_a_spelled_out_default_path_is_not_a_redirect,
                 test_status_shows_a_withdrawn_incident_as_withdrawn,
                 test_hk_finds_a_modern_interpreter_for_an_old_host,
                 test_report_grace_is_measured_from_the_report,
                 test_a_consumed_report_withdraws_the_incident,
                 test_the_sweep_and_gc_share_one_delivery_grace,
                 test_decision_inbox_round_trip,
                 test_installer_installs_every_reference,
                 test_kanban_watcher_reads_default_board,
                 test_named_board_is_not_the_default_board,
                 test_gate_check_reports_ungated_profiles,
                 test_per_run_report_interval_overrides_the_host_setting,
                 test_state_writes_cannot_resurrect_a_finished_run,
                 test_report_creates_a_child_session,
                 test_interpreter_probe_ignores_a_hostile_pythonpath,
                 test_hk_survives_a_lying_pythonpath_across_the_reexec,
                 test_issue_watch_reports_replies_on_closed_issues,
                 test_report_can_append_to_one_ledger_child,
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
