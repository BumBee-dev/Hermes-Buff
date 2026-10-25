"""hawkeye-v2 config: defaults, TOML overrides, env override for the run root."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

# Every knob in one place; no magic numbers anywhere else in the package.
DEFAULTS: dict[str, Any] = {
    # retry policy for exec runs
    "attempts": 3,
    "backoff_seconds": [30.0, 120.0, 300.0],
    "attempt_timeout_seconds": 3600.0,
    # a job whose output stops growing this long is considered hung and killed
    "stall_seconds": 900.0,
    "poll_seconds": 2.0,
    # a phase exits to report at most this often (upper bound on wake rate)
    "report_interval_seconds": 1800.0,
    # milestones printed by the job are coalesced into one report window
    "milestone_prefix": "HK-MS ",
    "milestone_flush_seconds": 120.0,
    # kanban watcher: settle window before a batch counts as reportable
    "kanban_kinds": ["completed", "blocked", "crashed", "timed_out", "gave_up", "reclaimed"],
    "kanban_quiet_seconds": 20.0,
    # self-health
    "wake_grace_seconds": 900.0,    # agent must renew its lease within this after a report
    "sup_stale_seconds": 180.0,     # doctor treats a phase silent this long as dead
    "max_wakes": 6,                 # hard budget per run (kernel allows 8 watch hits/session)
    "max_restarts": 5,              # doctor's restart budget for one run; over it, raise an incident instead of retrying
    "fallback_command": "",         # optional external page (stdin = one line)
}


def _coerce(key: str, value: Any, default: Any) -> Any:
    """Convert without silently truncating: a float override of an int default
    used to become ``int(0.3) == 0``, which turned every grace window to zero."""
    try:
        if isinstance(default, bool):
            if not isinstance(value, bool):
                raise TypeError("expected true/false")
            return value
        if isinstance(default, (int, float)):
            return float(value) if isinstance(value, float) else type(default)(value)
        if isinstance(default, list):
            if not isinstance(value, (list, tuple)):
                raise TypeError("expected a list")
            return [_coerce(key, item, default[0] if default else "") for item in value]
        return str(value)
    except (TypeError, ValueError) as exc:
        raise SystemExit("hawkeye: bad config value for %r (%r): %s" % (key, value, exc)) from exc


def load(path: str | None = None) -> dict[str, Any]:
    """Defaults <- config file. Unknown keys are rejected loudly, not ignored.

    A *missing* file is not an error: the systemd unit always exports
    ``HAWKEYE_V2_CONFIG`` so a later hand-written file gets picked up, and a
    timer that fails every tick because the optional file is absent would be
    worse than useless.
    """
    cfg = dict(DEFAULTS)
    candidate = path or os.environ.get("HAWKEYE_V2_CONFIG", "").strip() or ""
    if not candidate:
        return cfg
    file = Path(candidate).expanduser()
    if not file.is_file():
        if path:                                  # explicit path => a typo must be loud
            raise SystemExit("hawkeye: config file not found: %s" % file)
        return cfg
    with open(file, "rb") as fh:
        raw = tomllib.load(fh)
    for key, value in raw.items():
        if key not in DEFAULTS:
            raise SystemExit("hawkeye: unknown config key %r in %s" % (key, file))
        cfg[key] = _coerce(key, value, DEFAULTS[key])
    return cfg
