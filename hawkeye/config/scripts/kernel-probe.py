#!/usr/bin/env python3
"""Report whether the Hermes kernel is importable from here.

Used by ``install.sh`` to tell a fresh host whether board resolution will go through
``kanban_db_path()`` or through hawkeye's portable fallback.

Booting through ``hermes_bootstrap`` first is the whole point: ``hermes_cli.kanban_db``
needs its own dependencies (measured on the reference host: ``ruamel``), and those live
in the Hermes environment's ``site-packages``, not in whichever interpreter is running.
The interpreter alone is never enough — the boot is.

Two guards, because this runs unattended:
  * ``HERMES_HOME`` must be set. With it unset, importing hermes_cli resolves a default
    home and provisions it — measured: 1.7 GB of tools downloaded into ``~/.hermes`` by a
    single probe.
  * ``HERMES_DISABLE_LAZY_INSTALLS=1`` is held for the duration of the import (Hermes'
    own opt-out), and restored afterwards. A health check must not install anything.

Exit 0 and print ``available: <default board path>`` when it works, exit 1 and print
``unavailable: <reason>`` when it does not.
"""

import os
import sys


def main() -> int:
    src = (sys.argv[1] if len(sys.argv) > 1 else os.environ.get("HAWKEYE_KERNEL_SRC", "")).strip()
    if src and src not in sys.path:
        sys.path.insert(0, src)
    if not (os.environ.get("HERMES_HOME") or "").strip():
        print("unavailable: HERMES_HOME is not set (refusing to resolve a default home)")
        return 1

    prev = os.environ.get("HERMES_DISABLE_LAZY_INSTALLS")
    os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
    try:
        try:
            from hermes_cli import kanban_db
        except Exception:
            try:
                import hermes_bootstrap  # noqa: F401  (wires the environment's site-packages)
                from hermes_cli import kanban_db
            except Exception as exc:
                print("unavailable: %s: %s" % (exc.__class__.__name__, exc))
                return 1
        print("available: %s" % kanban_db.kanban_db_path("default"))
        return 0
    finally:
        if prev is None:
            os.environ.pop("HERMES_DISABLE_LAZY_INSTALLS", None)
        else:
            os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = prev


if __name__ == "__main__":
    raise SystemExit(main())
