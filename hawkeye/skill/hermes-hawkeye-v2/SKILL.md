---
name: hermes-hawkeye-v2
description: Use when a task runs unattended for hours or via kanban.
version: 1.0.0
category: software-development
author: eSun
license: MIT
metadata:
  hermes:
    tags: [long-running, unattended, kanban, self-healing]
    related_skills: [hermes-agent]
---

# hermes-hawkeye-v2

Project: `<REPO_DIR>` (bin/hk, standard library only, no build step).

The shape is three pieces: a **standalone program** (`bin/hk` + `lib/hawkeye/`, pure
Python >= 3.11), this **skill** (the agent-side procedure), and an optional
**plugin** (`plugins/hermes-hawkeye-gate`, the completion gate; default mode is
`block`, set `HAWKEYE_GATE_MODE=warn` to roll back to advisory).

## Repository rules (apply every time you change this project)

- The working tree is the repository; hawkeye is one subdirectory of a public MIT
  repository that will hold other Hermes tools beside it.
- **English only**: comments, docstrings, CLI output, test labels, README, config
  comments — no other language anywhere, including punctuation width.
- **No hardcoded paths**: no `/root/...`, no private IPs, no hostnames, no
  usernames. Paths come from env (`HAWKEYE_*`, `HERMES_HOME`) or from the
  placeholders `<HERMES_HOME>`, `<REPO_DIR>`, `<HERMES_SRC>`.
- After a change: both self-test suites pass, commit, push, then **verify from the
  remote** (clone or read the file back through the API) — a successful push is
  not proof that the remote content is what you think it is.
- General practice for publishing internal tools lives in the
  `opensourcing-internal-tools` skill; this file keeps only what is specific to
  hawkeye. Release mechanics for this repo — pre-flight greps, creating the remote
  without `gh`, verifying a push from the GitHub API, and what the installed-skill
  symlink does *not* cover — are in `references/publishing.md`.
- **What the docs point at, the installer must ship.** A `references/<name>.md` named in
  `SKILL.md` is a promise with two halves: the file exists in the tree, **and** `install.sh`
  copies it. Copying the directory (`cp -a "$REPO_DIR/skill/<name>/." "$SKILL_DEST/"`) is the
  only version that keeps them together; a single-file `cp` ships a contract whose pointers a
  fresh host cannot open, while the author's own runs keep working off the local copy. Assert
  after the install that every reference the skill names exists, and keep a test for the class
  (grep the names out of `SKILL.md`, compare with the tree, and check the installer copies a
  directory rather than the entry file).
- **The host's home directory is a published list; this tool adds nothing to its top.** Runtime
  artefacts live under `<HERMES_HOME>/cache/<tool>/` (the shape Hermes uses for its own tool caches:
  `cache/scratch`, `cache/<name>.json`), configuration under  `$XDG_CONFIG_HOME/<tool>/`, and
  `hk paths` prints all three so a user never has to read the source to learn what is safe to
  delete. A migration must carry *finished* runs and **name** the live ones it leaves in the old
  root (moving a directory out from under a phase is the orphan bug again); keep a test that a run
  leaves nothing but `cache/` at the home root, and one that config resolution never *requires* a
  directory inside `HERMES_HOME`. Anything a tool of this kind creates in the UI's own store (a
  session row, an index entry) is the same problem from the other side: read `hk paths`' cousins —
  the WebUI's `sessions/` directory — before claiming a deleted artefact is gone.
- **Move a path or rename a symbol, then grep for its old spelling.** The default run root moved and
  `hk --help` plus two README lines still named the old one, so the flag help pointed a user at a
  directory the tool no longer writes. Search help strings, the config example and README for the
  old path/name in the same commit; keep exactly one deliberate mention where the legacy location
  is the subject (the helper that *names* runs left behind in the old root has to describe it).

## When to use

- The task will outlive one conversation turn and must not die when a turn times
  out or is interrupted.
- The task is dispatched to subagents (kanban) and you have to supervise them and
  report the outcome back into the originating session.
- You need "the work keeps running even if I am gone, and no report is lost".

## Three hard constraints (do not invent your own wake channel)

| Fact | Consequence |
|---|---|
| `notify_on_complete` fires once per process exit, with no cap | Wake-ups ride **phase exit**, never `notify=[pattern]` |
| `watch_patterns` has a 15 s cooldown per session, disables after 3 cooldown hits, 8 hits per lifetime | Never use it as a continuous channel; `max_wakes` defaults to 6 |
| Every wake is a full agent turn (one turn costs roughly a full context window) | Merge and debounce reports; retry inside the phase instead of waking per failure |

## Starting work

```bash
P=<REPO_DIR>

# Long run (the job script prints "HK-MS <milestone>" lines itself)
$P/bin/hk run --id <id> -- <cmd...>

# Board supervision
$P/bin/hk kanban --id <id> --board default
```

When starting it through the terminal tool, **all three flags are required**:
`background=True, notify=True, persist_on_release=True`. `persist_on_release` is what
keeps the job alive across `/new`, compaction, and the end of the session.

## Contract when woken (do this in this order)

1. `hk lease --run <id>` — skip it and the doctor files an incident after
   `wake_grace_seconds`.
2. Read `runs/<id>/report.md` (or `hk status --run <id>`); do not replay history.
3. Reply with 1–2 lines of progress; merge several cards or milestones into one line.
4. Expand only on a terminal state (artifacts, commands, leftovers, rollback point).
5. Do not start another long run inside a wake turn — create a new run instead.

## Delayed or proactive delivery into the source session

To push a message into the current session **after** this turn ends (reminder,
scheduled notice, self-check signal), do not reach for cron — use the same
primitive:

```bash
# job script: stay quiet until this turn is over, then print the text as a milestone
sleep 75; echo "HK-MS <text to show>"
```

Start it with `hk run --id <id> -- /bin/bash <script>` (same three flags). The job
exits, the phase reports, the session wakes, and the wake turn quotes the milestone
text back to the user. The `sleep` has to be longer than your estimate of the time
left in the current turn.

## Dispatch parameters (set them on the card, never inherit them)

A card is executed by a worker that starts its own session, so nothing about the
dispatching turn reaches it unless it is written on the card. `hermes kanban create`
takes `--provider`, `--model` and `--completion-contract`; on a host with more than
one model, treat the first two as mandatory.

- **`--provider` and `--model` on every card, always together.** A worker session takes
  its model from `task.model_override` / `provider_override`, and otherwise falls back
  to the host default. The fallback is silent: the card runs, produces output, and
  nothing in the card log says the model was not the one you intended. Observed on a
  host with both an online and a local route: cards dispatched from a turn running on
  the online model came up on the local model, and the only visible symptom was
  slowness. `--provider` without `--model` is rejected, so set both or neither.
- **`--completion-contract` when the card must produce evidence.** Every card carries a
  contract: the kernel defaults it to `local-only` (`validate_contract(None)`), and clause
  (c) exempts `local-only`. So a card created without the flag cannot fail (c), however
  thin its evidence — (c)'s strictness comes from the dispatch, not from the gate mode.
  Pass `OWNER/REPO` or an exact PR URL when the card has to publish; writing
  `--completion-contract local-only` is the same as omitting the flag.
- **Re-dispatch repeats the parameters.** `reassign` and `set-model` change the profile
  and the model of an existing card; `set-model none` clears the override (model and
  provider are cleared together). A card created to replace a blocked one starts from
  nothing, so restate the overrides and the contract explicitly.

## Judgement and correction (dispatch scenarios)

- **Nothing I start may be invisible and immortal.** The user's only view is the chat window, so
  every background thing (a run, a watcher, a timer) needs two properties stated out loud when it
  is armed: **how long it lives** (a TTL) and **when it comes back to the chat**. A process that
  outlives the conversation without saying so is a resource the user cannot see and cannot reason
  about — that is the failure mode, not the leak itself. Prefer: systemd unit (visible via
  `systemctl`, dies with the host) for mechanical work, and a bounded `hk run` watcher whose exit
  wakes this session for notification.
- **Deleting a session means two stores, not one.** The WebUI keeps its own copy of every session
  under `$HERMES_HOME/webui/sessions/sessions/<id>.json` plus a row in that directory's
  `_index.json`; the kernel's `SessionDB.delete_sessions()` only clears `state.db`. Delete half and
  the session survives as a ghost row in the sidebar that opens nothing — the user sees a window you
  reported as deleted, which is worse than not deleting it. When removing a child session, remove the
  WebUI file and its index row too (back the index up first), then check `file / index / state.db`
  separately before saying it is gone.
- **A recurring producer writes into one ledger, not one child per cycle.** `hk report --parent`
  creates a child; `hk report --into <sid>` appends to an existing one, stamped per entry. A poll
  loop that reported each cycle as a fresh child would rebuild the sidebar clutter one level down.
  The loop shape: poll (0 tokens, heartbeat so stall detection sees it alive) → on change, append the
  finding with `--into`, mark it seen, **exit** → the exit wakes this session → the wake turn does
  the work and **re-arms the next window**. Exit-on-detection is not laziness: a wake is a job
  *ending*, so the window is one-shot and re-arming is the wake turn's first obligation. Never leave
  a monitor stopped without saying so in the chat.
- **A report is a child of the session that asked for the work — never a new window.** The user's
  only view is the sidebar, so an unattended loop that mints its own top-level sessions leaves them
  triaging windows after every night of work. `hk report --parent <sid>` has no "no parent" mode and
  refuses a parent id that does not exist; `watch-file --report-into <sid>` carries the parent
  through the watcher. Detached conversations are a decision the person makes in the UI, not
  something a background job decides for them. Mechanics and the verification query are in
  `references/in-session-watch.md`.
- **A decision waits in a file, not in the session todo list.** The WebUI todo list is per-session,
  written only by the agent (the UI has read paths, no write endpoint) and visible only in that
  session — which is the right default, because a list shared across projects is how a decision about
  one codebase gets applied to another. It is therefore a *view*, never the store: items that must
  survive the session that produced them go to a durable file (`<HERMES_HOME>/cache/hawkeye/inbox/
  decisions.jsonl`, one id/why/options/default per entry), which is mirrored into the session's todo
  list for the person to read when they are back, and answered by id. Two rules: a default makes
  answering cheap but **never executes on its own** (anything safe to decide alone does not belong in
  the inbox), and nothing auto-answers. Recipe and the read-only facts in
  `references/in-session-watch.md`.
- **cron and the watcher are two halves, not two alternatives.** cron cannot address an existing
  session (its delivery targets platforms, webui is not one, and every fire opens its own session),
  and a pure `hk run` watcher is *invisible* in the WebUI's scheduled-task list — which is exactly
  what the user reports as "I cannot see my scheduled task". When the thing is meant to be
  scheduled, build both and split by role: cron with `no_agent: true` + a script is visible, costs
  0 tokens and opens **no** session at all (measured: a silent tick took 3.4 s, 0 API calls, the
  session table unchanged), so it detects, appends the finding with `hk report --into <ledger>` and
  appends one line to a signal file; `hk run -- <hk watch-file --path <signal>>` turns a change to
  that file into the wake. One ledger, one signal, no window on either side; the wake turn re-arms
  the watcher half. Mechanics and the measured numbers are in `references/in-session-watch.md`.
- **A delegated fix is a claim until you re-derive it.** When an autonomous loop (cron, a worker, a
  subagent) reports "fixed in <commit>", treat that as a pointer, not a fact: read the commit, run
  both suites yourself, and reproduce the behaviour on a live probe. Twice in one maintenance pass
  the report was accurate but the *test* was not (a suite that could not go red), and only an
  independent run showed it.
- **Do not edit the tree a running loop is editing.** A scheduled job and this session share one
  checkout; two writers plus a `git checkout` means the other side re-writes your revert and you
  re-write its fix. Pause the schedule first, then look at `git status`/`git log` and decide whose
  implementation wins — verifying the winner beats racing it. Resume only after the tree is clean.
  **Pausing the schedule does not stop an in-flight run**: a turn that started before the pause keeps
  writing files for minutes afterwards (it re-wrote a revert three minutes later). Check the loop's
  session `last_activity_at` in `state.db` and the mtimes of the files you are about to touch, and
  wait for idle before editing or reverting.
- **Run `hk gate-check` before the first dispatch.** The gate plugin is loaded per profile,
  so an install that only touched `default` blocks the orchestrator's own completions and
  lets every worker through. Exit code 1 names the ungated profiles; `install.sh --gate
  --all-profiles` fixes them all.
- **Fail closed, but never print a fix that cannot run.** In a cron-shaped environment
  (`env -i PATH=/usr/bin:/bin`) there is no `hermes` on `PATH`, so every row reads
  `enabled=unknown → NO`: the right verdict, but the remediation printed was
  `./install.sh --gate --all-profiles`, which needs the very CLI that is missing. Keep the fail-closed
  exit code and fix the sentence: distinguish "could not determine" from "missing" — name the missing
  CLI and the override (`HAWKEYE_HERMES_BIN`), and print the install hint only for rows whose files are
  actually absent, with a test for **both** branches so the hint cannot silently disappear.
- Judge against the acceptance criteria written into the card body, never against
  the subagent's self-assessment.
- **First real dispatch: one card, default profile, whole chain.** Only after you
  have seen the gate in `block` mode not misfire should you open several cards.
- **First two real dispatches**: show the user which criteria (a)(b)(c) hit and
  which were missing for each card. After that, stop pasting per-card detail.
- Not acceptable: `kanban_block` with what is missing, then re-dispatch via a new
  card / `reassign` / `set-model`, and report one line.
- Acceptable: one line, move to the next batch.
- **Never let the work decompose its own card.** Cards are the unit of relay and the
  unit the gate judges; a worker that creates child cards moves work out from under the
  card you are about to accept, and the pipeline loses its single source of truth. If a
  card is too large, split it at design time instead — `--triage` plus
  `hermes kanban decompose`, or several sibling cards with explicit dependencies and
  one contract each.

## Operations

```bash
$P/bin/hk status --all          # state / attempts / reports / supervisor age / lease age
$P/bin/hk doctor --fix          # backstop: restart dead phases, file incidents (rc=1 = incident)
$P/bin/hk events --run <id>     # event stream
$P/bin/hk stop --run <id>       # stop the run AND the command it started
$P/bin/hk gc --days 7           # drop old finished runs (runs with an incident are kept)
```

The `--all` is not decoration: bare `hk status` prints `(no runs)` for a run that is alive and needs
no attention, which reads as "the watcher vanished". `hk run` itself never returns — it supervises in
the foreground, so a start without `background=True` hits the tool timeout, and the timeout kills the
phase it was waiting on; remove the half-made run directory before re-arming.

`hk stop` kills two process groups, not one: the `hk job` wrapper and the command it
spawned with `start_new_session=True` (the command is its own group leader, so killing
only the wrapper leaves an orphan that keeps working with nobody supervising it). After
the SIGKILL escalation it re-checks every target and **exits 1 with
`warning: still alive after stop: <pids>`** if anything survived. Treat a non-zero stop
as unresolved work, never as a log line to move past; a shell waiting on a foreground
child defers SIGTERM, which is exactly why the escalation and the re-check exist.

A stop is also a *silent* finish. `cmd_stop` writes `finished`/`finish_reason=stopped` from a
separate process, and the phase re-reads `state.json` on every poll, so it exits without
starting another attempt and without emitting a report: the wake a stop is meant to prevent
stays unspent, and the verdict stays `stopped` instead of being rewritten as `gaveup`. If a
stop returns 0 and the run later reports `attempts_exhausted`, that is this contract broken.

**Re-reading is only half of it: the write side must be atomic too.** `phase._set()` used to ask
"has anybody finished this run?" and then write, as two steps; a stop landing between them was
overwritten by the phase's in-memory copy, so the loop started another attempt, timed out, and
reported a run the operator had cancelled (event order: `attempt_failed → run_stopped →
attempt_start → gaveup → report`, `finish_reason=gaveup`). Take the same `store.file_lock()`
`cmd_stop` uses around the whole check-and-write, and refuse **only progress writes** — a phase
that is itself finishing still has to write its last counters, because the report's own counters
land *after* `_finish`, and a blanket refusal silently drops `reports` and `report.md` from every
successful run. Any change here needs both halves tested: a verdict that survives a concurrent
writer, and a normal run that still counts its report.

Two things a stop deliberately leaves behind: `state.json` keeps `job_pid` and the run
directory keeps `job.pid`, so a stopped run can still name what it stopped -- a "stopped"
that cannot name its own command has the same unaccountable shape as the orphan reports
this contract exists to prevent. There is no `job.exit` (`result.json` is absent too) because
the command was killed by us, so any exit code would be invented; `attempts_done` stays unset
(TRIES 0) for the same reason. The operator ended the run, the job did not fail.

A stop is bounded by `poll_seconds`, not immediate: `hk stop` returns as soon as the verdict is
on disk, and the phase only notices at its next tick -- with a 30 s poll the phase process can
still be exiting 30 s after `hk stop` printed `stopped`. Anything that deletes the run root
right after a stop returns has to wait one poll interval (or run `hk doctor --fix`) instead of
racing a phase that is still exiting.

Install the fallback timer (every 2 minutes, independent of the agent):

```bash
./install.sh                    # renders the units for this host and enables the timer
./install.sh --print-only       # show what it would write
```

`doctor` is an ordinary Python script: one idle sweep is about 55 ms, 19 MB RSS,
no directory creation, no network, no model call — **0 tokens**. Tokens only come
from "phase exits -> notify -> wake turn". No run means no phase, so no wake.
A monitor-gated cron tick has the same shape: the gate runs before the agent turn, so an
unchanged tick costs 0 API calls and opens no session; only a changed tick spends a turn.
When asked "is monitoring expensive", answer by layer and measure it from `state.db` — the
per-layer table and the query are in `references/in-session-watch.md`.
journald records one line per sweep; silence it with `StandardOutput=null` if the
noise bothers you.

`runs/` only grows — run `hk gc --days 7` periodically (`--dry-run` to look first,
`--force` to include runs that carry an incident). The doctor sweep prunes on the same schedule
(`retention_days`, incidents kept as evidence), so **"may this directory go?" needs exactly one
predicate and one default**: `hk gc` and `doctor._prune_old_runs` once read `delivery_grace_seconds`
with different fallbacks (300 vs 0), which made the sweep the permissive side for hand-built config
dicts. Both now call one helper, and the delivery grace itself has one meaning — a finished run whose
report has not been acknowledged yet survives the sweep (`300 s` by default, `null`/absent means the
default, an explicit `0` means "no grace").

### Issue maintenance loop (repo hygiene, separate from `hk runs`)

Inbound reports on the public repo are watched by a **bounded in-session watcher**:
`hk run --id <id> -- <script>` where the script polls `issue-watch.py --json` every few minutes
(deterministic and read-only, so an unchanged repo produces an identical tick — 0 tokens) and
prints one milestone line on news **or** on TTL expiry. `templates/ledger-watch.sh` is that loop with
the rules already baked in (heartbeat, record-then-exit, seen-state, explicit poll-failure
lines); copy it and replace the poll command and the ledger id. For the cron-split shape the same
script is the cron `no_agent` script, and `hk watch-file` is armed on the signal file it touches. The wake then lands in the chat that
armed it, and the window always closes with a question instead of going quiet. cron is only for
"nobody is here to see it": every fire opens a NEW session window and cannot deliver into an
existing one. Five rules that cost the loop silently:

- **A quiet-by-design job has to prove it is alive.** With the default `stall_seconds` (900 s) the
  phase kills any job whose `work.log` stops growing, and a watcher that only speaks on news is
  indistinguishable from a hung one: the event log shows `attempt_failed` with `reason: stalled`
  right after `attempt_start`, `last exit code: 124`, an automatic retry ~30 s later, and the same
  milestone repeated once per attempt in the delivered report — a healthy job being killed and
  restarted on a loop. Print one **plain** line per poll (`[watch] poll rc=0 at <ts>`) with no
  milestone prefix, so the file keeps moving without adding entries to the report, or set
  `stall_seconds = 0`, which now genuinely disables the check instead of meaning "kill on the
  first tick".

- **A notifier must not inherit a job's report cadence.** `report_interval_seconds` exits the phase
  to report — right for a build, wrong for a watcher (a wake every 30 min saying "still running", and
  each exit orphans the job). Use `hk run --report-interval 0` so it exits only on news or its TTL;
  the mechanism and the event-log signature are in `references/in-session-watch.md`.

- **Watch closed items too; on a closed item only a reply is news.** A poll that queries `state=open`
  goes blind exactly where a reporter's follow-up lands: a confirmation posted on a closed issue sat
  unseen for hours while the loop reported "nothing new", and that reply is the whole point of watching.
  Per state: open → a new item, an edited body or any comment is news; closed → **only** a new comment
  (closing is the maintainer's own act); closed and never seen → history, not an event. So the seen-state
  must record every item the scan *examined*, not just the ones it reported, or the first later reply
  arrives together with the item's entire comment history. Skip the per-item comment fetch when
  `updated_at` has not moved — that is most of a quiet tick's request budget.
- **Advance the seen-state strictly after the reply lands** (`--mark-seen` last), and filter
  comments authored by the token's own account (`--ignore-authors`). The watcher posts as the
  account that owns the GitHub token, so a state advanced before posting leaves your own reply
  behind as the next tick's "new comment" and the run answers itself.
- **A delivered tick whose text is a sentinel rather than watcher JSON means the
  job's monitor was swapped** (delivery tests do exactly that). Fix the job, not
  the issue: the Cron section of the `hermes-agent` skill's background-systems
  reference carries the swap/restore procedure.

## Pitfalls

- **A card created without `--provider` / `--model` runs on the host default and says
  nothing about it.** On a multi-model host that is a silent mis-dispatch rather than a
  fallback — the same failure family as the concatenated board path: no error, wrong
  result, and the only visible symptom is a latency figure that does not match what you
  expected.
- **Board resolution needs `hermes_bootstrap`, not the right interpreter.** `hermes_cli.kanban_db`
  has its own dependencies (`ruamel` and friends) that live in the Hermes *environment's*
  `site-packages`; importing the module straight from a bare interpreter fails no matter which
  python you pick — including Hermes' own bundled one. Boot through `hermes_bootstrap` first,
  exactly like Hermes' entry points do, and any Python ≥ 3.11 can host the kernel. That import
  costs about 0.8 s once per process; cache the resolved path per board, never per tick.
  `HAWKEYE_NO_KERNEL=1` forces the portable fallback when you need to compare the two.
- **Never let that boot provision anything.** With `HERMES_HOME` unset, importing `hermes_cli`
  resolves a default home and runs the lazy installer — measured: a single probe pulled 1.7 GB
  of tools into `~/.hermes`. hk therefore refuses to boot the kernel without `HERMES_HOME`, and
  holds `HERMES_DISABLE_LAZY_INSTALLS=1` around the import only (restored afterwards, not
  inherited by jobs).
- **The entry point must say its piece on a python too old to run the rest.** `hk` needs ≥ 3.11 (`tomllib`), while
  hosts that carry Hermes are not always that new (Ubuntu 18.04 → 3.6, Debian 10 → 3.7). Keep `bin/hk` and its
  resolver (`bin/_hk_interp.py`) parseable by 3.6 and let them *launch* what they find — order `HAWKEYE_PYTHON` →
  `<HERMES_HOME>/tools/python-*/bin/python3` → `installs/*/environments/*/venv/bin/python3`, each candidate proven
  by actually importing `tomllib` and `sqlite3`, because a half-built venv looks perfect to `os.path`. Never pin an
  absolute interpreter in the unit: the install hash moves on every `hermes update` and `/usr/bin/python3` differs
  per distribution. Guard the 3.6 promise with `ast.parse(src, feature_version=(3, 6))` over both files.
- **A capability probe and a re-exec both launch children, so both must scrub what they hand over.** A
  `PYTHONPATH` carrying a `sitecustomize` that reports an old version made every candidate look unusable
  (`find()` returned `None` on a healthy host); after only the probe was fixed, the *re-executed* tool
  rejected itself, because `os.execv` passes the caller's environment straight to the new interpreter and
  the last line was written by the interpreter it had just refused to use. Strip
  `PYTHONPATH`/`PYTHONHOME`/`PYTHONSTARTUP` in **one** helper, use it for the probe and for `os.execve`,
  and put the re-entry guard flag inside that dict (not in `os.environ`, or the same fact has two homes).
  When the running binary is also the only candidate, re-exec into it under the clean environment instead
  of reporting "found nothing usable" — the version it reported was the environment's, not its own. Test
  it **end-to-end**: run the real entry point as a subprocess under the hostile environment and assert the
  exit code, because a module-level assertion stayed green the whole time the CLI was red.
- **The kernel ignores `HERMES_HOME` for the board root on purpose** (`kanban_home()` uses
  `HERMES_KANBAN_HOME` or the machine root, because boards are shared across profiles). Any
  test that points `HERMES_HOME` at a fixture and then resolves a board must set
  `HAWKEYE_NO_KERNEL=1`, or it will happily read and write the real board.
- **The gate's hook `task_id` is not the card.** It is the agent's *effective* task id and can
  be a per-turn UUID, so resolve the card like the kernel does (`args.task_id` →
  `HERMES_KANBAN_TASK` → hook id) and validate each candidate against the board. An unresolved
  id must be logged as unknown (`card_known: false`), never read as "no contract" — that
  silently disables clause (c) while the verdict still looks normal.
- **The board DB path must come from the kernel's `kanban_db_path()`**: the default
  board lives at `<HERMES_HOME>/kanban.db`, and concatenating
  `boards/<slug>/kanban.db` writes zero events without any error (this is exactly
  what killed v1).
- Liveness cannot rely on `os.kill(pid, 0)`: an unreaped child is a zombie and
  still "alive". Read the state field in `/proc/<pid>/stat` and treat `Z` as dead.
- The supervisor must `poll()` its own child, otherwise a zombie leaves the phase
  waiting on a job that already finished.
- Never use `cursor == 0` as the "uninitialised" sentinel: on an empty board it is
  0, so every sweep re-aligns and swallows the first inserted event. Use an
  explicit `cursor_ready` flag.
- Build the digest before committing the board batch, or the digest always reports
  "0 new".
- **Cap restarts**: a phase that keeps crashing, restarted without a limit, becomes
  a permanent 2-minute no-op (no tokens, no progress). `doctor` caps it at
  `max_restarts` (default 5) and then writes `incident.json` (`phase_crash_loop`)
  so the exit code surfaces the problem.
- **Never guess the DB path in the fallback**: when `hermes_cli` cannot be
  imported, a named board must already exist or the resolver returns `None`;
  guessing the default DB would judge a card against the wrong board's contract —
  the same silent-failure family as the concatenated path.
- The gate plugin takes effect on `enable`: `hermes plugins enable <name>
  --no-allow-tool-override` hot-loads its hooks into the **running** gateway
  (it prints `active in the running gateway now: hooks`), no restart needed.
- But **replacing the files of an already-enabled plugin does not reload it** — the
  gateway keeps running the old module. `disable` then `enable` to hot-load again;
  the copy inside the WebUI process needs a WebUI restart.
- **A test can pass because the code it drives never ran**, a redirected install can rewrite the
  host's units, and a load harness can "prove" a race fixed without ever entering the window. All
  three live in `references/testing-and-release.md` (child-process liveness assertion, revert-callers
  grep, the verdict-integrity criterion, how to report a harness that cannot reproduce, and where a
  redirect's baseline must come from); read it before touching the race tests or `install.sh`.
- **"No leftover process" needs a sampling loop, not one `pgrep` snapshot.** A green suite can leave a
  phase alive for a sub-second window right after the run directory disappears, so a snapshot taken
  immediately after exit reports a leak and a snapshot a second later reports it fixed — same code,
  opposite verdicts. Sample every 0.5 s for ~20 s (`for i in $(seq 1 40); do pgrep -af 'bin/hk (phase|run)';
  sleep 0.5; done`) and report the measured lifetime, then repeat the suite 2–3 times: transient in
  every run means self-exiting, growing across runs means a real leak. A leftover with a *different*
  run id each time (not the one the test names) is a second fixture leaking, not the one you fixed.
- **A verdict file with two readers will disagree with itself.** `incident.json` answering "is this run in trouble"
  was read raw by `hk status` and through `store.incident_active()` by `doctor`, so after doctor withdrew an
  incident the status line still showed it open. Route every reader of "is this open" through one predicate, keep the
  raw document for the audit trail, and express the answer as an explicit boolean (`incident_open` in `--json`, a
  `(withdrawn)` marker in the note) instead of inferring it from the object being non-empty. Where the file's
  *presence* also serves as an evidence rule somewhere else (the sweep and `hk gc` keep any run carrying an
  incident), write down in the code which reading is deliberate — otherwise the next reader "fixes" the other half.
- **Reproduce the reporter's order in the fixture, not just its end state.** A withdrawn incident only exists after
  `doctor --fix` has run, so hand-writing the file and asserting on `status` tests the *open* case and looks like the
  fix failing. Drive the same writer the reporter did, and choose the run shape on purpose: a running run reaches
  `gc` as `not finished`, a finished one as `has incident` — wrong shape, wrong assertion.
- **A tool that writes into a session has to stamp activity on it.** `append_message` bumps
  `message_count` and leaves `last_activity_at` NULL, so a report-only child carries no time and reads
  as "never used" everywhere activity is displayed or sorted — what the user describes as "the session
  is empty / I cannot see it". Call `touch_session_activity()` after the write and, when asked to prove
  no new window appeared, read `parent_session_id` in `state.db` rather than the sidebar caption (the
  caption is derived from `source`). Recipes in `references/in-session-watch.md`.
- Wake turns advance on their own (measured on the WebUI: no page refresh needed).
- Use `OnCalendar=*:0/2` rather than `OnBootSec` + `OnUnitActiveSec`: after a manual
  `systemctl --user start` of the service, the latter stops scheduling the next
  elapse (`NEXT` shows empty), which makes troubleshooting blind.
- Moving the project means updating three places or it half-works: the user unit's
  `ExecStart` / `HAWKEYE_V2_CONFIG` (rerun `./install.sh`), the skill's paths, and
  both self-test suites (`daemon-reload`, then `systemctl --user start` once to
  confirm).

## Self-tests

```bash
python3 tests/selftest.py         # real subprocesses; the printed `result:` line is the verdict
python3 tests/gate_selftest.py    # synthetic board DBs, 29 checks
```

Both build their own minimal board DB through `tests/fixtures.py`; neither reads an absolute path,
so a clone runs on any host. The kernel-path branch needs `HAWKEYE_KERNEL_SRC` **and**
`HAWKEYE_TEST_UV_DEPS` — without both the related assertions are skipped rather than failed, so a
clean run does not prove the kernel branch was exercised. After a major Hermes upgrade run both
suites first (about 20 s, read-only): a failure means the kernel semantics changed. `tests/legacy/`
holds v1's silent-failure scripts (`HAWKEYE_LEGACY_PLUGIN`). The exact env values, what each suite
can and cannot prove, and why a suite that cannot go red is the real hazard are in
`references/testing-and-release.md`.

Release mechanics — proving a new test red against the old code before trusting it green,
sandbox-testing `install.sh` with a stub CLI, verifying a push through the API instead of
`raw.githubusercontent`, and the per-claim shape of answering an inbound report — are in
`references/testing-and-release.md`. The borrowed-kernel surface table (and what an upgrade
can break) is in `references/kernel-surfaces.md`. Reporting into the conversation that started
the work — TTL shape, wake latency, the report-cadence trap, child-session reports, proving no new
session window appeared, and the stop/pgrep pitfalls — is in `references/in-session-watch.md`.
