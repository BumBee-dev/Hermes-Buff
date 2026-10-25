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
