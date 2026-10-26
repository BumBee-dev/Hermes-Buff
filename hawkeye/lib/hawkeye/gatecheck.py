"""Per-profile coverage check for the completion gate.

The gate plugin is loaded from a **profile's own** ``plugins/`` directory, and "enabled"
lives in that profile's ``config.yaml`` (``plugins.enabled``). Installing it once into
``<HERMES_HOME>/plugins`` therefore covers the ``default`` profile only — and on a host
where cards are worked by other profiles (the normal kanban shape: orchestrator in
``default``, workers in ``dev-coder`` / ``dev-tester`` / …) every worker stays ungated
while the orchestrator's own completions get blocked. Nothing reports that, which is the
same class of silent hole as the concatenated board path.

This module answers one question: which profiles are actually gated.

"Enabled" is read through the Hermes CLI (``plugins list --json``) per profile, because
that is the authoritative answer — ``config.yaml`` is YAML and this project stays
standard-library only. ``HAWKEYE_HERMES_BIN`` overrides which CLI to ask, which is also
how the tests drive it.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

from . import store

PLUGIN = "hermes-hawkeye-gate"


def profiles() -> list[tuple[str, Path]]:
    """``[(name, home)]`` — the root home is the ``default`` profile."""
    root = store.hermes_home()
    found: list[tuple[str, Path]] = [("default", root)]
    pdir = root / "profiles"
    if pdir.is_dir():
        for entry in sorted(pdir.iterdir()):
            if entry.is_dir() and (entry / "config.yaml").is_file():
                found.append((entry.name, entry))
    return found


def plugin_dir(home: Path) -> Path:
    return home / "plugins" / PLUGIN


def _cli() -> str:
    return (os.environ.get("HAWKEYE_HERMES_BIN") or "").strip() or (shutil.which("hermes") or "")


def _cli_usable() -> str:
    """The CLI we can actually run -- "" when it is missing, not a file, or not executable.

    "The variable is set" is not the same fact as "the CLI can be asked": a Hermes reinstall or a
    profile move leaves a unit carrying a stale HAWKEYE_HERMES_BIN, `enabled_state()` degrades to
    `unknown` through OSError, and the renderer -- reading only the non-empty string -- printed the
    old "reinstall the plugin" hint again for a plugin that was already installed.
    """
    exe = _cli()
    return exe if exe and os.path.isfile(exe) and os.access(exe, os.X_OK) else ""


def enabled_state(name: str) -> str:
    """``enabled`` | ``disabled`` | ``absent`` | ``unknown`` (no CLI, or it errored)."""
    exe = _cli_usable()
    if not exe:
        return "unknown"
    cmd = [exe] + ([] if name == "default" else ["-p", name]) + ["plugins", "list", "--json"]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if done.returncode != 0:
        return "unknown"
    try:
        rows = json.loads(done.stdout or "[]")
    except ValueError:
        return "unknown"
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and row.get("name") == PLUGIN:
            return "enabled" if row.get("status") == "enabled" else "disabled"
    return "absent"


def report() -> list[dict]:
    """One row per profile: where the plugin is, and whether that profile gates."""
    rows = []
    for name, home in profiles():
        pdir = plugin_dir(home)
        present = (pdir / "plugin.yaml").is_file() or (pdir / "plugin.json").is_file()
        state = enabled_state(name) if present else "absent"
        rows.append({
            "profile": name,
            "home": str(home),
            "plugin_dir": str(pdir),
            "present": present,
            "enabled": state,
            # A profile gates only when the files are there AND the profile enabled them.
            "gated": bool(present and state == "enabled"),
        })
    return rows


def gaps(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not r["gated"]]


def render(rows: list[dict]) -> str:
    if not rows:
        return "no profiles found\n"
    width = max(len(r["profile"]) for r in rows + [{"profile": "profile"}])
    lines = ["%-*s  %-8s  %-9s  %s" % (width, "profile", "files", "enabled", "gates"),
             "%s  %s  %s  %s" % ("-" * width, "-" * 8, "-" * 9, "-" * 5)]
    for r in rows:
        lines.append("%-*s  %-8s  %-9s  %s" % (
            width, r["profile"], "present" if r["present"] else "missing",
            r["enabled"], "yes" if r["gated"] else "NO"))
    gap_rows = gaps(rows)
    if gap_rows:
        lines.append("")
        lines.append("ungated: %s" % ", ".join(r["profile"] for r in gap_rows))
        # A row with the files present and no CLI to ask about is "could not determine", not
        # "missing". Printing the install hint there sends the reader after the wrong thing -- and
        # the hint itself cannot run, because it needs the CLI that is already absent.
        cannot_ask = not _cli_usable()
        if cannot_ask:
            override = (os.environ.get("HAWKEYE_HERMES_BIN") or "").strip()
            if override:
                # Naming it by what it *is*: "no CLI on PATH" would be false when the variable is set.
                lines.append("hk: HAWKEYE_HERMES_BIN=%s is set but not executable;"
                             " unset it to fall back to PATH" % override)
            else:
                lines.append('hk: no hermes CLI on PATH; set HAWKEYE_HERMES_BIN (looked for "hermes")')
        present = [r for r in gap_rows if r["present"]]
        if present and cannot_ask:
            lines.append("note   : plugin files are present on %s; the verdict is"
                         " \"could not ask\", not \"missing\"" % ", ".join(r["profile"] for r in present))
        missing_or_askable = [r for r in gap_rows if not r["present"]] or not cannot_ask
        if missing_or_askable:
            lines.append("fix    : ./install.sh --gate --all-profiles"
                         "   (or: cp -a plugins/%s <profile-home>/plugins/ +"
                         " hermes -p <profile> plugins enable %s)" % (PLUGIN, PLUGIN))
    else:
        lines.append("")
        lines.append("every profile found is gated")
    return "\n".join(lines) + "\n"
