"""Find a Python >= 3.11 for hk to run under, without hardcoding a path.

Kept deliberately old-syntax (no f-strings, no walrus, no ``X | Y`` annotations): this module is
imported by ``bin/hk`` *before* it knows whether the interpreter is new enough, so on Ubuntu 18.04
or Debian 10 (python3 3.6/3.7) it has to import and run under that interpreter to say anything
useful at all.

The order is deliberate:

1. ``HAWKEYE_PYTHON`` if set -- an explicit answer wins;
2. ``$HERMES_HOME/tools/python-*/bin/python3`` -- Hermes' own managed runtime, the thing that exists
   on hosts whose system python is too old;
3. ``$HERMES_HOME/installs/*/environments/*/venv/bin/python3`` -- Hermes' per-install environments.
   Several may exist, some half-built, so each candidate is *validated* rather than trusted.

Nothing here writes anything, and every candidate is checked by actually launching it: a path that
exists but cannot import ``tomllib`` is worse than no path at all.
"""

import glob
import os
import subprocess
import sys

MIN_VERSION = (3, 11)
PROBE = "import sys, sqlite3, tomllib; sys.exit(0 if sys.version_info >= (3, 11) else 1)"


def hermes_home(env=None):
    env = os.environ if env is None else env
    value = (env.get("HERMES_HOME") or "").strip()
    if value:
        return os.path.expanduser(value)
    return os.path.join(os.path.expanduser("~"), ".hermes")


def candidates(env=None):
    """Every plausible interpreter, most-likely first, de-duplicated by real path."""
    env = os.environ if env is None else env
    home = hermes_home(env)
    found = []

    tools = sorted(glob.glob(os.path.join(home, "tools", "python-*", "bin", "python3")), reverse=True)
    found.extend(tools)

    venvs = glob.glob(os.path.join(home, "installs", "*", "environments", "*", "venv", "bin", "python3"))
    # Newest environment first: an update writes a new one and leaves the old behind.
    venvs.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    found.extend(venvs)

    seen = set()
    unique = []
    for path in found:
        real = os.path.realpath(path)
        if real in seen:
            continue
        seen.add(real)
        unique.append(path)
    return unique


PROBE_ENV_STRIP = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")


def _probe_env(env=None):
    """Environment for the probe child: the caller's, minus the variables that change who the
    interpreter *is*.

    Measured on a host where the caller exported a PYTHONPATH holding a sitecustomize that reported
    python 3.7: every candidate -- including /usr/bin/python3 and Hermes' own tools python -- was
    judged unusable and ``find()`` returned None, so a perfectly good host reported "found nothing
    usable". The verdict has to be about the interpreter, not about the shell that invoked hk, and
    cron/CI wrappers are exactly where a stray PYTHONPATH lives.
    """
    base = dict(os.environ if env is None else env)
    for key in PROBE_ENV_STRIP:
        base.pop(key, None)
    return base


def usable(path, timeout=15, env=None):
    """True when *path* is an executable interpreter that satisfies MIN_VERSION.

    Launched rather than inspected on purpose: a broken symlink, a venv whose base interpreter was
    removed, and an interpreter without ``tomllib`` all look fine to ``os.path`` and all fail here.
    """
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False
    try:
        proc = subprocess.run([path, "-c", PROBE], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=timeout,
                              env=_probe_env(env))
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def find(env=None, exclude=None):
    """The first usable interpreter that is not the one already running."""
    skip = os.path.realpath(exclude) if exclude else None
    for path in candidates(env):
        if skip and os.path.realpath(path) == skip:
            continue
        if usable(path):
            return path
    return None


def describe_searched(env=None):
    home = hermes_home(env)
    return [
        "$HERMES_HOME/tools/python-*/bin/python3  (under %s)" % home,
        "$HERMES_HOME/installs/*/environments/*/venv/bin/python3",
    ]


if __name__ == "__main__":                      # tiny self-check, handy when debugging a host
    current = (sys.version_info[0], sys.version_info[1])
    print("current : %s (%d.%d)" % (sys.executable, current[0], current[1]))
    for cand in candidates():
        print("  %-95s %s" % (cand, "usable" if usable(cand) else "not usable"))
    print("picked  : %s" % (find(exclude=sys.executable) or "(none)"))
    if current < MIN_VERSION and not find(exclude=sys.executable):
        print("searched:")
        for line in describe_searched():
            print("  " + line)
