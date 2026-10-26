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
| `hk stop --run X` | Stop the run: kill the wrapper and the command, mark it `stopped` (no report, phase exits within one poll) |
| `hk gc --days 7 [--dry-run] [--force]` | Clean up expired finished runs; runs with incidents are kept by default, `--force` deletes them |
| `hk gate-check [--json]` | Per-profile completion-gate coverage; exit code 1 when any profile is ungated |
| `hk phase --run X` | (internal) Run one supervisor phase; doctor uses it to restart |

`--config FILE` / `$HAWKEYE_V2_CONFIG` select the config; `--root DIR` / `$HAWKEYE_V2_ROOT` select the run root (default `<HERMES_HOME>/hawkeye-v2/runs`).

**Interpreter and module resolution** (this is where "missing module" surprises come from):

| Variable | Default | Meaning |
|---|---|---|
| `HAWKEYE_PYTHON` | the `python3` on `PATH` | Interpreter hk re-execs into, once. Set it when the system python is older than 3.11 or incomplete; point it at Hermes' environment python. |
| `HAWKEYE_KERNEL_SRC` | unset | Hermes source checkout, so `hermes_cli.kanban_db` can be imported for board paths |
| `HAWKEYE_NO_KERNEL` | unset | `1` forces the portable fallback even when the kernel is importable — used by the tests, and handy when diagnosing |

hk itself is standard library only (needs 3.11+ for `tomllib`, plus `sqlite3`). The kernel
import is the one thing that needs third-party modules (`ruamel` among them), and those
live in the Hermes **environment's** `site-packages`, not in any interpreter: hk boots
through `hermes_bootstrap` exactly like Hermes' own entry points do, which is what puts
them on `sys.path`. So the answer to "which venv" is: none in particular — the boot picks
the environment, and any Python ≥ 3.11 can be the host for it. The first resolution costs
about 0.8 s (importing the kernel's dependency graph); the result is cached per process
afterwards (1000 lookups: 1.1 ms), so a watcher pays it once per start.

Two guards make that boot safe to run from a 2-minute timer:

* **`HERMES_HOME` must be set**, otherwise hk refuses to boot the kernel and uses the
  fallback. With it unset, importing `hermes_cli` resolves a *default* home and provisions
  it — measured: one probe downloaded 1.7 GB of tools into `~/.hermes`.
* **Lazy installs are suppressed for the duration of the import**
  (`HERMES_DISABLE_LAZY_INSTALLS=1`, Hermes' own opt-out, restored immediately after). A
  health check must never provision anything, and the variable is not inherited by the
  jobs hk spawns.

---

## 7. Operations

```bash
# Install the fallback timer (every 2 minutes) and the agent skill, rendered for this host
./install.sh --hermes-home "<HERMES_HOME>" --kernel-src "<HERMES_SRC>"
./install.sh --print-only                  # inspect the rendered units without writing
systemctl --user list-timers | grep hawkeye

# The live config lands in <HERMES_HOME>/hawkeye-v2/hawkeye.toml, created from
# config/hawkeye.toml.example. Keep it out of the repository: it belongs to the
# host, and a config inside the code tree is the thing that gets committed by
# accident.

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

### Watching the issue tracker (maintenance only)

`config/scripts/issue-watch.py` polls a repository's open issues and prints only what changed
since the last look. It is not needed to run hawkeye — it exists so the maintenance loop can be
driven by a scheduled agent turn that costs nothing while nothing happens.

```bash
GITHUB_TOKEN=… python3 config/scripts/issue-watch.py --repo OWNER/NAME --state /path/state.json
#   exit 0  nothing new
#   exit 10 new or changed items (printed: body + the comments not seen before)
#   exit 2  error (no token, HTTP failure)
#   --mark-seen   advance the state after the items have been handled
#   --json        machine-readable
```

Output is deterministic on purpose: same state + same issues = same bytes, so a scheduler can
diff it and skip the agent entirely when nothing changed (measured here: an idle tick with a
monitor gate makes **zero** model calls). The only write is `--mark-seen` on the local state
file; the tool never touches the repository and takes the token from the environment.

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

## 7b. Where hawkeye writes (and what is safe to delete)

`HERMES_HOME` is a documented, short list of directories that Hermes itself owns. A tool that adds
entries to its top level makes every future cleanup decision harder — "is this mine, is it Hermes',
does deleting it break something" — so hawkeye keeps the same shape Hermes uses for its own tool
caches (`cache/project_skill_scans`, `cache/banner_snapshot.json`, `cache/scratch`):

| What | Where | Safe to delete |
|---|---|---|
| run directories (state, events, report, incidents) | `<HERMES_HOME>/cache/hawkeye/runs/` | yes, when no phase is alive |
| anything else the tool produces at runtime | `<HERMES_HOME>/cache/hawkeye/` | yes |
| configuration | `$XDG_CONFIG_HOME/hawkeye/hawkeye.toml` (default `~/.config/hawkeye/hawkeye.toml`) | no — edit it |

```bash
$ ./bin/hk paths
config : /home/<user>/.config/hawkeye/hawkeye.toml
runs   : <HERMES_HOME>/cache/hawkeye/runs
cache  : <HERMES_HOME>/cache/hawkeye  <- everything hk writes lives here, safe to delete when idle
```

Resolution order for the config is `$HAWKEYE_V2_CONFIG` → the XDG path → a legacy
`<HERMES_HOME>/hawkeye-v2/hawkeye.toml` **if it exists** (so an older install keeps its tuning
instead of silently reverting to defaults). `install.sh` moves that legacy file to the XDG path on
upgrade, and `hk paths --json` prints both for scripts.

Two rules follow from this, and `tests/selftest.py` enforces them:

* a run started with nothing but `HERMES_HOME` set must leave that directory with exactly one new
  entry, `cache/` — nothing at the top level (`test_nothing_is_written_at_the_hermes_home_root`);
* config must never be *required* to live inside `HERMES_HOME`
  (`test_config_resolution_prefers_xdg_and_keeps_legacy_working`).

## 7c. Reporting into the session as a child session (optional third mode)

The WebUI nests child sessions (``parent_session_id``) under their parent -- that is how subagent
work appears. A background producer can use the same shape for its reports:

```bash
hk report --parent <session-id> --file <report.md> --title "hawkeye triage report"
```

It is always a *child*: there is no "no parent" mode, and a parent that does not exist is refused
rather than turned into a new top-level session. An unattended run that mints its own windows
leaves a user triaging the sidebar after every night of work, which is the opposite of a report.
Detached conversations stay a decision made by a person in the UI.

It writes through the kernel's own ``SessionDB`` (the class the CLI and the WebUI use), never
hand-written SQL, so the session row, its counters and its message store stay consistent with the
release running on the host. The result is browsable and attributable: nested under the session
that asked for the work, collapsible, and gone from the sidebar when the parent is.

``watch-file`` can do it on the way out, which is the whole chain in one flag:

```bash
hk run --id inbox-watch -- /path/to/bin/hk watch-file \
    --path "$HERMES_HOME/cache/hawkeye/inbox/report.md" \
    --report-into <session-id> --report-title "hawkeye triage report" \
    --rotate-bytes 204800 --interval 10 --ttl 43200 --name inbox
```

Three modes, and they compose rather than replace each other:

| Mode | Where the result appears | Use when |
|---|---|---|
| wake only (`watch-file`, no ``--report-into``) | one turn in the originating session | you want a nudge, not an artefact |
| wake + child session (`--report-into`) | a turn **and** a browsable child session | default: the work should outlive the message |
| cron delivery | a session of its own | nobody is in the chat at all |

``--rotate-bytes`` archives an oversized signal file into ``<dir>/archive/`` at startup and starts
a fresh one, so a long-lived inbox cannot grow without bound. Rotation happens *before* the
digest baseline is taken -- rotating mid-watch would otherwise look like a change and the watcher
would wake itself.

## 8a. Two-process pattern: a producer that works, a watcher that wakes

Some work must continue when nobody is in the chat — an issue triage loop, a nightly audit. A run
started from the chat cannot do that (it dies with the session), and cron cannot wake a chat session
(its delivery targets platforms; WebUI is not one, and every fire creates a session of its own).
Split the two halves:

| Half | Runs where | Job | Visible how |
|---|---|---|---|
| producer | cron (`monitor`-gated) | detection + real work + append to a report file | the schedule is listed in the UI; a run appears only when the monitor output changed |
| notifier | `hk run` in the chat session | `hk watch-file --path <report>` — exit on change | the wake lands in the session that armed it |

```bash
# producer: cron job whose monitor is a deterministic detector; when it fires it does the
# work and appends one entry to the report file (the file is the contract between the halves)
# notifier: armed from the chat, exits the moment the producer writes
hk run --id inbox-watch -- /path/to/bin/hk watch-file \
    --path "$HERMES_HOME/inbox/report.md" --interval 10 --ttl 43200 --name inbox
```

Why the file and not a queue or a socket: the report is inspectable by a human after the fact, it
survives both processes, and its digest is a wake signal that cannot fire on a clock tick. Wakes
therefore mean "something was written", never "time passed" — the same property the in-session
issue watcher gets from exiting only on new issues.

`watch-file` also carries two properties every long-lived notifier needs: it prints a plain
heartbeat line each poll (so stall detection cannot mistake a quiet watcher for a hung one) and it
ends its window with a line of output rather than silence, so an expired watch becomes a question
in the chat instead of a process that quietly stopped.

## 9. Boundaries and unverified items

| Item | Status |
|---|---|
| Phase exit → WebUI server-side wake turn | **Verified end to end** (2026-10-25 `drill-verify`: a 28-second task → phase exited at 14:52:49 → the source session received a wake, and the summary carried three milestones plus the `lease: hk lease` instruction) |
| Wake-turn visibility | The wake turn appears on its own in the current WebUI build (observed 2026-10-25); no manual refresh needed |
| `hk kanban` against named boards (non-default) | **Covered end to end** by `test_named_board_is_not_the_default_board`: a completion event inserted only into `boards/alt/kanban.db` is reported, and the watcher runs with site-packages disabled so the portable path is what gets exercised. A live multi-board dispatch has not been run against a real subagent yet |
| Board resolution without the kernel (bare host) | **Fixed and regression-tested**: the fallback used to return `<home>/kanban.db` for *every* board, so a named-board watcher reported another board's cards with no error. It now mirrors the kernel layout, and a missing DB raises instead of silently reading the wrong board |
| Wake channel from cron or a bare shell | **Not available, by design.** Waking depends on Hermes' `terminal(background, notify, persist_on_release)`. Started from cron, a systemd timer, or a plain shell there is no `notify_on_complete`, so you get `hk doctor` incidents and `fallback_command` alerts, but no wake turn. Use a Hermes-managed background process for the wake path |
| Unreachable-agent incident (`agent_unreachable`) | **Verified deterministically** as of the current test suite: the test now waits for the report and for the lease to exceed `wake_grace_seconds` instead of relying on doctor startup being slow. The previous version flaked (2 of 3 runs) and the flake landed exactly on this path |
| `fallback_command` external call | Implemented, empty by default; the reference host only had a WeChat bridge, so it was not configured or exercised |
| Job output over 256KB per tick | A single tick reads at most 256KB of deltas; the rest is read on the next tick — nothing is lost, but it lags by one tick |
| A run directory removed underneath a live run | The supervision phase exits within one poll (`HK-ERROR run directory disappeared`) and does not recreate it. It does **not** kill the job child: deleting a run root means "forget this run", not "stop the work", and `hk gc` never deletes a live run — only an outside `rm` can get here. Use `hk stop --run <id>` when the intent is to stop: it kills the command's **own** process group (the wrapper starts it with `start_new_session=True`, so the wrapper's group is a different one) |

---

## 10. Self-tests

```bash
python3 tests/selftest.py         # real subprocesses, 39 checks, ~30s (hk core)
python3 tests/gate_selftest.py    # synthetic board database, 29 checks (completion-gate plugin)
```

Both suites must be green. The unreachable-agent check is timing-sensitive on purpose — it waits for
the report and for the lease to age past `wake_grace_seconds` rather than assuming process startup
covers the window, because that assumption used to make it the one flaky test in the suite.

Both board tests stop their run in a `finally` and assert nothing of theirs survives: a watcher that
has *reported* is not `finished`, so a later doctor sweep in the same suite would adopt it (that is
where the orphaned `hk phase --run kb-1` processes in #4 came from — and on a failing run the
harness keeps the temp root, so the adoptee would poll it forever).

Checking for leftovers needs a deliberate pattern: a run started as `hk --root DIR run --id X` has
neither `phase --run` nor a bare `run` at the position a naive `pgrep -f "bin/hk (phase|run)"`
expects. Use `ps -eo args | grep -E "[b]in/hk"` and filter by run id; that also catches the
`hk job --run` children.

Coverage: a failed retry reports only once / stall detection and process-group kill / no lease renewal raises an
incident / no false alarm after a lease is renewed / **the job keeps running after the phase is SIGKILLed and
doctor picks it up** / milestone merging and "no wake spam while nobody renews the lease" / **events on the
default board database are readable and reported only once** / **a named board is watched instead of the default
board, with the kernel unavailable** / `status` aggregation. The gate plugin
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
| Log | `hawkeye-gate.log` next to the board database, one JSON object per line. Each line carries `task_id_source` (`args` / `env` / `hook`) and `card_known`, so a resolution problem is visible instead of looking like "this card has no contract" |
| Cost | About 0.2–7ms per call (measured on a local board database), no network, no LLM |

Install — **per profile, and that is not optional**:

```bash
# covers the default profile AND every profile under <HERMES_HOME>/profiles/*/
./install.sh --gate --all-profiles
./install.sh --gate --profiles default,dev-coder        # or pick them explicitly

./install.sh --print-only          # anything else installs no gate; check with:
bin/hk gate-check                  # per-profile table, exit code 1 when a profile is ungated
```

**Why per profile.** The plugin is discovered from `get_hermes_home()`, and "enabled" is
`plugins.enabled` inside that profile's own `config.yaml`. Card workers do not run in your
profile: the dispatcher starts each worker against its card's assignee profile, so a gate
installed only in `default` sees the orchestrator's completions and **none** of the workers'
— strict where it does not matter, silent where it does. Nothing warns you about it, which
is why `hk gate-check` exists:

```
$ bin/hk gate-check
profile     files     enabled    gates
----------  --------  ---------  -----
default     present   enabled    yes
dev-coder   missing   absent     NO

ungated: dev-coder
fix    : ./install.sh --gate --all-profiles
```

`HAWKEYE_GATE_MODE` is read from the process environment, so it is inherited by the workers
the gateway spawns; it is not stored per profile. If you need different modes per profile,
that is not supported today — the coverage check is the honest control instead.

**Which card is being judged.** The hook's `task_id` argument is the agent's *effective*
task id, not the card — on v0.22.5 it is sometimes a per-turn UUID. The gate therefore
resolves the card the way the kernel's own tool path does (`args.task_id` →
`HERMES_KANBAN_TASK` pin → hook id) and validates every candidate against the board before
believing it. If nothing resolves, the line says `card_known: false` rather than quietly
dropping clause (c) — a silent skip there is indistinguishable from a card without a
contract, which is how it stayed hidden until an outside report caught it (#3).

> Updating an already-enabled plugin is not automatic: copying new files over it leaves the **old module
> running in the gateway**. Re-run `hermes plugins enable <name>` (that hot-reloads hooks) or restart the gateway.
> `install.sh --gate` does both (copy, then enable) per profile for exactly this reason.

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
| `hermes_cli.kanban_db` (kernel source path) | Required for the board scenario | Taken from `HAWKEYE_KERNEL_SRC` (set it to `<HERMES_SRC>`), imported the way Hermes imports it (via `hermes_bootstrap`, so the environment's dependencies are on `sys.path`); if it still cannot be imported, fallback path resolution is used |
| Hermes kanban (including the in-gateway dispatcher) | Required for the board scenario | `kanban.dispatch_in_gateway` defaults to true |
| systemd --user | Optional | Only to run `hk doctor --fix` every 2 minutes |
| Hermes plugin system + `pre_tool_call` | Optional | Only the completion gate needs it — and it must be installed and enabled **in every profile whose workers call `kanban_complete`** (see §11); `hk gate-check` reports the gaps |

**Explicitly not needed** (the biggest difference from v1): cron, a gateway messaging platform (WeChat/Telegram),
the `api_server` Bearer key, Docker, and **no modification of the Hermes source**.

**Moving to another Hermes host:**

```bash
# 1) Copy the project (no long-running dependencies)
rsync -a hawkeye/ <newhost>:/path/hermes-tools/hawkeye/

# 2) Smoke test (read-only, no side effects)
cd /path/hermes-tools/hawkeye && python3 tests/selftest.py && python3 tests/gate_selftest.py

# 3) (Optional) completion gate in EVERY profile + fallback timer + agent skill, for THIS host
./install.sh --hermes-home "<HERMES_HOME>" --kernel-src "<HERMES_SRC>" --gate --all-profiles
#    --print-only    print the rendered units, write nothing
#    --no-enable     write the units but leave the timer disabled
#    --skip-skill    units (+ gate) only
#    --profiles a,b  gate only these profiles (default with --gate: all of them)
#    --unit-dir DIR  write the units somewhere else (test before touching ~/.config)

# 3b) after any install or upgrade: is every worker profile actually gated?
bin/hk gate-check
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
│   ├── scripts/               kernel-probe.py, issue-watch.py (maintenance/diagnostics)
│   └── systemd/               unit templates (rendered by install.sh)
├── install.sh                 renders units, installs the skill
├── tests/                     two self-test suites + v1 legacy reproducers
└── README.md
```

Note: `runs/` is state, not code — **do not copy it across hosts** (it holds that host's pids and timestamps).

### After a Hermes upgrade

The doctor timer is a systemd `--user` unit, not a Hermes-managed service, so
`hermes update` neither restarts nor removes it. That is deliberate: the fallback has
to stay up exactly while the agent's own processes bounce.

Its module resolution is also upgrade-tolerant by construction: hk needs only the
standard library itself, and when it wants the kernel it boots through
`hermes_bootstrap` (the same first import Hermes' entry points use), which puts the
environment's `site-packages` on `sys.path` at run time. Nothing pins an interpreter
version or a `site-packages` path, so a new bundled Python or a rebuilt environment
does not have to be reflected anywhere — and if the environment is gone, board lookups
degrade to the portable fallback, which mirrors the kernel layout.

What an upgrade *can* break is the coupling around it. Three checks, ten seconds:

| Check | Command | If it fails |
|---|---|---|
| Kernel still resolvable | `python3 config/scripts/kernel-probe.py <HERMES_SRC>` (expects `available: …`) | Point `--kernel-src` at the new checkout and rerun `./install.sh`; board lookups fall back in the meantime (the fallback is board-aware, so results stay correct — it just stops using the kernel) |
| Gate plugin still enabled | `hermes plugins list \| grep hawkeye` | `cp -a plugins/hermes-hawkeye-gate "<HERMES_HOME>/plugins/"` then `hermes plugins enable hermes-hawkeye-gate --no-allow-tool-override` |
| Live config still present | `test -f <HERMES_HOME>/hawkeye-v2/hawkeye.toml && echo ok` | Recreate it from `config/hawkeye.toml.example`; the built-in defaults are byte-identical to the example, so nothing changes silently in the meantime |

A run that finishes during the upgrade window can lose its single wake, because the
session it was reporting into is restarting. That case is covered: once
`wake_grace_seconds` (900 by default) passes with no renewal, `hk doctor` files
`agent_unreachable` and `fallback_command` is the out-of-band alert. The job itself is
detached and keeps running throughout the upgrade.
