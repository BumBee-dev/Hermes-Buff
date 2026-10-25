"""hk — command line for hawkeye-v2."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path

from . import config as cfg_mod
from . import doctor, gatecheck, job as job_mod, phase, store


def _err(msg: str, code: int = 2) -> int:
    sys.stderr.write("hk: %s\n" % msg)
    return code


def _entry() -> str:
    return str(Path(sys.argv[0]).resolve())


# ── run creation ──────────────────────────────────────────────────────────
def _create(cfg: dict, args: argparse.Namespace, kind: str, extra: dict) -> str:
    rid = args.id or store.new_run_id(kind)
    rd = store.run_dir(rid)
    if (rd / store.META).exists():
        raise SystemExit("hk: run already exists: %s" % rid)
    meta = {"run_id": rid, "kind": kind, "created_at": time.time(),
            "entry": _entry(), "attempts": cfg["attempts"]}
    meta.update(extra)
    store.write_json(rd / store.META, meta)
    # The run creator is the agent itself, so the first lease is written right here;
    # every later wake has to renew it with hk lease.
    store.write_json(rd / store.LEASE, {"agent_seen": time.time(),
                                        "at": store.now_iso(), "by": "creator"})
    store.append_event(rd, "run_created", kind=kind)
    return rid


def cmd_run(args: argparse.Namespace, cfg: dict) -> int:
    argv = list(args.argv)
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        return _err("usage: hk run [--id X] -- <command...>")
    rid = _create(cfg, args, "exec", {
        "argv": argv,
        "cwd": os.path.abspath(args.cwd or os.getcwd()),
    })
    # Banner only on an interactive terminal: in a background spawn stdout is a
    # pipe and the kernel forwards our tail into the wake notice, where it is noise.
    if sys.stdout.isatty():
        print("HK-START %s started (detached job + separate supervision phase)" % rid)
    sys.stdout.flush()
    return phase.Phase(cfg, rid).run()


def cmd_kanban(args: argparse.Namespace, cfg: dict) -> int:
    rid = _create(cfg, args, "kanban", {"board": args.board or "default"})
    if sys.stdout.isatty():
        print("HK-START %s board watcher started (board=%s)" % (rid, args.board or "default"))
    sys.stdout.flush()
    return phase.Phase(cfg, rid).run()


def cmd_phase(args: argparse.Namespace, cfg: dict) -> int:
    return phase.Phase(cfg, args.run).run()


def cmd_job(args: argparse.Namespace, cfg: dict) -> int:
    rd = store.run_dir(args.run)
    meta = store.read_json(rd / store.META, {}) or {}
    if not meta.get("argv"):
        return _err("run has no argv: %s" % args.run)
    attempt = int((store.read_json(rd / store.STATE, {}) or {}).get("attempts_done") or 0) + 1
    beat = max(2.0, min(60.0, float(cfg["poll_seconds"]) * 5))
    return job_mod.run(rd, list(meta["argv"]), meta.get("cwd"), beat, attempt)


# ── agent side ────────────────────────────────────────────────────────────
def cmd_lease(args: argparse.Namespace, cfg: dict) -> int:
    targets = _targets(args)
    if not targets:
        return _err("no run to lease")
    now = time.time()
    for rid in targets:
        store.write_json(store.run_dir(rid) / store.LEASE,
                         {"agent_seen": now, "at": store.now_iso()})
        store.append_event(store.run_dir(rid), "lease")
    print("leased: %s" % " ".join(targets))
    return 0


def cmd_status(args: argparse.Namespace, cfg: dict) -> int:
    targets = _targets(args)
    rows = []
    for rid in targets:
        rows.append(_describe(rid))
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return 0
    if not rows:
        print("(no runs)")
        return 0
    print("%-24s %-8s %-10s %5s %4s %6s %6s  %s" %
          ("RUN", "KIND", "STATUS", "TRIES", "RPTS", "SUP_AGE", "LSE_AGE", "NOTE"))
    for row in rows:
        print("%-24s %-8s %-10s %5d %4d %6s %6s  %s" % (
            row["run"], row["kind"], row["status"], row["attempts"], row["reports"],
            "-" if row["sup_age_s"] is None else "%ds" % row["sup_age_s"],
            "-" if row["lease_age_s"] is None else "%ds" % row["lease_age_s"],
            row["note"]))
    return 0


def _describe(rid: str) -> dict:
    rd = store.run_dir(rid)
    meta = store.read_json(rd / store.META, {}) or {}
    state = store.read_json(rd / store.STATE, {}) or {}
    result = store.read_json(rd / store.RESULT, {}) or {}
    incident = store.read_json(rd / store.INCIDENT, {}) or {}
    sup_age = store.mtime_age(rd / store.SUP_BEAT)
    lease = store.read_json(rd / store.LEASE, {}) or {}
    seen = float(lease.get("agent_seen") or 0)
    if state.get("finished"):
        status = "done" if result.get("reason") == "done" else "gave-up"
    elif sup_age is not None and sup_age < 180:
        status = "running"
    else:
        status = "unwatched"
    notes = []
    if incident:
        notes.append("incident=%s" % incident.get("reason"))
    if state.get("last_exit_code") is not None:
        notes.append("exit=%s" % state["last_exit_code"])
    if state.get("deferred"):
        notes.append("deferred=%s" % state["deferred"])
    return {
        "run": rid, "kind": meta.get("kind", "exec"), "status": status,
        "attempts": int(state.get("attempts_done") or 0),
        "reports": int(state.get("reports") or 0),
        "sup_age_s": None if sup_age is None else int(sup_age),
        "lease_age_s": None if not seen else int(time.time() - seen),
        "incident": incident or None,
        "note": ", ".join(notes),
    }


def cmd_doctor(args: argparse.Namespace, cfg: dict) -> int:
    rows = doctor.scan(cfg, fix=args.fix)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print("%-24s %-8s %-10s reports=%d %s" % (
                row["run"], row["kind"], row["status"], row["reports"],
                row["action"] or ("incident" if row["incident"] else "")))
        if not rows:
            print("(no runs)")
    return 1 if any(row["incident"] for row in rows) else 0


def cmd_watch_file(args: argparse.Namespace, cfg: dict) -> int:
    """Block until a file's content changes, then exit — the notification half of a run.

    This is the counterpart to a producer that must keep working when nobody is in the chat
    (a cron job doing detection and real work). The producer writes a report file; this exits
    when it changes, the phase reports, and the session that started the run gets woken. It does
    no work of its own and never touches the network, so a wake always means "something was
    written", never "the clock ticked".

    Heartbeat lines are plain (no milestone prefix, so they add nothing to the report) but they
    keep ``work.log`` growing: a deliberately quiet watcher must not look hung to stall detection.
    """
    path = Path(args.path).expanduser()
    interval = float(args.interval if args.interval is not None else 10.0)
    ttl = float(args.ttl if args.ttl is not None else 43200.0)
    prefix = str(cfg.get("milestone_prefix", "HK-MS "))

    def digest() -> str | None:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    baseline = digest()
    if baseline is None:
        sys.stderr.write("hk watch-file: %s does not exist yet; waiting for it to appear\n" % path)
    started = time.time()
    while True:
        current = digest()
        if current != baseline:
            print("%s%s: report changed (%s)" % (prefix, args.name, path))
            return 0
        if time.time() - started >= ttl:
            print("%s%s: no change for %ds; stopping" % (prefix, args.name, int(ttl)))
            return 0
        print("[watch] %s unchanged, next check in %ds" % (path.name, int(interval)))
        sys.stdout.flush()
        time.sleep(interval)


def cmd_gate_check(args: argparse.Namespace, cfg: dict) -> int:
    """Report gate coverage per profile — the gate plugin loads per profile, not per host."""
    rows = gatecheck.report()
    missing = gatecheck.gaps(rows)
    if args.json:
        print(json.dumps({"rows": rows, "ungated": [r["profile"] for r in missing]},
                         ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(gatecheck.render(rows))
    return 1 if missing else 0


def cmd_events(args: argparse.Namespace, cfg: dict) -> int:
    rd = store.run_dir(args.run)
    for rec in store.read_events(rd, limit=args.limit):
        print("%s %-16s %s" % (rec.get("at"), rec.get("kind"),
                               json.dumps({k: v for k, v in rec.items()
                                           if k not in ("ts", "at", "kind")},
                                          ensure_ascii=False)))
    return 0


def cmd_stop(args: argparse.Namespace, cfg: dict) -> int:
    rd = store.run_dir(args.run)
    state = store.read_json(rd / store.STATE, {}) or {}
    # Two different processes matter here: state["job_pid"] is the `hk job` wrapper the phase
    # spawned, while job.pid is the *command* the wrapper started with start_new_session=True —
    # i.e. a process group of its own. Killing only the wrapper's group leaves the real work
    # running with nobody supervising it (measured: `hk stop` left the job command alive).
    targets: list[tuple[int, int | None]] = []
    pid = state.get("job_pid")
    if pid:
        targets.append((int(pid), state.get("job_pid_ticks")))
    try:
        cmd_pid = int((rd / store.JOB_PID).read_text().strip())
    except (OSError, ValueError):
        cmd_pid = None
    if cmd_pid and cmd_pid != pid:
        targets.append((cmd_pid, None))
    for target, ticks in targets:
        if not store.pid_alive(target, ticks):
            continue
        try:
            os.killpg(os.getpgid(target), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    # Give the group a moment to die, then make sure: a shell waiting on a child can ignore TERM.
    survivors: list[int] = []
    for target, ticks in targets:
        for _ in range(20):
            if not store.pid_alive(target, ticks):
                break
            time.sleep(0.05)
        else:
            try:
                os.killpg(os.getpgid(target), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        for _ in range(20):
            if not store.pid_alive(target, ticks):
                break
            time.sleep(0.05)
        else:
            survivors.append(target)
    store.write_json(rd / store.STATE, {**state, "finished": True,
                                        "finish_reason": "stopped",
                                        "finished_at": time.time()})
    store.append_event(rd, "run_stopped", survivors=survivors or None)
    print("stopped %s" % args.run)
    if survivors:
        # Never let a stop look successful while something it was meant to kill is still alive:
        # a watcher that survives its own stop is exactly the invisible-resource problem.
        sys.stderr.write("hk: warning: still alive after stop: %s\n"
                         % ", ".join(str(p) for p in survivors))
        return 1
    return 0


def cmd_gc(args: argparse.Namespace, cfg: dict) -> int:
    """Delete expired **finished** runs; those with an incident are kept by default (they are evidence)."""
    root = store.run_root()
    if not root.is_dir():
        print("(no runs)")
        return 0
    now = time.time()
    removable: list[Path] = []
    kept: list[tuple[str, str]] = []
    for rd in sorted(root.iterdir()):
        if not rd.is_dir() or not (rd / store.META).is_file():
            continue
        state = store.read_json(rd / store.STATE, {}) or {}
        if not state.get("finished"):
            kept.append((rd.name, "not finished"))
            continue
        finished_at = float(state.get("finished_at") or 0)
        if finished_at and (now - finished_at) < args.days * 86400:
            kept.append((rd.name, "younger than %.0f days" % args.days))
            continue
        if (rd / store.INCIDENT).is_file() and not args.force:
            kept.append((rd.name, "has incident"))
            continue
        removable.append(rd)
    for rd in removable:
        if not args.dry_run:
            shutil.rmtree(rd, ignore_errors=True)
    print("%s: removed %d, kept %d%s" % (
        "dry-run" if args.dry_run else "cleaned up", len(removable), len(kept),
        " (only --force also removes incidents)" if any(k[1] == "has incident" for k in kept) else ""))
    for name, why in kept:
        print("  kept %-28s %s" % (name, why))
    return 0


def _targets(args: argparse.Namespace) -> list[str]:
    if getattr(args, "all", False):
        root = store.run_root()
        if not root.is_dir():
            return []
        return [p.name for p in sorted(root.iterdir()) if (p / store.META).is_file()]
    rid = getattr(args, "run", None)
    return [rid] if rid else []


# ── parser ────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hk", description="hawkeye-v2: long-run supervision and reports back to the source session")
    p.add_argument("--config", help="TOML config (defaults to $HAWKEYE_V2_CONFIG)")
    p.add_argument("--root", help="run directory root (defaults to $HAWKEYE_V2_ROOT or <HERMES_HOME>/hawkeye-v2/runs)")
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="start an exec run (best launched from a terminal background)")
    run.add_argument("--id")
    run.add_argument("--cwd")
    run.add_argument("argv", nargs=argparse.REMAINDER)

    kb = sub.add_parser("kanban", help="start a board-event watcher run")
    kb.add_argument("--id")
    kb.add_argument("--board")

    ph = sub.add_parser("phase", help="(internal/doctor) run one supervision phase")
    ph.add_argument("--run", required=True)

    jb = sub.add_parser("job", help="(internal) execute one job attempt")
    jb.add_argument("--run", required=True)

    for name, func, help_text in (("status", cmd_status, "show run status"),
                                  ("lease", cmd_lease, "renew the lease (called when I am woken)")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--run")
        sp.add_argument("--all", action="store_true")
        if name == "status":
            sp.add_argument("--json", action="store_true")

    wf = sub.add_parser("watch-file",
                        help="block until a file changes, then exit (producer-notifies-consumer)")
    wf.add_argument("--path", required=True, help="file whose content is the signal")
    wf.add_argument("--interval", type=float, default=10.0, help="seconds between checks")
    wf.add_argument("--ttl", type=float, default=43200.0, help="give up after this many seconds")
    wf.add_argument("--name", default="watch", help="label used in the milestone line")
    doc = sub.add_parser("doctor", help="safety sweep: restart dead supervision phases, raise incidents")
    doc.add_argument("--fix", action="store_true")
    doc.add_argument("--json", action="store_true")

    gchk = sub.add_parser("gate-check", help="report which profiles have the completion gate installed and enabled")
    gchk.add_argument("--json", action="store_true")

    ev = sub.add_parser("events", help="print the event stream")
    ev.add_argument("--run", required=True)
    ev.add_argument("--limit", type=int, default=50)

    st = sub.add_parser("stop", help="stop a run")
    st.add_argument("--run", required=True)

    gc = sub.add_parser("gc", help="remove expired finished runs (those with an incident are kept by default)")
    gc.add_argument("--days", type=float, default=7.0)
    gc.add_argument("--dry-run", action="store_true")
    gc.add_argument("--force", action="store_true", help="also remove runs that have an incident")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.root:
        os.environ["HAWKEYE_V2_ROOT"] = args.root
    cfg = cfg_mod.load(args.config)
    handlers = {
        "run": cmd_run, "kanban": cmd_kanban, "phase": cmd_phase, "job": cmd_job,
        "status": cmd_status, "lease": cmd_lease, "doctor": cmd_doctor,
        "events": cmd_events, "stop": cmd_stop, "gc": cmd_gc,
        "gate-check": cmd_gate_check, "watch-file": cmd_watch_file,
    }
    return handlers[args.cmd](args, cfg)
