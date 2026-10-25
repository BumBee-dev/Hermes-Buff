#!/usr/bin/env python3
"""Regression evidence: hawkeye v1's completion gate silently passed clause (c).

Same A/B layout as ``repro_v1_default_board.py``, but the card carries a
``completion_contract`` and no acceptance record, and the gate is put in
``block`` mode. v1 reads the contract from the same concatenated path, so:

  A. kernel layout -> contract reads as "unknown" -> no (c) problem -> allowed
  B. named layout  -> contract reads fine          -> (c) problem     -> blocked

Set HAWKEYE_LEGACY_PLUGIN to the v1 plugin directory to run.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fixtures  # noqa: E402

PLUGIN_DIR = os.environ.get("HAWKEYE_LEGACY_PLUGIN", "").strip()
TASK = "t_v1gate"
CONTRACT = "acme/widgets"


def load_v1(plugin_dir: Path):
    init = plugin_dir / "__init__.py"
    spec = importlib.util.spec_from_file_location("hawkeye_v1_gate", init)
    if spec is None or spec.loader is None:
        raise SystemExit("cannot load the v1 plugin from %s" % init)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(layout: str, root: Path):
    home = fixtures.make_hermes_home(root)
    db = home / "kanban.db" if layout == "kernel" else \
        home / "kanban" / "boards" / "default" / "kanban.db"
    fixtures.build_kanban_db(db, tasks=[{"id": TASK, "title": "v1 gate probe",
                                         "status": "running", "assignee": "dev",
                                         "completion_contract": CONTRACT}])
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_KANBAN_HOME"] = str(home)
    os.environ["HERMES_KANBAN_BOARD"] = "default"
    os.environ["HAWKEYE_GATE_MODE"] = "block"
    plugin = load_v1(Path(PLUGIN_DIR))
    # Evidence for (a) and (b) is supplied, so only clause (c) can object.
    verdict = plugin.on_pre_tool_call(
        tool_name="kanban_complete",
        args={"task_id": TASK, "summary": "done", "metadata": {"artifacts": ["/tmp/x"]}})
    return verdict


def main() -> int:
    if not PLUGIN_DIR or not (Path(PLUGIN_DIR) / "__init__.py").is_file():
        print("SKIP: set HAWKEYE_LEGACY_PLUGIN to the v1 plugin directory")
        return 0
    root = Path(os.environ.get("HAWKEYE_LEGACY_ROOT", "/tmp/hawkeye-legacy-gate"))
    kernel_verdict = run("kernel", root / "kernel")
    named_verdict = run("named", root / "named")
    print("layout                       verdict")
    print("kernel  <home>/kanban.db      %r" % (kernel_verdict,))
    print("named   boards/default/...    %r" % (named_verdict,))
    blocked_kernel = isinstance(kernel_verdict, dict) and kernel_verdict.get("action") == "block"
    blocked_named = isinstance(named_verdict, dict) and named_verdict.get("action") == "block"
    ok = (not blocked_kernel) and blocked_named
    print("\nverdict: %s" % ("REPRODUCED (clause (c) is skipped on the default board)"
                             if ok else "unexpected: v1 behaved differently"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
