"""Kanban read helpers.

The board DB path comes from the kernel (``kanban_db_path``): the default board
lives at ``<home>/kanban.db`` while named boards live under
``<home>/kanban/boards/<slug>/kanban.db``. String-concatenating
``boards/<slug>/kanban.db`` is exactly the bug that made hawkeye v1 a silent
no-op on a default-board install — never do it here.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path

from . import store


def _kernel():
    """Import the Hermes module that owns the board layout.

    A real Hermes process already has it on ``sys.path``. ``HAWKEYE_KERNEL_SRC``
    exists only for out-of-process callers pointing at a source checkout; no
    machine path is baked in, so the same code runs anywhere.
    """
    src = os.environ.get("HAWKEYE_KERNEL_SRC", "").strip()
    if src and src not in sys.path:
        sys.path.insert(0, src)
    from hermes_cli import kanban_db
    return kanban_db


def db_path(board: str | None = None) -> Path:
    try:
        return Path(_kernel().kanban_db_path(board))
    except Exception:
        env = os.environ.get("HAWKEYE_KANBAN_DB", "").strip()
        if env:
            return Path(env).expanduser()
        return store.hermes_home() / "kanban.db"


def connect(board: str | None = None) -> sqlite3.Connection:
    path = db_path(board)
    if not path.is_file():
        raise FileNotFoundError("kanban db not found: %s" % path)
    conn = sqlite3.connect("file:%s?mode=ro" % path, uri=True, timeout=5)
    conn.execute("PRAGMA query_only=1")
    conn.row_factory = sqlite3.Row
    return conn


def max_event_id(conn: sqlite3.Connection) -> int:
    row = conn.execute("select coalesce(max(id),0) from task_events").fetchone()
    return int(row[0] or 0)


def new_events(conn: sqlite3.Connection, since: int, kinds: list[str]) -> list[dict]:
    if not kinds:
        return []
    marks = ",".join("?" * len(kinds))
    rows = conn.execute(
        "select id, task_id, run_id, kind, payload, created_at from task_events"
        " where id > ? and kind in (%s) order by id" % marks,
        (since, *kinds),
    ).fetchall()
    return [dict(r) for r in rows]


def titles(conn: sqlite3.Connection, task_ids: list[str]) -> dict[str, str]:
    wanted = sorted({t for t in task_ids if t})
    if not wanted:
        return {}
    marks = ",".join("?" * len(wanted))
    rows = conn.execute("select id, title from tasks where id in (%s)" % marks, wanted).fetchall()
    return {r["id"]: r["title"] for r in rows}


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute("select status, count(*) from tasks group by status").fetchall()
    return {str(r[0]): int(r[1]) for r in rows}
