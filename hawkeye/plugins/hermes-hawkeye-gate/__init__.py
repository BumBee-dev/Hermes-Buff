"""hawkeye-v2 completion gate — vetoes a ``kanban_complete`` that declares done with no evidence.

Three modes (``HAWKEYE_GATE_MODE``): ``off`` | ``warn`` | ``block`` (default).
Three criteria; missing any one means insufficient evidence:

  (a) summary or result is non-empty;
  (b) artifacts is non-empty, **or** metadata carries a command + output summary;
  (c) when the card has a completion_contract, an acceptance record exists
      (a pr_acceptance event, or metadata.published_pr on this call);
      a ``local-only`` contract is exempt.

The three criteria match hawkeye v1 SPEC §6. The implementation differs
**deliberately** in three places — each one is a root cause of v1 failing
silently on this machine:

1. DB lookup uses **kernel truth only**, ``kanban_db_path(board)``. The default
   board's DB is ``<HERMES_HOME>/kanban.db``; v1 glued together
   ``boards/<slug>/kanban.db`` itself, returned None when that missed ("better
   not to write"), so on the default board the gate always let everything pass.
2. The board is resolved from **the task's own board** (a worker process has
   ``HERMES_KANBAN_BOARD`` pinned), not from the process's current board; v1
   used the current board, so cross-board calls read the wrong DB.
3. The gate log lives **beside the board DB** (``hawkeye-gate.log``); it no
   longer conjures a ``boards/default/`` directory out of thin air.

Contract: never raise, no network/LLM work, read-only SQLite; ``warn`` mode
**never returns** a block directive (a false-positive veto nails the card shut).
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

GATE_TOOL = "kanban_complete"
GATE_MODES = ("off", "warn", "block")
# The default was switched by the user from warn to block (2026-10-25). A false
# verdict costs a card that cannot complete; rolling back needs no code change —
# set HAWKEYE_GATE_MODE=warn in the process env (env wins over this default).
DEFAULT_MODE = "block"
LOG_NAME = "hawkeye-gate.log"
LOCAL_ONLY = "local-only"
ANSWER_KEYS = ("summary", "result")
ARTIFACT_KEYS = ("artifacts", "attachments")
COMMAND_KEYS = ("command", "repro_command", "repro_commands", "repro")
OUTPUT_KEYS = ("output", "output_summary", "stdout", "raw_output", "command_output")
DB_BUSY_TIMEOUT = 0.2


def register(ctx: Any) -> None:
    """Register the gate. Each hook is best-effort: a broken one must not kill the plugin."""
    try:
        ctx.register_hook("pre_tool_call", on_pre_tool_call)
    except Exception:
        pass


def on_pre_tool_call(**kwargs: Any) -> Optional[dict]:
    """Return the veto directive only in ``block`` mode with missing evidence."""
    try:
        if kwargs.get("tool_name") != GATE_TOOL:
            return None                      # fast path: every other tool is free
        return _gate(kwargs.get("args") or {}, kwargs)
    except BaseException:
        return None                          # a raised hook fails open; we never raise


# ── verdict ───────────────────────────────────────────────────────────────
def _gate(args: dict, kw: dict) -> Optional[dict]:
    mode = _mode()
    if mode == "off":
        return None
    started = time.perf_counter()
    board = _board()
    task_id = _text(args.get("task_id")) or _text(kw.get("task_id")) \
        or _text(os.environ.get("HERMES_KANBAN_TASK"))
    row = _task_row(board, task_id) if task_id else {}
    problems = _problems(args, row)
    _log(board, {
        "mode": mode,
        "board": board,
        "task_id": task_id or None,
        "verdict": "insufficient_evidence" if problems else "ok",
        "problems": problems or None,
        "duration_ms": round((time.perf_counter() - started) * 1000.0, 3),
    })
    if problems and mode == "block":
        return {"action": "block", "message": "missing acceptance evidence: " + "; ".join(problems)}
    return None


def _problems(args: dict, row: dict) -> list[str]:
    problems: list[str] = []
    if not any(_text(args.get(k)) for k in ANSWER_KEYS):
        problems.append("(a) summary/result are both empty")
    if not _has_artifacts(args) and not _command_with_output(args):
        problems.append("(b) artifacts is empty and no command+output summary")
    contract = _text(row.get("completion_contract"))
    if contract and contract != LOCAL_ONLY and not _has_acceptance(args, row):
        problems.append("(c) completion_contract=%s has no matching acceptance record" % contract)
    return problems


def _has_artifacts(args: dict) -> bool:
    return any(_present(scope, ARTIFACT_KEYS) for scope in _scopes(args))


def _command_with_output(args: dict) -> bool:
    """The command and the output summary must sit on the same level (args top level or metadata)."""
    for scope in _scopes(args):
        if _present(scope, COMMAND_KEYS) and _text_any(scope, OUTPUT_KEYS):
            return True
    return False


def _scopes(args: dict) -> list[dict]:
    scopes = [args]
    meta = args.get("metadata")
    if isinstance(meta, dict):
        scopes.append(meta)
    return scopes


def _present(scope: dict, keys: tuple[str, ...]) -> bool:
    for key in keys:
        value = scope.get(key)
        if isinstance(value, str) and value.strip():
            return True
        if isinstance(value, (list, tuple)) and any(str(v).strip() for v in value):
            return True
    return False


def _text_any(scope: dict, keys: tuple[str, ...]) -> bool:
    return any(_text(scope.get(k)) for k in keys)


def _has_acceptance(args: dict, row: dict) -> bool:
    meta = args.get("metadata") if isinstance(args.get("metadata"), dict) else {}
    if _text(meta.get("published_pr")) or _text(args.get("published_pr")):
        return True
    return bool(row.get("pr_acceptance"))


# ── kernel-truth lookups ──────────────────────────────────────────────────
def _mode() -> str:
    raw = (os.environ.get("HAWKEYE_GATE_MODE") or "").strip().lower()
    return raw if raw in GATE_MODES else DEFAULT_MODE


def _board() -> str:
    env_board = (os.environ.get("HERMES_KANBAN_BOARD") or "").strip()
    if env_board:
        return env_board
    try:
        from hermes_cli.kanban_db import get_current_board
        return str(get_current_board() or "default")
    except BaseException:
        return "default"


def _db_path(board: str) -> Optional[Path]:
    """Kernel truth first; the fallback is board-aware and refuses to guess."""
    try:
        from hermes_cli.kanban_db import kanban_db_path
        return Path(kanban_db_path(board))
    except BaseException:
        pass
    return _fallback_db(board)


def _fallback_db(board: str) -> Optional[Path]:
    """No kernel: default is ``<home>/kanban.db``, a named board must already exist.

    Returning None for an unknown named board is deliberate — guessing the
    default DB would judge a card against the wrong board's contract, which is
    the same failure family as v1's path bug.
    """
    override = (os.environ.get("HERMES_KANBAN_DB") or "").strip()
    if override:
        return Path(override).expanduser()
    home = (os.environ.get("HERMES_HOME") or "").strip()
    if not home:
        return None
    root = Path(home).expanduser()
    if not board or board == "default":
        return root / "kanban.db"
    named = root / "kanban" / "boards" / board / "kanban.db"
    return named if named.is_file() else None


def _task_row(board: str, task_id: str) -> dict:
    """Read the card's completion_contract plus its acceptance event; {} when unknown."""
    db = _db_path(board)
    if db is None or not db.is_file() or not task_id:
        return {}
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=DB_BUSY_TIMEOUT)
        conn.execute("PRAGMA query_only=1")
        try:
            row = conn.execute(
                "select completion_contract from tasks where id = ?", (task_id,)).fetchone()
            if row is None:
                return {}
            accepted = conn.execute(
                "select 1 from task_events where task_id = ? and kind = 'pr_acceptance'"
                " limit 1", (task_id,)).fetchone()
            return {"completion_contract": row[0], "pr_acceptance": accepted is not None}
        finally:
            conn.close()
    except BaseException:
        return {}


def _log(board: str, rec: dict) -> None:
    """One JSONL line beside the board DB; the gate verdict is unaffected by log failure."""
    override = (os.environ.get("HAWKEYE_GATE_LOG") or "").strip()
    db = _db_path(board)
    target = Path(override).expanduser() if override else (db.parent / LOG_NAME if db else None)
    if target is None:
        return
    # Only write beside a board that actually exists. The kernel returns a path for
    # any slug it is handed, so creating the parent directory here is what used to
    # leave empty ``boards/<slug>/`` directories behind after a bad board name.
    if not override and (db is None or not db.is_file()):
        return
    line = json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                       "event": "gate", "hook": "pre_tool_call",
                       "tool": GATE_TOOL, "pid": os.getpid(), **rec},
                      ensure_ascii=False, default=str) + "\n"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""
