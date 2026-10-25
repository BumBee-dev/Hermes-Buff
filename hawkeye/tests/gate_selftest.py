#!/usr/bin/env python3
"""Completion-gate self-test — real DB, real plugin module; targets v1's two silent failures:

  * the default board (DB at <home>/kanban.db) can also read a card's completion_contract;
  * the gate log is written beside the board DB, no more inventing boards/default/.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins" / "hermes-hawkeye-gate" / "__init__.py"
# Optional: point at a Hermes source checkout (plus its dependency cache) to
# exercise the kernel branch. Unset means the kernel assertions are skipped and
# only the portable fallback path is covered.
KERNEL = os.environ.get("HAWKEYE_KERNEL_SRC", "").strip()
BASE = Path(tempfile.mkdtemp(prefix="hkgate-"))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fixtures  # noqa: E402  (local test helper, same directory)
HOME = BASE / "home"
FAILURES: list[str] = []


def check(label: str, ok: bool, detail: str = "") -> None:
    print("  %s %s%s" % ("PASS" if ok else "FAIL", label, "" if ok else "  <<< " + detail))
    if not ok:
        FAILURES.append(label)


def seed() -> None:
    HOME.mkdir(parents=True)
    (HOME / "config.yaml").write_text("plugins:\n  enabled: []\n")
    (HOME / "state.db").write_text("")
    fixtures.build_kanban_db(HOME / "kanban.db")
    conn = sqlite3.connect(HOME / "kanban.db")
    rows = [
        ("t_plain", None),
        ("t_contract", "acme/widgets"),
        ("t_localonly", "local-only"),
        ("t_accepted", "acme/widgets"),
    ]
    for task_id, contract in rows:
        conn.execute("insert or replace into tasks (id,title,status,assignee,created_at,"
                     "completion_contract) values (?,?,?,?,?,?)",
                     (task_id, task_id, "running", "dev-coder", 1790000000, contract))
    conn.execute("insert into task_events (task_id,run_id,kind,payload,created_at)"
                 " values ('t_accepted',1,'pr_acceptance','{}',1790000001)")
    conn.commit()
    conn.close()

    alt = HOME / "kanban" / "boards" / "alt"
    fixtures.build_kanban_db(alt / "kanban.db", tasks=[{
        "id": "t_alt", "title": "t_alt", "status": "running",
        "assignee": "dev-coder", "created_at": 1790000000,
        "completion_contract": "alt/repo"}])


def load_plugin():
    if KERNEL:
        sys.path.insert(0, KERNEL)          # in a real Hermes process this import already works
    _add_uv_deps()                          # optional: mount Hermes' uv dependency cache
    spec = importlib.util.spec_from_file_location("hkgate", PLUGIN)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _add_uv_deps() -> bool:
    """Mount Hermes' uv dependency cache when the caller points at one.

    Hermes runs from a uv-managed environment, so ``hermes_cli`` is importable
    inside it but not from a bare interpreter. ``HAWKEYE_TEST_UV_DEPS`` names
    that cache root; unset simply means the kernel branch stays untested.
    """
    cache = os.environ.get("HAWKEYE_TEST_UV_DEPS", "").strip()
    if not cache:
        return False
    for candidate in Path(cache).expanduser().glob("*/ruamel"):
        sys.path.insert(0, str(candidate.parent))
        return True
    return False


def _kernel_importable() -> bool:
    try:
        import hermes_cli.kanban_db  # noqa: F401
        return True
    except Exception:
        return False


def gate_log() -> list[dict]:
    path = HOME / "hawkeye-gate.log"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def call(mod, task_id: str, **args) -> object:
    payload = {"task_id": task_id, **args}
    return mod.on_pre_tool_call(tool_name="kanban_complete", args=payload)


def main() -> int:
    seed()
    os.environ["HERMES_HOME"] = str(HOME)
    for var in ("HERMES_KANBAN_BOARD", "HERMES_KANBAN_DB", "HAWKEYE_GATE_LOG",
                "HERMES_KANBAN_TASK", "HAWKEYE_GATE_MODE"):
        os.environ.pop(var, None)
    mod = load_plugin()

    print("# register smoke test (v1 lesson: an undefined hook gets the whole plugin skipped silently)")
    registered = []

    class Ctx:
        def register_hook(self, name, cb):
            registered.append((name, cb))

    mod.register(Ctx())
    check("register exposes pre_tool_call and it is callable",
          [n for n, _ in registered] == ["pre_tool_call"]
          and all(callable(cb) for _, cb in registered), str(registered))

    print("\n# three modes")
    os.environ.pop("HAWKEYE_GATE_MODE", None)
    verdict = call(mod, "t_plain")
    line = gate_log()[-1]
    check("default mode (env unset) = block: vetoes outright",
          isinstance(verdict, dict) and verdict.get("action") == "block", repr(verdict))
    check("default mode logs mode=block", line.get("mode") == "block", json.dumps(line))

    os.environ["HAWKEYE_GATE_MODE"] = "off"
    before = len(gate_log())
    check("off: returns None (lets it through)", call(mod, "t_plain") is None)
    check("off: writes no log line at all", len(gate_log()) == before, str(len(gate_log()) - before))

    os.environ["HAWKEYE_GATE_MODE"] = "warn"
    verdict = call(mod, "t_plain")
    line = gate_log()[-1]
    check("warn: does not change the call result (returns None)", verdict is None, repr(verdict))
    check("warn: logs insufficient_evidence and includes (a)(b)",
          line["verdict"] == "insufficient_evidence"
          and any(p.startswith("(a)") for p in line["problems"])
          and any(p.startswith("(b)") for p in line["problems"]), json.dumps(line))

    print("\n# the default board's completion_contract is readable (v1 silent failure 1)")
    verdict = call(mod, "t_contract", summary="done", metadata={"artifacts": ["/tmp/a.log"]})
    line = gate_log()[-1]
    check("warn: contract present, acceptance missing -> reports (c)", verdict is None
          and any(p.startswith("(c)") for p in line["problems"]), json.dumps(line))
    check("warn: the log creates no boards/default/ directory",
          not (HOME / "kanban" / "boards" / "default").exists())
    check("warn: the gate log sits beside the board DB", (HOME / "hawkeye-gate.log").is_file())

    os.environ["HAWKEYE_GATE_MODE"] = "block"
    verdict = call(mod, "t_contract", summary="done", metadata={"artifacts": ["/tmp/a.log"]})
    check("block: really vetoes and names (c)",
          isinstance(verdict, dict) and verdict.get("action") == "block"
          and "(c)" in verdict.get("message", ""), repr(verdict))
    verdict = call(mod, "t_accepted", summary="done", metadata={"artifacts": ["/tmp/a.log"]})
    check("block: a card with a pr_acceptance record is let through", verdict is None, repr(verdict))
    verdict = call(mod, "t_localonly")
    check("block: local-only contract is exempt from (c) but still reports (a)(b)",
          isinstance(verdict, dict) and "(c)" not in verdict.get("message", ""), repr(verdict))

    print("\n# evidence shapes")
    os.environ["HAWKEYE_GATE_MODE"] = "block"
    ok = call(mod, "t_plain", summary="finished the run",
              metadata={"command": "pytest -q", "output": "12 passed"})
    check("block: command + output summary satisfies (b)", ok is None, repr(ok))
    check("block: command with no output -> still blocked",
          isinstance(call(mod, "t_plain", summary="x", metadata={"command": "pytest -q"}), dict))
    check("block: the result field also counts for (a)",
          call(mod, "t_plain", result="see artifacts", metadata={"artifacts": ["/tmp/b"]}) is None)

    print("\n# isolation and fault tolerance")
    before = len(gate_log())
    check("non-kanban_complete tool: neither judged nor logged",
          mod.on_pre_tool_call(tool_name="terminal", args={}) is None and len(gate_log()) == before)
    check("unknown card id: a failed DB read still lets it through (no collateral damage)",
          call(mod, "t_nope", summary="x",
               metadata={"artifacts": ["/tmp/c"]}) is None)
    empty = call(mod, "")
    check("empty args: no exception, treated as missing evidence",
          empty is None or (isinstance(empty, dict) and empty.get("action") == "block"), repr(empty))

    print("\n# DB path resolution (kernel first, fallback never guesses)")
    if _kernel_importable():
        check("kernel: default -> <home>/kanban.db",
              mod._db_path("default") == HOME / "kanban.db", str(mod._db_path("default")))
        check("kernel: named board -> boards/<slug>/kanban.db",
              mod._db_path("alt") == HOME / "kanban" / "boards" / "alt" / "kanban.db",
              str(mod._db_path("alt")))
    else:
        print("  SKIP kernel path assertions: hermes_cli is not importable here")
        print("       (set HAWKEYE_KERNEL_SRC and HAWKEYE_TEST_UV_DEPS to enable them)")
    check("fallback: default goes to <home>/kanban.db", mod._fallback_db("default") == HOME / "kanban.db")
    check("fallback: an existing named board resolves",
          mod._fallback_db("alt") == HOME / "kanban" / "boards" / "alt" / "kanban.db")
    check("fallback: unknown named board returns None (never guesses the default DB)",
          mod._fallback_db("ghost") is None)

    print("\n# named board (boards/<slug>/kanban.db) and the worker's board pin")
    os.environ["HERMES_KANBAN_BOARD"] = "alt"
    verdict = call(mod, "t_alt", summary="done", metadata={"artifacts": ["/tmp/a.log"]})
    alt_log = HOME / "kanban" / "boards" / "alt" / "hawkeye-gate.log"
    alt_lines = [json.loads(ln) for ln in alt_log.read_text().splitlines() if ln.strip()] \
        if alt_log.is_file() else []
    line = alt_lines[-1] if alt_lines else {}
    check("named board: reads the contract via the pinned board and reports (c)",
          isinstance(verdict, dict) and line.get("board") == "alt"
          and any(p.startswith("(c)") for p in line.get("problems") or []), json.dumps(line))
    check("named board: the log lands in that board's DB directory (no leak to default)",
          alt_log.is_file())
    os.environ.pop("HERMES_KANBAN_BOARD", None)

    print("\n# a board that does not exist must not be created by the logger")
    bogus = HOME / "kanban" / "boards" / "no-such-board"
    shutil.rmtree(bogus, ignore_errors=True)
    os.environ["HERMES_KANBAN_BOARD"] = "no-such-board"
    os.environ["HAWKEYE_GATE_MODE"] = "block"
    before = len(gate_log())
    verdict = call(mod, "t_plain")
    check("bogus board: still judged (a call with no evidence is blocked)",
          isinstance(verdict, dict) and verdict.get("action") == "block", repr(verdict))
    check("bogus board: no boards/<slug>/ directory is left on disk", not bogus.exists(),
          str(bogus))
    check("bogus board: no log line is invented for a board that does not exist",
          len(gate_log()) == before, str(len(gate_log()) - before))
    os.environ.pop("HERMES_KANBAN_BOARD", None)

    shutil.rmtree(BASE, ignore_errors=True) if not FAILURES else None
    print("\nresult: %s" % ("all passed" if not FAILURES else "%d failed %s" % (len(FAILURES), FAILURES)))
    if FAILURES:
        print("kept the scene for inspection: %s" % BASE)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
