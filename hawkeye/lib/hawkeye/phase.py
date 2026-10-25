"""Supervision phase — "the process is the phase".

A phase advances one run and then EXITS when it has something to report: that
process exit is what fires ``notify_on_complete``, the only wake channel the
kernel does not cap (``watch_patterns`` is limited to 8 hits per session, with a
15 s cooldown and a 3-strike breaker — it is NOT a durable channel).

Three rules keep that channel honest:

1. the job runs DETACHED (``hk job``, own session), so killing or exiting a
   phase never kills the work — the work writes its own exit code to disk;
2. a phase only exits for a *non-terminal* report while the agent lease is
   fresh; if nobody renews the lease it keeps working silently instead of
   burning wakes on an empty room;
3. every fact lives in ``state.json`` / ``events.jsonl``, so whichever phase
   comes next (mine, or doctor's) resumes without loss.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import job as job_mod
from . import kanban, store

TERMINAL = ("done", "gaveup")
REASONS_CN = {
    "done": "job done",
    "gaveup": "retry budget exhausted, gave up",
    "failure": "this attempt failed",
    "milestone": "new milestone",
    "interval": "scheduled progress report",
    "board": "new board events",
}

_MAX_DIGEST_TAIL_LINES = 3
_MAX_MILESTONES_IN_DIGEST = 4
_LINE_CLIP = 90


class Phase:
    def __init__(self, cfg: dict, run_id: str):
        self.cfg = cfg
        self.rid = run_id
        self.rd = store.run_dir(run_id)
        self.meta: dict = store.read_json(self.rd / store.META, {}) or {}
        self.state: dict = store.read_json(self.rd / store.STATE, {}) or {}
        self._child: subprocess.Popen | None = None      # reap the job wrapper we spawned

    # ── lifecycle ─────────────────────────────────────────────────────────
    def run(self) -> int:
        if not self.meta:
            print("HK-ERROR missing meta.json: %s" % self.rd)
            return 2
        with store.run_lock(self.rd) as owned:
            if not owned:
                return 0                      # another phase owns this run
            if self.state.get("finished"):
                return 0
            store.append_event(self.rd, "phase_start", pid=os.getpid())
            while True:
                self._beat()
                terminal, reason = self._advance()
                if terminal:
                    self._finish(reason)
                    self._report(reason)
                    return 0
                if self._report_due(reason):
                    if self._report_allowed():
                        self._report(reason)
                        return 0
                    self._defer(reason)
                time.sleep(float(self.cfg["poll_seconds"]))

    def _beat(self) -> None:
        try:
            (self.rd / store.SUP_BEAT).write_text(store.now_iso())
        except OSError:
            pass

    def _set(self, **patch: Any) -> None:
        self.state.update(patch)
        store.write_json(self.rd / store.STATE, self.state)

    # ── advance: dispatch ─────────────────────────────────────────────────
    def _advance(self) -> tuple[bool, str]:
        if self.meta.get("kind") == "kanban":
            return self._advance_kanban()
        return self._advance_exec()

    # ── advance: exec runs ────────────────────────────────────────────────
    def _advance_exec(self) -> tuple[bool, str]:
        self._sample_log()                    # sample first: short jobs' milestones are not missed
        status = self._attempt_state()
        if status == "not_started":
            if self._attempts_done() >= int(self.cfg["attempts"]):
                return True, "gaveup"
            if time.time() < float(self.state.get("next_attempt_at") or 0):
                return False, "backoff"
            self._start_attempt()
            return False, "running"
        if status in ("running", "finishing"):
            hang = self._hang_reason()
            if hang:
                self._kill(hang)
                return self._fail(124, hang)
            return False, "milestone" if self._milestone_pending() else "running"
        code = job_mod.exit_code(self.rd)
        if code is None:                       # wrapper died without a verdict
            return self._fail(137, "lost")
        if code == 0:
            return True, "done"
        return self._fail(code, "exit")

    def _attempt_state(self) -> str:
        # poll() reaps our own child; without it a finished wrapper stays a zombie.
        if self._child is not None and self._child.poll() is not None:
            return "exited" if job_mod.exit_code(self.rd) is not None else "finishing"
        pid = self.state.get("job_pid")
        if pid and store.pid_alive(int(pid), self.state.get("job_pid_ticks")):
            return "running"
        if job_mod.exit_code(self.rd) is not None:
            return "exited"
        return "lost" if pid else "not_started"

    def _attempts_done(self) -> int:
        return int(self.state.get("attempts_done") or 0)

    def _start_attempt(self) -> None:
        attempt = self._attempts_done() + 1
        self._clear_job_files()
        entry = [sys.executable, str(self.meta["entry"]), "job", "--run", self.rid]
        proc = subprocess.Popen(entry, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        self._child = proc
        ticks = None
        for _ in range(50):
            ticks = store.proc_start_ticks(proc.pid)
            if ticks is not None:
                break
            time.sleep(0.02)
        self._set(job_pid=proc.pid, job_pid_ticks=ticks, attempt=attempt,
                  attempt_started=time.time(), log_offset=0,
                  next_attempt_at=0, last_failure=None)
        store.append_event(self.rd, "attempt_start", attempt=attempt, pid=proc.pid)

    def _fail(self, code: int, why: str) -> tuple[bool, str]:
        done = self._attempts_done() + 1
        self._set(attempts_done=done, job_pid=None, job_pid_ticks=None,
                  last_exit_code=code, last_failure=why, attempt_started=None)
        self._clear_job_files()
        store.append_event(self.rd, "attempt_failed", attempt=done,
                           exit_code=code, reason=why)
        if done >= int(self.cfg["attempts"]):
            return True, "gaveup"
        self._set(next_attempt_at=time.time() + self._backoff(done))
        return False, "failure"

    def _backoff(self, done: int) -> float:
        table = list(self.cfg["backoff_seconds"]) or [30]
        return float(table[min(done - 1, len(table) - 1)])

    def _clear_job_files(self) -> None:
        # A stale Popen handle would make every later tick read as "finishing"
        # (poll() != None with no exit file) and spin forever without retrying.
        self._child = None
        for name in (store.JOB_PID, store.JOB_EXIT):
            try:
                (self.rd / name).unlink()
            except OSError:
                pass

    def _hang_reason(self) -> str:
        started = float(self.state.get("attempt_started") or 0)
        if started and time.time() - started > float(self.cfg["attempt_timeout_seconds"]):
            return "timeout"
        age = store.mtime_age(self.rd / store.WORK_LOG)
        if age is not None and age > float(self.cfg["stall_seconds"]):
            return "stalled"
        return ""

    def _kill(self, why: str) -> None:
        pid = self.state.get("job_pid")
        if not pid:
            return
        pid = int(pid)
        ticks = self.state.get("job_pid_ticks")
        _signal_group(pid, signal.SIGTERM)
        deadline = time.time() + 5.0
        while time.time() < deadline and store.pid_alive(pid, ticks):
            time.sleep(0.1)
        if store.pid_alive(pid, ticks):
            _signal_group(pid, signal.SIGKILL)
        store.append_event(self.rd, "job_killed", reason=why, pid=pid)

    def _sample_log(self) -> None:
        """Read only new bytes; collect ``<prefix>`` milestone lines the job prints."""
        path = self.rd / store.WORK_LOG
        try:
            size = path.stat().st_size
        except OSError:
            return
        offset = int(self.state.get("log_offset") or 0)
        if size <= offset:
            return
        with open(path, "rb") as fh:
            fh.seek(offset)
            chunk = fh.read(min(size - offset, 1 << 18))
        cut = chunk.rfind(b"\n")
        if cut < 0:
            return
        self._set(log_offset=offset + cut + 1)
        prefix = str(self.cfg["milestone_prefix"])
        fresh = [line[len(prefix):].strip()
                 for line in chunk[: cut + 1].decode("utf-8", "replace").splitlines()
                 if line.startswith(prefix)]
        if fresh:
            self._set(milestones=(list(self.state.get("milestones") or []) + fresh)[-50:],
                      milestone_at=time.time())

    def _milestone_pending(self) -> bool:
        total = len(self.state.get("milestones") or [])
        if total <= int(self.state.get("milestones_reported") or 0):
            return False
        quiet = time.time() - float(self.state.get("milestone_at") or 0)
        return quiet >= float(self.cfg["milestone_flush_seconds"])

    # ── advance: kanban watcher ───────────────────────────────────────────
    def _advance_kanban(self) -> tuple[bool, str]:
        board = self.meta.get("board") or "default"
        since = int(self.state.get("cursor") or 0)
        try:
            conn = kanban.connect(board)
        except Exception as exc:                # board absent/locked: retry next tick
            store.append_event(self.rd, "kanban_unavailable", error=str(exc))
            return False, "kanban_down"
        try:
            if not self.state.get("cursor_ready"):
                # Align to the current max id on the first run (no history replay).
                # cursor==0 is no sentinel: on an empty board it IS 0, which would
                # re-align every tick and swallow the first event inserted.
                since = kanban.max_event_id(conn)
                self._set(cursor=since, cursor_ready=True)
            rows = kanban.new_events(conn, since, list(self.cfg["kanban_kinds"]))
            names = kanban.titles(conn, [r["task_id"] for r in rows]) if rows else {}
        finally:
            conn.close()
        if rows:
            pending = list(self.state.get("pending") or [])
            pending += [dict(r, title=names.get(r["task_id"], "")) for r in rows]
            self._set(pending=pending[-200:],
                      pending_since=self.state.get("pending_since") or time.time())
        pending = self.state.get("pending") or []
        if not pending:
            return False, "idle"
        quiet = time.time() - float(self.state.get("pending_since") or 0)
        if quiet < float(self.cfg["kanban_quiet_seconds"]):
            return False, "settling"
        return False, "board"

    def _commit_board(self) -> None:
        pending = self.state.get("pending") or []
        if not pending:
            return
        last = max(int(e.get("id") or 0) for e in pending)
        self._set(cursor=last, pending=[], pending_since=0)

    # ── reporting ─────────────────────────────────────────────────────────
    def _report_due(self, reason: str) -> bool:
        """Which reasons justify a process exit (= one wake).

        ``failure`` is deliberately NOT here: a failed attempt is retried
        in-phase, so three attempts cost one report, not three wakes. Only
        terminal state, milestones, board batches and the interval report exit.
        """
        if reason in ("milestone", "board"):
            return True
        anchor = self.state.get("last_report_at") or self.state.get("attempt_started")
        if not anchor:
            return False
        return time.time() - float(anchor) > float(self.cfg["report_interval_seconds"])

    def _lease_age(self) -> float:
        lease = store.read_json(self.rd / store.LEASE, {}) or {}
        seen = float(lease.get("agent_seen") or 0)
        return float("inf") if not seen else max(0.0, time.time() - seen)

    def _report_allowed(self) -> bool:
        if int(self.state.get("reports") or 0) == 0:
            return True                        # spawned inside a live turn
        if int(self.state.get("wakes") or 0) >= int(self.cfg["max_wakes"]):
            return False                       # budget spent: stay silent, keep working
        return self._lease_age() < float(self.cfg["wake_grace_seconds"])

    def _defer(self, reason: str) -> None:
        if self.state.get("deferred") != reason:
            self._set(deferred=reason)
            store.append_event(self.rd, "report_deferred", reason=reason,
                               lease_age=None if self._lease_age() == float("inf")
                               else int(self._lease_age()))

    def _report(self, reason: str) -> None:
        # Digest BEFORE committing the board batch: the digest reads state["pending"],
        # and committing first made every board report say "0 rows".
        text = self._digest(reason)
        if reason == "board":
            self._commit_board()
        self._set(reports=int(self.state.get("reports") or 0) + 1,
                  wakes=int(self.state.get("wakes") or 0) + 1,
                  last_report_at=time.time(), last_reason=reason, deferred=None,
                  milestones_reported=len(self.state.get("milestones") or []))
        (self.rd / store.REPORT).write_text(text + "\n", encoding="utf-8")
        store.append_event(self.rd, "report", reason=reason,
                           duration_s=int(time.time() - float(self.meta.get("created_at") or time.time())))
        sys.stdout.write(text + "\n")
        sys.stdout.flush()

    def _digest(self, reason: str) -> str:
        head = "[hawkeye] %s %s" % (self.rid, REASONS_CN.get(reason, reason))
        if self.meta.get("kind") == "kanban":
            body = self._digest_board()
        else:
            body = self._digest_exec(reason)
        return "\n".join([head] + body + ["lease: hk lease --run " + self.rid])

    def _digest_exec(self, reason: str) -> list[str]:
        lines: list[str] = []
        done = self._attempts_done()
        attempt = int(self.state.get("attempt") or 0)
        started = float(self.meta.get("created_at") or time.time())
        lines.append("progress: attempt %d/%d, running for %s"
                     % (max(attempt, done), int(self.cfg["attempts"]),
                        store.fmt_dur(time.time() - started)))
        if self.state.get("last_exit_code") is not None:
            lines.append("last exit code: %s (%s)" % (self.state["last_exit_code"],
                                                self.state.get("last_failure") or "-"))
        milestones = (self.state.get("milestones") or [])[-_MAX_MILESTONES_IN_DIGEST:]
        if milestones:
            lines.append("milestones: " + " | ".join(_clip(m, _LINE_CLIP) for m in milestones))
        tail = [ln for ln in store.tail(self.rd / store.WORK_LOG, 4096).splitlines()
                if ln.strip() and not ln.startswith(str(self.cfg["milestone_prefix"]))]
        if tail:
            lines.append("recent output: " + " ⟂ ".join(_clip(ln, _LINE_CLIP)
                                                   for ln in tail[-_MAX_DIGEST_TAIL_LINES:]))
        if reason == "gaveup":
            lines.append("verdict: retries stopped, needs a human (details in events.jsonl / work.log)")
        return lines

    def _digest_board(self) -> list[str]:
        pending = self.state.get("pending") or []
        by_kind: dict[str, int] = {}
        for ev in pending:
            by_kind[ev["kind"]] = by_kind.get(ev["kind"], 0) + 1
        lines = ["board: %s has %d new (%s)"
                 % (self.meta.get("board") or "default", len(pending),
                    ", ".join("%s×%d" % (k, v) for k, v in sorted(by_kind.items())))]
        for ev in pending[-4:]:
            label = "%s %s %s" % (ev["kind"], ev["task_id"], _clip(ev.get("title") or "", 40))
            lines.append("· " + label.strip())
        return lines

    # ── terminal ──────────────────────────────────────────────────────────
    def _finish(self, reason: str) -> None:
        started = float(self.meta.get("created_at") or time.time())
        payload = {
            "run_id": self.rid, "kind": self.meta.get("kind", "exec"),
            "reason": reason, "finished": True,
            "attempts_done": self._attempts_done(),
            "last_exit_code": self.state.get("last_exit_code"),
            "duration_s": int(time.time() - started),
            "work_log": str(self.rd / store.WORK_LOG),
        }
        self._set(finished=True, finished_at=time.time(), finish_reason=reason)
        store.write_json(self.rd / store.RESULT, payload)
        store.append_event(self.rd, "run_" + ("done" if reason == "done" else "gaveup"),
                           attempts=self._attempts_done(), exit_code=payload["last_exit_code"])
        if reason == "gaveup":
            store.write_json(self.rd / store.INCIDENT, {
                "reason": "attempts_exhausted", "at": store.now_iso(),
                "detail": "gave up after %d attempts, last exit code %s"
                          % (self._attempts_done(), payload["last_exit_code"]),
            })


def _signal_group(pid: int, sig: int) -> None:
    try:
        os.killpg(os.getpgid(pid), sig)
    except (ProcessLookupError, PermissionError):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            pass


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"
