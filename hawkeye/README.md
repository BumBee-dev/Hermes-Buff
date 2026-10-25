# hermes-hawkeye-v2

Long-task supervision plus reporting back to the originating session. It answers two scenarios:

1. **Dispatch**: I write tasks into the kanban board, sub-agents do the work, and I supervise automatically, judge the output against a standard, and report to the **originating session** after corrections.
2. **Single long run**: I execute one long task without interruption, post **concise progress** to the originating session at each milestone, and give a detailed reply when it finishes.

The core idea is not "polling + pushing" but **"the process is the phase stage"**: work processes live independently of my conversation turn and write their own exit codes; the supervisor exits only when it has something to say, and uses that exit to trigger a wake; continuation and fallback are handled by a `doctor` that does **not** depend on the agent.

---

## 0. Path placeholders used in this document

This repository is meant to be portable, so all machine-specific paths are written as placeholders. Substitute your own:

| Placeholder | Meaning |
|---|---|
| `<REPO_DIR>` | Checkout root of this repository (commands below assume `cd <REPO_DIR>` first) |
| `<HERMES_HOME>` | Hermes home directory (state, plugins, skills, `kanban.db`) |
| `<HERMES_SRC>` | Path to the Hermes kernel source checkout (needed only for kanban path resolution) |
| `<VENV_PYTHON>` | Any Python interpreter ≥ 3.11 you want to run the tests with |
| `$HOME` | Your home directory |

---

## 1. Three kernel hard constraints (measured, and they drive the whole design)

| Fact | Evidence | Design consequence |
|---|---|---|
| `notify_on_complete` **fires once per process exit and has no lifetime cap** | `tools/process_registry.py`, `tools/process_registry_awaiting.py` | Choose it as the wake primitive: **one phase exit = one wake** |
| `watch_patterns` per session: **15s cooldown / disabled after 3 consecutive cooldown hits / lifetime cap of 8 hits** | `tools/process_registry.py` (`WATCH_MIN_INTERVAL_SECONDS`, `WATCH_STRIKE_LIMIT`, `WATCH_LIFETIME_MAX_HITS`) | Never use it as a continuous channel; set `max_wakes` to 6 to keep headroom |
| Every wake = **one full agent turn** (a turn in this session is on the order of 100k tokens of context — cached, but not free) | Measured against the local `state.db` | Aggregate and debounce first: merge milestones, retry failures inside a phase, never wake once per failure |
| The WebUI drains `completion_queue` in `api/background_process.py` and **starts a wake turn server-side** (the browser does not need to be online); the wake turn is **pushed into the session without a manual refresh** in the current WebUI build (observed 2026-10-25) | Header comment in that file + `routes.start_session_turn` | Progress shows up on its own; older kernel comments claiming a hard refresh is required are stale |
| The default board's database lives at `<HERMES_HOME>/kanban.db`; only named boards live at `kanban/boards/<slug>/kanban.db` | `hermes_cli.kanban_db.kanban_db_path()` | Board paths **only ever go through the kernel function** (v1 died on string concatenation) |

---

## 2. Architecture

```
                 ┌──────────── source of truth (single) ────────────┐
you ──request──> me (source session) ──card/run──>  kanban.db  |  runs/<id>/{state,events,result}
                 │      ▲                        │
                 │      │wake                    │ (1) hk job (detached, writes exit code itself)
                 │  completion_queue              ▼
                 │      │                     job process (own session, outlives my turn)
                 └── report ── phase exit ── hk phase (watch / retry / judge)
                                 │
                     (2) hk doctor (systemd timer, every 2min) ── restart dead phases, file incidents
```

| Layer | Carrier | Responsibility | Idle cost |
|---|---|---|---|
| L0 source of truth | `kanban.db` / `runs/<id>/*` | Single truth; any process can rebuild the view from it | 0 |
| L1 execution | `hk job` (detached) | Runs the job, writes `work.log`, writes `job.exit` | 0 tokens |
| L2 supervision | `hk phase` | Retries, stall detection, milestone merging, decides when to report | 0 tokens |
| L3 wake | phase exit → `notify_on_complete` | Wakes me up, with a budget | One turn per wake |
| L4 fallback | `hk doctor` (timer) | Restarts dead phases; files an incident when a report was never read | 0 tokens |

---

## 3. Self-healing: who dies, who notices, how it recovers

This is the heart of the project, and the piece the previous version (v1 + cron injection) was missing.

| Failure | Who notices | Recovery action | Result |
|---|---|---|---|
| **My turn times out / is interrupted / is killed** | Nobody has to notice | Both the job and the supervisor run **outside** my turn (detached + `persist_on_release`) | Work keeps running |
| **I crash again during a wake turn** | `doctor`: `reports>0` and `lease` not renewed within `wake_grace` (900s) | File `incident.json`; optionally call out via `fallback_command` | The next time I am around, `hk status` shows it immediately |
| **Job crashes** | `phase`: `job.exit != 0` | Back off and retry up to `attempts`; when exhausted → `gaveup` + incident | One report, not one per failure |
| **Job hangs** (does not exit, produces no output) | `phase`: `work.log` mtime older than `stall_seconds` (900s) | Kill the process group (SIGTERM → SIGKILL) → record `stalled` → retry | No blind wait until timeout |
| **Supervisor phase is killed / machine reboots** | `doctor`: `sup.heartbeat` older than `sup_stale_seconds` | Restart the phase; the phase reads `state.json` and resumes | Measured: after SIGKILL of the phase the job kept running, and the restarted phase picked up and finished |
| **Supervisor phase crash-loops** | `doctor`: restart count reaches `max_restarts` | Stop restarting + incident `phase_crash_loop` | Never degenerates into an infinite restart every 2 minutes |
| **Wake never lands / I am gone entirely** | `phase` + `doctor` | The phase **does not exit** (so it does not spam wakes), keeps working to completion; the terminal report is still emitted; doctor accumulates incidents | No lost work, no report spam |
| **Notification channel rate-limits** | Avoided by design | Depends only on process exit events + a budget of 6; never uses the pattern channel with its 8-hit cap | No silent failure on the 9th wake |

> In one line: **nothing depends on the agent staying alive**. If the agent dies, the worst case is that *reporting* is delayed — work does not stop and state is not lost.

---

## 4. Usage

### Scenario 2: single long run (I execute, uninterrupted)

```bash
# The job script prints its own milestones (configurable prefix, default "HK-MS ")
bin/hk run --id my-task -- /path/to/long_job.sh
```

This is how I start it (the three parameters that matter):

```
terminal(background=True,
         notify=True,                 # process exit = one wake (no cap)
         persist_on_release=True,     # /new, compaction, session end do not kill it
         command="<REPO_DIR>/bin/hk run --id my-task -- …")
```

On each wake: `hk lease --run my-task` → read `report.md` → reply with 1–2 lines.

### Scenario 1: dispatch to the kanban board, supervise automatically

```bash
# 1) Dispatch a card (use Hermes' kanban tool; hard-code the acceptance criteria in the card body)
# 2) Start board supervision (one report per batch of events; a 20s quiet window merges concurrent completions)
bin/hk kanban --id board-watch --board default
```

After being woken: `kanban_show` to read the output → judge against the card's criteria → if it passes, report one line; if not, `kanban_block` with a reason and re-dispatch or add a card.

---

## 5. Contract with me (the agent)

1. **On every wake, call `hk lease --run <id>` first**: this is the only "I am alive" signal, and doctor uses it to decide whether to escalate an incident.
2. Keep reports short: 1–2 lines; progress reports carry no process narration.
3. Expand into detail only for terminal states (artifacts, commands, leftovers, rollback point).
4. **Never start long tasks inside a wake turn** — start another run instead.

---

## 6. Commands

| Command | Purpose |
|---|---|
| `hk run --id X -- <cmd...>` | Start an execution run (job + supervisor phase) |
| `hk kanban --id X --board default` | Start a board-supervision run |
| `hk lease --run X` / `--all` | Renew the lease (mandatory on wake) |
| `hk status --all [--json]` | Overview: state / attempts / reports / supervisor age / lease age |
| `hk doctor [--fix] [--json]` | Fallback sweep; `--fix` restarts dead phases; exit code 1 when an incident exists |
| `hk events --run X [--limit N]` | Event stream |
| `hk stop --run X` | Stop the job and mark it finished |
| `hk gc --days 7 [--dry-run] [--force]` | Clean up expired finished runs; runs with incidents are kept by default, `--force` deletes them |
| `hk phase --run X` | (internal) Run one supervisor phase; doctor uses it to restart |

`--config FILE` / `$HAWKEYE_V2_CONFIG` select the config; `--root DIR` / `$HAWKEYE_V2_ROOT` select the run root (default `<HERMES_HOME>/hawkeye-v2/runs`).

---

## 7. Operations

```bash
# Install the fallback timer (every 2 minutes) and the agent skill, rendered for this host
./install.sh --hermes-home "<HERMES_HOME>" --kernel-src "<HERMES_SRC>"
./install.sh --print-only                  # inspect the rendered units without writing
systemctl --user list-timers | grep hawkeye

# Day to day
bin/hk status --all
bin/hk doctor --fix
bin/hk gc --days 7 [--dry-run]     # clean up expired finished runs; those with incidents are kept by default
```

> The timer uses `OnCalendar=*:0/2` (every even minute) rather than `OnBootSec`+`OnUnitActiveSec`:
> after a manual `systemctl --user start` of the service, the latter does not schedule the next run
> again (measured: `NEXT` stays empty), which makes the next trigger time invisible while troubleshooting.

**Idle cost (measured on the reference host)**: with the run root absent, one `hk doctor` invocation takes
**55ms / 19MB RSS**, creates no directories, and the code makes zero network and zero model calls.
720 runs per day is roughly 40 seconds of CPU. **Token consumption is 0** — doctor is an ordinary Python
script that never goes through the agent or the model; the only thing that burns tokens is
"phase exit → wake turn", and with no runs there are no phases, hence no wakes.

---

## 8. Differences from v1 (the hermes-hawkeye 0.2.0 bundle)

| Dimension | v1 | v2 |
|---|---|---|
| Wake channel | cron + `hermes chat --resume` injecting into the session | phase exit → kernel `notify_on_complete` |
| Session busy | `SESSION_NOT_OWNED`, silently dropped | Never grabs the session; server-side wake, queued and retried when busy |
| Event source | Self-built JSONL + self-built cursor | `task_events` deltas + explicit `cursor_ready` |
| Default board path | Concatenated `boards/<slug>/kanban.db` → **silently broken** | Goes through `kanban_db_path()` (verified with a board database laid out like the real one) |
| Agent dies | cron keeps scanning, but "notification into the session" was already broken | doctor fallback + incident + optional external call |
| Work vs. reporting | Coupled (reporting only while the worker is alive) | Decoupled (job lives independently; reports can be late or re-sent) |
| Code size | 440 lines of plugin + 606 lines of sentinel + 122-line notify script | 1,296 lines of pure stdlib (plus 682 lines of self-tests), 10 scenarios, 34 assertions |

---

## 9. Boundaries and unverified items

| Item | Status |
|---|---|
| Phase exit → WebUI server-side wake turn | **Verified end to end** (2026-10-25 `drill-verify`: a 28-second task → phase exited at 14:52:49 → the source session received a wake, and the summary carried three milestones plus the `lease: hk lease` instruction) |
| Wake-turn visibility | The wake turn appears on its own in the current WebUI build (observed 2026-10-25); no manual refresh needed |
| `hk kanban` against named boards (non-default) | The path goes through the kernel function, so it is consistent in theory; **only the default board has been exercised end to end**, a named board is covered by the gate's own tests but not by a live dispatch |
| Wake channel from cron or a bare shell | **Not available, by design.** Waking depends on Hermes' `terminal(background, notify, persist_on_release)`. Started from cron, a systemd timer, or a plain shell there is no `notify_on_complete`, so you get `hk doctor` incidents and `fallback_command` alerts, but no wake turn. Use a Hermes-managed background process for the wake path |
| Unreachable-agent incident (`agent_unreachable`) | **Verified deterministically** as of the current test suite: the test now waits for the report and for the lease to exceed `wake_grace_seconds` instead of relying on doctor startup being slow. The previous version flaked (2 of 3 runs) and the flake landed exactly on this path |
| `fallback_command` external call | Implemented, empty by default; the reference host only had a WeChat bridge, so it was not configured or exercised |
| Job output over 256KB per tick | A single tick reads at most 256KB of deltas; the rest is read on the next tick — nothing is lost, but it lags by one tick |

---

## 10. Self-tests

```bash
python3 tests/selftest.py         # real subprocesses, 37 checks, ~25s (hk core)
python3 tests/gate_selftest.py    # synthetic board database, 29 checks (completion-gate plugin)
```

Both suites must be green. The unreachable-agent check is timing-sensitive on purpose — it waits for
the report and for the lease to age past `wake_grace_seconds` rather than assuming process startup
covers the window, because that assumption used to make it the one flaky test in the suite.

Coverage: a failed retry reports only once / stall detection and process-group kill / no lease renewal raises an
incident / no false alarm after a lease is renewed / **the job keeps running after the phase is SIGKILLed and
doctor picks it up** / milestone merging and "no wake spam while nobody renews the lease" / **events on the
default board database are readable and reported only once** / `status` aggregation. The gate plugin
covers the three modes, the three evidence clauses, default versus named board lookup, worker board-number
pinning, a register smoke test, and a bogus board name (no directory invented, no log line invented).

> Any Python ≥ 3.11 works; to stay isolated from the system interpreter use your environment's
> `<VENV_PYTHON> tests/selftest.py`.

---

## 11. Optional plugin: completion gate (`plugins/hermes-hawkeye-gate`)

Not part of `hk`. It is the **one component that must exist as a plugin**, because it has to hang off Hermes'
`pre_tool_call` hook and judge at the moment a sub-agent calls `kanban_complete`.

| Item | Value |
|---|---|
| Hook | `pre_tool_call` (intercepts only `kanban_complete`; every other tool returns immediately at zero cost) |
| Three modes | `HAWKEYE_GATE_MODE=off / warn / block`, **currently `block` by default** (operator decision, switched 2026-10-25; the env var overrides the default, and rollback is just setting `HAWKEYE_GATE_MODE=warn`) |
| Criteria | (a) summary/result non-empty; (b) artifacts, or a command plus an output summary; (c) if a contract exists, an acceptance record (waived for `local-only`) |
| Board lookup | Kernel `kanban_db_path(board)`; without the kernel it falls back to **accepting only an existing named board, never guessing "default"** |
| Log | `hawkeye-gate.log` next to the board database, one JSON object per line |
| Cost | About 0.2–7ms per call (measured on a local board database), no network, no LLM |

Install:

```bash
cp -a plugins/hermes-hawkeye-gate "<HERMES_HOME>/plugins/"
HERMES_HOME="$HERMES_HOME" hermes plugins enable hermes-hawkeye-gate --no-allow-tool-override
hermes plugins list | grep hawkeye        # "enabled" means it is active (the gateway hot-loads hooks)
```

> Updating an already-enabled plugin is not automatic: copying new files over it leaves the **old module
> running in the gateway**. Re-run `hermes plugins enable <name>` (that hot-reloads hooks) or restart the gateway.
> This bites exactly once, on the machine where you iterate on the plugin.

> The gate currently runs in `block`. If `hawkeye-gate.log` starts showing false positives, roll back with `HAWKEYE_GATE_MODE=warn` — no code change, no redeploy.
> The cost of `block` is that **a false positive pins the card**.

---

## 12. Dependencies and migration (what a fresh Hermes host needs)

**The shape is a combination of three things, not "a plugin":**

| Component | Form | Where it runs |
|---|---|---|
| `bin/hk` + `lib/hawkeye/` | **Standalone program** (does no plugin work) | Any shell / background process |
| `skill/hermes-hawkeye-v2/` | **skill** (my operating procedure, English) | `<HERMES_HOME>/skills/` |
| `plugins/hermes-hawkeye-gate` | **plugin** (the only part that must run inside the Hermes process) | `<HERMES_HOME>/plugins/` |

**Dependency list:**

| Dependency | Required? | Notes / consequence if missing |
|---|---|---|
| Python ≥ 3.11 | Required | Uses `tomllib`; pure standard library, **no pip packages needed** |
| Writable directory | Required | Default `<HERMES_HOME>/hawkeye-v2/runs`, changeable via `HAWKEYE_V2_ROOT` |
| Hermes `terminal(background, notify, persist_on_release)` | Required for the scenarios | The wake channel. Built into Hermes, nothing extra to install |
| `hermes_cli.kanban_db` (kernel source path) | Required for the board scenario | Taken from `HAWKEYE_KERNEL_SRC` (set it to `<HERMES_SRC>`); if unreadable, fallback path resolution is used |
| Hermes kanban (including the in-gateway dispatcher) | Required for the board scenario | `kanban.dispatch_in_gateway` defaults to true |
| systemd --user | Optional | Only to run `hk doctor --fix` every 2 minutes |
| Hermes plugin system + `pre_tool_call` | Optional | Only the completion gate needs it |

**Explicitly not needed** (the biggest difference from v1): cron, a gateway messaging platform (WeChat/Telegram),
the `api_server` Bearer key, Docker, and **no modification of the Hermes source**.

**Moving to another Hermes host:**

```bash
# 1) Copy the project (no long-running dependencies)
rsync -a hawkeye/ <newhost>:/path/hermes-tools/hawkeye/

# 2) Smoke test (read-only, no side effects)
cd /path/hermes-tools/hawkeye && python3 tests/selftest.py && python3 tests/gate_selftest.py

# 3) (Optional) install the completion-gate plugin
cp -a plugins/hermes-hawkeye-gate "<HERMES_HOME>/plugins/"
hermes plugins enable hermes-hawkeye-gate --no-allow-tool-override

# 4) (Optional) fallback timer + agent skill, rendered for THIS host
./install.sh --hermes-home "<HERMES_HOME>" --kernel-src "<HERMES_SRC>" --config "<HERMES_HOME>/hawkeye-v2/hawkeye.toml"
#    --print-only    print the rendered units, write nothing
#    --no-enable     write the units but leave the timer disabled
#    --skip-skill    units only
#    --unit-dir DIR  write the units somewhere else (test before touching ~/.config)
```

No hand editing is needed anywhere: `install.sh` renders the four host-specific values
(`HERMES_HOME`, `HAWKEYE_V2_CONFIG`, `HAWKEYE_KERNEL_SRC`, `ExecStart`) from the templates in
`config/systemd/` and leaves everything else authoritative, creates the config file from the example
if it is missing, and copies `skill/hermes-hawkeye-v2/` into the Hermes skills directory.

**Tree:**

```
hawkeye/
├── bin/hk                     entry point (python3, stdlib only)
├── lib/hawkeye/               store, job, phase, doctor, kanban, config, cli
├── skill/hermes-hawkeye-v2/   the agent-side procedure (English)
├── plugins/hermes-hawkeye-gate/  optional completion gate
├── config/
│   ├── hawkeye.toml.example   every tunable, documented
│   └── systemd/               unit templates (rendered by install.sh)
├── install.sh                 renders units, installs the skill
├── tests/                     two self-test suites + v1 legacy reproducers
└── README.md
```

Note: `runs/` is state, not code — **do not copy it across hosts** (it holds that host's pids and timestamps).
