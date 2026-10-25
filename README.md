# Hermes-Buff

Open-source tooling for [Hermes Agent](https://hermes-agent.nousresearch.com/docs),
kept in one repository so that every tool shares a single licence, a single
review workflow and a single place to look.

Each tool lives in its own subdirectory. Tools do not share code with each
other; if two of them ever need the same helper, it gets promoted to a
top-level `shared/` package rather than copied.

## Tools

| Directory | What it does | Status |
|---|---|---|
| [`hawkeye/`](hawkeye/) | Runs long, unattended tasks and kanban work outside of any single agent turn, reports progress back into the originating session, and recovers itself when the agent, the supervisor or the host dies. | Stable |

## Getting started

Every tool is self-contained: nothing is installed into Hermes itself, and no
tool needs third-party Python packages.

```bash
# 1. Put the tool somewhere stable (any path works)
cp -a hawkeye /opt/hawkeye

# 2. Smoke-test it — portable, read-only, ~20s
cd /opt/hawkeye && python3 tests/selftest.py && python3 tests/gate_selftest.py

# 3. Optional: register the completion gate as a Hermes plugin
cp -a /opt/hawkeye/plugins/hermes-hawkeye-gate "$HERMES_HOME/plugins/"
hermes plugins enable hermes-hawkeye-gate --no-allow-tool-override

# 4. Optional: let a timer close the loop while nobody is watching
cp /opt/hawkeye/config/systemd/hawkeye-doctor.{service,timer} ~/.config/systemd/user/
#    edit ExecStart / Environment in the .service to match your paths first
systemctl --user daemon-reload && systemctl --user enable --now hawkeye-doctor.timer
```

Requirements: Python 3.11 or newer. The board-aware parts additionally need a
Hermes installation whose `hermes_cli.kanban_db` is importable (point
`HAWKEYE_KERNEL_SRC` at the source checkout when running outside Hermes).

Architecture, the operating contract and the full command reference live in
[hawkeye/README.md](hawkeye/README.md).

## Layout

```
Hermes-Buff/
├── LICENSE              MIT, applies to every tool in this repository
├── README.md            this file
└── hawkeye/             one tool, self-contained
```

Every tool subdirectory carries its own README with install, usage and
architecture notes. Read that first; this file stays deliberately short so it
never goes stale.

## Adding a new tool

1. Create a directory named after the tool.
2. Give it its own `README.md`, `tests/`, and a runnable entry point.
3. Keep its runtime state out of the repository (put it under
   `$HERMES_HOME/...` and add the path to the tool's `.gitignore`).
4. Add one row to the table above.

## Licence

MIT. See [LICENSE](LICENSE). Every tool in this repository is released under
the same terms.
