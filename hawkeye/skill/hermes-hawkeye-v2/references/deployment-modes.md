# Deployment modes: how to wire hawkeye to a task

Three modes exist, and they answer three different questions. Pick by *who has to hear about the
result*, not by which sounds modern.

| Mode | Who runs the work | Where the result lands | Reply into the conversation that asked? |
|---|---|---|---|
| **1. cron + hawkeye (default)** | the agent, in the conversation, after a wake | one child session (ledger) + a short summary in the conversation | **yes** |
| **2. cron only** | a fresh cron session (or a plain script, `no_agent`) | a file, or a delivery target that is a real platform | **no** — the delivery layer rejects `webui` |
| **3. hawkeye only** | the agent, in the conversation, after a wake | wherever the wake turn puts it | **yes** |

Two facts decide most of this, and both are measured, not assumed:

* **A wake is a process ending.** `hk run` supervises a process; when it exits, the kernel's
  `notify_on_complete` starts a turn *in the session that started the process*. That is the only
  channel that reaches a WebUI conversation.
* **cron cannot address a conversation.** Its delivery targets are platform adapters; `webui` is not
  one (`unknown platform 'webui'`, recorded in `cron/deliveries.db`). A cron run also always gets a
  fresh session, so anything it "reports" is a new window, not a reply.

Each mode below is given twice: **single profile, no board** and **multi profile with a kanban board**.
Keep the facts you rely on in one place — if a mode needs a TTL, a lease and a re-arm, say all three.

---

## Mode 1 — cron detects, hawkeye wakes, the agent works (default)

The split is: cron is the sentinel (visible in the scheduler, cheap), hawkeye is the messenger (its exit
is the wake), the agent is the worker (it has the conversation's context and can reply there).

```
cron (every N minutes, script-only)
  └─ detect            -> nothing: silent, 0 tokens, no session
                       -> something: allocate round N, keep the payload on disk
  └─ touch signal file
        └─ hk run ... watch-file --path signal  (armed earlier, bounded by TTL)
              └─ exit  ──> wake the conversation that armed it
                    └─ agent: do the work, write ONE human-readable entry per round into the
                       ledger child session, then lease and re-arm the next window
```

### 1a. Single profile, no board

* **Use when**: you watch a repository, an inbox, a file, a price page — anything where the work is
  "read, decide, fix, reply" and a person may be watching the conversation.
* **Shape**: one cron job (script only), one `hk run` watcher, one ledger child session.
* **Pros**: the conversation gets one wake and a few lines; the detail accumulates in the child
  session; idle cost is zero (no LLM, no session); no new windows ever appear.
* **Cons**: the wake needs a live conversation to land in — if the window is gone, the signal sits
  on disk until someone arms a watcher again. The work happens *in* the conversation, so a long repair
  blocks that turn. You own the TTL and the re-arm; forgetting the re-arm stops the wake half silently.
* **Watch out**: the ledger must hold conclusions, not payloads (an issue-metadata JSON dump is
  unreadable to the person it is for); the round number must come from the allocator, not from prose.

### 1b. Multi profile with a kanban board

* **Use when**: the work fans out — several workers on their own profiles pick up cards, and a human
  wants a per-card audit trail.
* **Shape**: cron detects board/repo events and writes the ledger; the gateway dispatcher does the
  dispensing; each profile carries the completion gate (`install.sh --gate --all-profiles`); `hk kanban`
  or a `watch-file` on the board is the wake; `hk gate-check` proves every profile is gated.
* **Pros**: parallel work without one conversation carrying it; the gate makes "done without evidence"
  impossible; the ledger summarises, the cards keep the authority.
* **Cons**: the most moving parts you can have — per-profile plugins, a board, a dispatcher, a wake. A
  gap in one profile means the workers on it run ungated, which is worse than no gate at all because
  it looks gated.
* **Watch out**: gate the profiles you *have*, not the ones you remember; `hk gate-check` is the
  coverage check, and it fails closed when it cannot ask the CLI.

---

## Mode 2 — cron only

* **Use when**: nobody is in a conversation, or the result belongs on disk / in Telegram / in a chat
  platform. Nightly reports, periodic scraping, "tell me at 09:00", a script that should just run.
* **Shape**: a cron job with a script; `no_agent` if no reasoning is needed (stdout is the delivery);
  `monitor` if you want the agent woken only when the output changed.
* **Pros**: simplest thing that works; needs no session, no watcher, no TTL; survives everything,
  including a closed browser. With `no_agent` plus a change detector, an idle tick costs nothing.
* **Cons**: **it cannot reply into the conversation.** Results go to a file or a platform adapter; on
  WebUI the run is a fresh window the user has to find. Every trigger is a new session, so a frequent
  schedule fragments the sidebar.
* **Watch out**: `deliver=origin` on WebUI fails; use `local` and read the file, or deliver to a real
  platform. Do not put a long task in cron — the run has a hard interruption budget.

### 2a. Single profile, no board
One job, one script, one output file. Ideal for maintenance (pull, build, verify, write a report).

### 2b. Multi profile with a kanban board
Cron scans the board and reports diffs/exception cards (blocked, crashed, gave up); the *work* is
dispensed by the gateway dispatcher, not by cron. Cards are the record; cron is only the alarm.

---

## Mode 3 — hawkeye only

* **Use when**: the trigger is not a schedule but an event you can see from the conversation — a file
  changing, a process finishing, a board moving — and someone will be there when it fires.
* **Shape**: `hk run --id <name> -- hk watch-file --path <file> --ttl <seconds>`, armed from the
  conversation with `background=True, notify=True, persist_on_release=True`.
* **Pros**: no scheduler at all; the wake is precise (a change, not a tick); the whole thing is one
  command you can read.
* **Cons**: **something has to arm it and re-arm it.** A watcher that has exited is a monitor that no
  longer exists, and only the conversation (or the doctor timer) can bring it back. Nothing polls
  between windows, so a change that happens while nothing is armed is invisible until the next arm.
* **Watch out**: give every window a new id (`issue-wake-1`, `-2`, …) — `hk run --id` refuses an id
  that already exists, so reusing one leaves the monitor down; and lease the window that just ended,
  or doctor reports `agent_unreachable` once its timer is running.

### 3a. Single profile, no board
A file watcher or a process watcher. The conversation is the only supervisor, so the TTL is the
lifetime and the wake turn is the re-arm.

### 3b. Multi profile with a kanban board
`hk kanban` supervises the board and the phase exit wakes the conversation; the gate plugin decides
whether a card may complete. The board holds the record; the wake only says "something moved".

---

## Choosing, in one table

| If this is true | Mode |
|---|---|
| A person may be in the conversation; the result must come back to it | **1** (default) or 3 |
| Nobody is there, or the window will be closed | **2** |
| The result must survive the conversation and be re-readable later | 1 or 2, with a ledger/file |
| The trigger is a schedule | 1 or 2 (never 3 alone) |
| The trigger is a file/process/board event, and polling would be waste | 3 |
| Several profiles must work in parallel with per-card evidence | 1b or 3b + `install.sh --gate --all-profiles` |
| You want the smallest thing that can possibly work | 2 |

## What every mode still owes the user

1. **A stated lifetime.** TTL for a watcher, retention for runs; "forever, silently" is not a design.
2. **A stated return point.** Where the result appears, and who says so when the monitor stops.
3. **Zero idle cost.** Polling scripts and change detectors must not spend a model call to find nothing.
4. **One record, in the reader's language.** Conclusions, not payloads; numbered rounds, not prose.
5. **A way to verify it is actually on.** `hk status --all`, `hk gate-check`, `hk doctor`, and the
   scheduler's own list — each answers a different "is it on".
