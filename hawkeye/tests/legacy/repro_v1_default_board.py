#!/usr/bin/env python3
"""Regression evidence: hawkeye v1 wrote no events at all on a default board.

A/B pair with the same card:

  A. kernel layout   -> <home>/kanban.db                    (the default board)
  B. named layout    -> <home>/kanban/boards/default/kanban.db

v1 probes ``boards_root()/<board>/kanban.db`` for both, so A never resolves and
its "unknown ownership => do not write" guard silently drops the event.

Set HAWKEYE_LEGACY_PLUGIN to the v1 plugin directory to run. Exits 0 either way;
the printed verdict is the evidence.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fixtures  # noqa: E402  (shared test helper)

PLUGIN_DIR = os.environ.get("HAWKEYE_LEGACY_PLUGIN", "").strip()
TASK = "t_v1probe"
EVENT_FILE = "hawkeye-events.jsonl"


def load_v1(plugin_dir: Path):
    init = plugin_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location("hawkeye_v1", init)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load the v1 plugin from %s" % init)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build(layout: str, root: Path) -> Path:
    home = fixtures.make_hermes_home(root)
    if layout == "kernel":
        db = home / "kanban.db"
    else:
        db = home / "kanban" / "boards" / "default" / "kanban.db"
    fixtures.build_kanban_db(db, tasks=[{"id": TASK, "title": "v1 probe",
                                         "status": "running", "assignee": "dev"}])
    return home


def run(layout: str, root: Path) -> tuple[bool, Path]:
    home = build(layout, root)
    # v1 falls back to these when hermes_cli is not importable; setting both keeps
    # the reproduction deterministic on any machine.
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_KANBAN_HOME"] = str(home)
    os.environ["HERMES_KANBAN_BOARD"] = "default"
    os.environ.pop("HAWKEYE_EVENTS_DIR", None)
    plugin = load_v1(Path(PLUGIN_DIR))
    plugin.on_task_claimed(task_id=TASK, board="default", assignee="dev", run_id=1,
                           profile_name="dev")
    written = sorted(home.rglob(EVENT_FILE))
    return bool(written), (written[0] if written else home)


def main() -> int:
    if not PLUGIN_DIR or not (Path(PLUGIN_DIR) / "__init__.py").is_file():
        print("SKIP: set HAWKEYE_LEGACY_PLUGIN to the v1 plugin directory")
        return 0
    root = Path(os.environ.get("HAWKEYE_LEGACY_ROOT", "/tmp/hawkeye-legacy-events"))
    kernel_wrote, kernel_path = run("kernel", root / "kernel")
    named_wrote, named_path = run("named", root / "named")
    print("layout                       wrote?  path probed by v1")
    print("kernel  <home>/kanban.db      %-6s  %s" % (kernel_wrote, kernel_path))
    print("named   boards/default/...    %-6s  %s" % (named_wrote, named_path))
    ok = (kernel_wrote is False) and (named_wrote is True)
    print("\nverdict: %s" % ("REPRODUCED (v1 is silent on the default board)"
                             if ok else "unexpected: v1 behaved differently"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
