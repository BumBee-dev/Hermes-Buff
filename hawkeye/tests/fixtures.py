"""Portable test fixtures — no absolute paths, no dependency on a real install.

Both test suites need a kanban database to exercise board-path resolution and
the completion gate. Copying a live database from the machine would make the
tests unreproducible for anyone else, so this module builds a minimal one with
the exact columns the code reads.

Nothing here imports the Hermes kernel: the schema below is the contract.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                  TEXT PRIMARY KEY,
    title               TEXT,
    status               TEXT,
    assignee             TEXT,
    created_at           REAL,
    completion_contract  TEXT
);
CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id     TEXT,
    run_id      INTEGER,
    kind        TEXT,
    payload     TEXT,
    created_at  REAL
);
"""


def build_kanban_db(
    path: Path,
    tasks: Iterable[dict[str, Any]] = (),
    events: Iterable[dict[str, Any]] = (),
) -> Path:
    """Create ``path`` with the minimal kanban schema and seed it.

    ``tasks`` rows accept id/title/status/assignee/created_at/completion_contract;
    ``events`` rows accept task_id/run_id/kind/payload/created_at.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.executescript(SCHEMA)
        for task in tasks:
            row = {
                "id": task["id"],
                "title": task.get("title", task["id"]),
                "status": task.get("status", "ready"),
                "assignee": task.get("assignee", "default"),
                "created_at": task.get("created_at", 0.0),
                "completion_contract": task.get("completion_contract"),
            }
            conn.execute(
                "INSERT OR REPLACE INTO tasks"
                " (id, title, status, assignee, created_at, completion_contract)"
                " VALUES (:id, :title, :status, :assignee, :created_at, :completion_contract)",
                row,
            )
        for event in events:
            conn.execute(
                "INSERT INTO task_events (task_id, run_id, kind, payload, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    event["task_id"],
                    event.get("run_id", 1),
                    event["kind"],
                    json.dumps(event.get("payload", {})),
                    event.get("created_at", 0.0),
                ),
            )
        conn.commit()
    finally:
        conn.close()
    return path


def make_hermes_home(root: Path) -> Path:
    """A directory that the kernel accepts as a Hermes home (needs marker files)."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text("plugins:\n  enabled: []\n", encoding="utf-8")
    (root / "state.db").write_text("", encoding="utf-8")
    return root
