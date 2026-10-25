# In-session watchers: reporting into the conversation that started the work

Background work in Hermes is a **process**, not a conversation: a run, a phase or a job never
appears in the WebUI session list. So when the user asks "keep an eye on X and tell me", the
deliverable is not the watcher — it is a wake turn **in their own window**. Two properties decide
whether that works, and both belong in the sentence you say when arming it: **how long it lives**
and **when it comes back to the chat**.

## Shape

1. **Detection is mechanical.** A shell loop runs a pure-stdlib check every few minutes; an idle
   tick costs 0 tokens and touches nothing. Keep judgement out of the loop — it prints a payload
   and exits, the wake turn does the thinking.
2. **The loop owns a TTL.** It exits on exactly two conditions: news, or the window ending. Both
   print one `HK-MS` line, so both come back as a sentence in the chat instead of silence.
3. **Exit is the notification.** Job exit → phase report → `notify_on_complete` → wake turn in the
   session that started the run. All three flags are required (`background`, `notify`,
   `persist_on_release`) or the report has nowhere to land.
4. **Carry the payload in a file.** Write the fetched JSON to `pending.json` next to the state file
   so the wake turn reads facts instead of re-fetching and re-deciding what changed.

```sh
INTERVAL=${INTERVAL:-300}    # 5 min: wake latency is bounded by this
TTL=${TTL:-43200}            # 12 h: then come back and ask, do not poll forever
while :; do
    out=$(<mechanical check> ); rc=$?
    [ "$rc" -eq 10 ] && { printf '%s\n' "$out" > "$PENDING"; echo "HK-MS new: <summary>"; exit 0; }
    [ $(( $(date +%s) - started )) -ge "$TTL" ] && { echo "HK-MS window ended; say the word to re-arm"; exit 0; }
    sleep "$INTERVAL"
done
```

Reference numbers: 5-minute poll, 12-hour TTL, one wake turn per event or per TTL, idle cost 0.
A non-zero exit from the check itself (network, auth) is not fatal — keep polling, the next tick
may work; only exit on news or TTL.

## cron is the wrong tool for this

| | cron job | in-session watcher (`hk run`) |
|---|---|---|
| Lands in the current chat | no — `deliver=origin` on a WebUI session fails (`unknown platform 'webui'`), `bot-chat` goes to another window | yes |
| User-visible side effect | one **new session window per fire** | none |
| Idle cost | 0 LLM when the monitor output is byte-identical (deterministic monitor) | 0 tokens, ~a few MB RSS |
| Lifetime | until you remember to pause it | TTL, then it asks |
| Survives the session closing | yes | no (wake has nowhere to land) |

Use cron when nobody needs to see it in this chat; use the watcher when the point is the chat.

## Verifying it really works (three cheap checks)

```bash
bin/hk status --all                      # RUN / STATUS / SUP_AGE / LSE_AGE — is it alive and ticking
ps -eo pid,args | grep -c '[h]awkeye-issue-wake'   # wrapper + phase + job command expected
python3 - <<'PY'                         # did a wake land in THIS session, or open a new window?
import sqlite3, datetime
c = sqlite3.connect("file:<HERMES_HOME>/state.db?mode=ro", uri=True)
for src, title, ts in c.execute("select source, title, started_at from sessions order by rowid desc limit 5"):
    print(datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S"), src, title)
PY
```

An unchanged session table across a wake turn is the proof the report arrived in the existing
window rather than creating one.

## Pitfalls

- **A broad `pgrep` pattern kills the watcher you meant to keep.** `pgrep -f 'bin/hk (phase|run)'`
  also matches the `hk run` wrapper itself, so "cleaning orphan phases" takes down the live run.
  Match the job command line, or stop by run id — never by pattern.
- **Deleting a run directory is not stopping work.** The phase exits by design (its guard sees
  `meta.json` gone), and the job process keeps running with nobody supervising it: forget a run,
  not a worker. `hk stop --run <id>` first, then `hk gc`.
- **An exported `HAWKEYE_V2_ROOT` in your shell makes `hk status` read a different root** and print
  `(no runs)` — a phantom "the watcher vanished". Unset it before reading status, and do not leave
  it exported between steps.
- **A shell waiting on a foreground child defers SIGTERM**, so a stop that only sends TERM, or only
  kills the `hk job` wrapper group, leaves the actual command alive. Stop must kill both groups and
  escalate to SIGKILL, then re-check and fail loudly if anything survived.
- **A watcher that outlives its stop is the same bug as no TTL at all**: an orphaned bash child
  polling forever is invisible in the UI. If a stop reports success, confirm the pids are gone in
  the same breath.
