"""Report into a session as a *child* session, using the kernel's own session API.

Why this exists: a wake turn tells the agent something happened, but the artefact -- what the
background run actually did -- otherwise lives only in a file the user has to be told about. The
WebUI already renders child sessions (``parent_session_id``) nested under their parent, which is
how subagent work appears, so a background producer can report *there* and the result is
browsable, collapsible and attributable instead of being another top-level window.

The write goes through ``hermes_state.SessionDB`` -- the same class the CLI and the WebUI use --
not through hand-written SQL, so the session row, its counters and its message store stay
consistent with whatever the kernel expects this release.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

_SESSION_DB: Any = None
_TRIED = False


def _session_db_class():
    """Import ``SessionDB`` with the same two guards ``kanban._kernel()`` uses.

    Booting the kernel must never provision anything: importing ``hermes_cli`` with
    ``HERMES_HOME`` unset makes Hermes resolve a default home and *install* into it (measured:
    1.7 GB of tools from one probe), so refuse without a home and hold
    ``HERMES_DISABLE_LAZY_INSTALLS`` for the duration of the import only.
    """
    global _SESSION_DB, _TRIED
    if _TRIED:
        if _SESSION_DB is False:
            raise ImportError("hermes_state is not importable here")
        return _SESSION_DB
    _TRIED = True

    if not (os.environ.get("HERMES_HOME") or "").strip():
        _SESSION_DB = False
        raise ImportError("HERMES_HOME is not set; refusing to boot the kernel")

    src = os.environ.get("HAWKEYE_KERNEL_SRC", "").strip()
    if src and src not in sys.path:
        sys.path.insert(0, src)

    prev_lazy = os.environ.get("HERMES_DISABLE_LAZY_INSTALLS")
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    try:
        try:
            from hermes_state import SessionDB
        except Exception:
            import hermes_bootstrap  # noqa: F401  (wires the environment's site-packages)
            from hermes_state import SessionDB
        _SESSION_DB = SessionDB
        return _SESSION_DB
    except Exception:
        _SESSION_DB = False
        raise ImportError("hermes_state is not importable here") from None
    finally:
        if prev_lazy is None:
            os.environ.pop("HERMES_DISABLE_LAZY_INSTALLS", None)
        else:
            os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = prev_lazy


def child_id() -> str:
    return "hawkereport-%d-%04x" % (int(time.time()), os.getpid() & 0xFFFF)


def write_child(parent: str, title: str, body: str, *, source: str = "hawkeye") -> str:
    """Create a child session under ``parent`` holding ``body``; return the new session id.

    Raising is deliberate: the caller decides whether a failed report is worth failing the run
    over. ``cmd_report`` treats it as a warning (the wake still has to happen), while a caller
    that only exists to report can let it fail.
    """
    if not (parent or "").strip():
        # There is deliberately no "no parent" mode: a report is always a child of the session
        # that asked for the work. A tool that can mint top-level sessions fills the sidebar with
        # windows a user has to triage by hand after every unattended run, which is the opposite
        # of what a report is for. Detached conversations stay a user decision made in the UI.
        raise ValueError("a parent session id is required (reports are always child sessions)")
    db_cls = _session_db_class()
    sid = child_id()
    db = db_cls()
    try:
        if db.get_session(parent.strip()) is None:
            raise ValueError("parent session %s does not exist; refusing to write an orphan report"
                             % parent.strip())
        db.create_session(
            sid,
            source=source,
            parent_session_id=parent.strip(),
            display_name=(title or "hawkeye report")[:120],
            cwd=str(Path.cwd()),
        )
        db.append_message(sid, "assistant", body)
    finally:
        db.close()
    return sid


def append_child(sid: str, title: str, body: str) -> int:
    """Append one entry to an existing child session and return the message row id.

    A ledger is a single child session that a recurring producer keeps writing into: one place for
    the user to read, and no new window per cycle. Two refusals keep it honest -- the target must
    exist, and it must be a *child*: appending to a top-level conversation would edit a
    conversation the user owns.
    """
    if not (sid or "").strip():
        raise ValueError("--into needs a session id")
    db_cls = _session_db_class()
    db = db_cls()
    try:
        row = db.get_session(sid.strip())
        if row is None:
            raise ValueError("session %s does not exist" % sid.strip())
        parent = row["parent_session_id"] if not isinstance(row, dict) else row.get("parent_session_id")
        if not parent:
            raise ValueError("session %s is a top-level session; a ledger must be a child session"
                             % sid.strip())
        stamp = "[%s] %s\n\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), title or "entry")
        return db.append_message(sid.strip(), "assistant", stamp + body)
    finally:
        db.close()


def report_file(path: Path, parent: str, title: str, *, source: str = "hawkeye") -> str | None:
    """Report a file's contents as a child session. Returns the id, or None when unavailable.

    Never raises: this runs at the end of a watcher whose exit is the wake signal, and a report
    that cannot be written must not swallow that signal.
    """
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    try:
        return write_child(parent, title, body, source=source)
    except Exception as exc:                      # noqa: BLE001 - reported, never fatal
        sys.stderr.write("hk: could not write the child-session report (%s)\n" % exc)
        return None
