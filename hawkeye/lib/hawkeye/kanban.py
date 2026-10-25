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
from typing import Any

from . import store


_KERNEL: Any = None            # module once resolved, False when it cannot be imported
_KERNEL_TRIED = False


def _kernel():
    """Import the Hermes module that owns the board layout.

    Three steps, cheapest first, because this runs inside processes that may live for
    hours and must not pay a 1.5 s import more than once:

    1. A real Hermes process already has the module on ``sys.path``.
    2. Otherwise boot the way Hermes boots: import ``hermes_bootstrap``, which is
       stdlib-only and responsible for putting the environment's ``site-packages`` on
       ``sys.path``. Without it, ``hermes_cli.kanban_db`` dies on its own dependencies
       (measured here: ``ModuleNotFoundError: ruamel``) even under Hermes' own bundled
       interpreter — the interpreter is not what carries the modules, the boot does.
    3. If both fail, the caller falls back to :func:`_fallback_db`, which mirrors the
       kernel layout and refuses to guess.

    ``HAWKEYE_KERNEL_SRC`` exists only for out-of-process callers pointing at a source
    checkout; no machine path is baked in, so the same code runs anywhere.
    """
    global _KERNEL, _KERNEL_TRIED
    if _KERNEL_TRIED:
        if _KERNEL is False:
            raise ImportError("hermes_cli is not importable here")
        return _KERNEL
    _KERNEL_TRIED = True

    # Explicit opt-out: makes the portable path selectable on a host where the kernel
    # *is* importable, which is what the fallback tests need to stay deterministic.
    if (os.environ.get("HAWKEYE_NO_KERNEL") or "").strip().lower() in ("1", "true", "yes"):
        _KERNEL = False
        raise ImportError("kernel disabled by HAWKEYE_NO_KERNEL")

    src = os.environ.get("HAWKEYE_KERNEL_SRC", "").strip()
    if src and src not in sys.path:
        sys.path.insert(0, src)

    # Booting the kernel must never provision anything. Importing hermes_cli can trigger
    # Hermes' lazy install, and with HERMES_HOME unset it resolves a *default* home and
    # provisions there — measured: 1.7 GB of tools downloaded into ~/.hermes by one probe.
    # Two guards: refuse without a configured home, and hold the official opt-out for the
    # duration of the import only, so nothing is inherited by the jobs we spawn.
    if not (os.environ.get("HERMES_HOME") or "").strip():
        _KERNEL = False
        raise ImportError("HERMES_HOME is not set; refusing to boot the kernel")

    prev_lazy = os.environ.get("HERMES_DISABLE_LAZY_INSTALLS")
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    try:
        try:
            from hermes_cli import kanban_db
        except Exception:
            import hermes_bootstrap  # noqa: F401  (wires the environment's site-packages)
            from hermes_cli import kanban_db
        _KERNEL = kanban_db
        return _KERNEL
    except Exception:
        _KERNEL = False
        raise ImportError("hermes_cli is not importable here") from None
    finally:
        if prev_lazy is None:
            os.environ.pop("HERMES_DISABLE_LAZY_INSTALLS", None)
        else:
            os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = prev_lazy


_DB_CACHE: dict[tuple, Path] = {}


def db_path(board: str | None = None) -> Path:
    """Where this board's DB lives: kernel first, then the same layout by hand.

    The fallback used to return ``<home>/kanban.db`` for *every* board, so a
    named-board watcher silently read the default board instead — no error, wrong
    cards reported. That is the v1 failure family again, so the fallback now
    mirrors the kernel: ``default`` -> ``<home>/kanban.db``, anything else ->
    ``<home>/kanban/boards/<slug>/kanban.db`` (missing file surfaces as a
    FileNotFoundError from ``connect()``, never as the wrong board).

    Results are cached per process: resolving through the kernel costs an import of
    its whole dependency graph, and a watcher asks for the same board every tick.
    """
    key = ((board or "").strip(), os.environ.get("HAWKEYE_KANBAN_DB", "").strip(),
           os.environ.get("HERMES_HOME", "").strip())
    cached = _DB_CACHE.get(key)
    if cached is not None:
        return cached
    path = _db_path_uncached(board)
    _DB_CACHE[key] = path
    return path


def _db_path_uncached(board: str | None = None) -> Path:
    try:
        return Path(_kernel().kanban_db_path(board))
    except Exception:
        env = os.environ.get("HAWKEYE_KANBAN_DB", "").strip()
        slug = (board or "").strip()
        if env and not slug:                     # the pin only applies without an explicit board
            return Path(env).expanduser()
        home = store.hermes_home()
        if not slug or slug == "default":
            return home / "kanban.db"
        return home / "kanban" / "boards" / slug / "kanban.db"


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
