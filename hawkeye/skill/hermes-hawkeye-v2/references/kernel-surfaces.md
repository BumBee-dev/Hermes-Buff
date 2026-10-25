# Borrowed kernel surfaces and the upgrade checklist

hawkeye-v2 **does not modify the kernel**; everything below is a public surface it leans on.
After a major Hermes upgrade this table is the risk list: if a row changed, that layer's
semantics changed, and re-running the two suites shows it immediately.

## Surfaces we depend on

| Surface | Where | Key value / semantics | What breaks if it moves |
|---|---|---|---|
| `notify_on_complete` | `tools/process_registry*.py` | one event per process exit, no lifetime cap | the main wake channel dies |
| `watch_patterns` | same | `WATCH_MIN_INTERVAL_SECONDS=15`, `WATCH_STRIKE_LIMIT=3`, `WATCH_LIFETIME_MAX_HITS=8` | raise the cap and coalescing can loosen; lower it and wake count must be squeezed again |
| `heartbeat` | same | time driven, 60s minimum, no breaker | basis for periodic progress reporting |
| `persist_on_release` | terminal tool | background process survives /new, compaction, session end | phases get killed when the session winds down |
| completion delivery | `api/background_process.py` | drains `completion_queue` into a server-side `start_session_turn`, browser need not be online | wakes stop landing in the session |
| hook names | `hermes_cli/plugins.py` `VALID_HOOKS` | `pre_tool_call` / `post_tool_call` / `kanban_task_{claimed,completed,blocked}` | the plugin silently does not load |
| veto instruction | `pre_tool_call` parsing in `hermes_cli/plugins.py` | only `{"action": "block", "message": ...}` actually vetoes | the completion gate degrades to logging |
| board database path | `hermes_cli.kanban_db.kanban_db_path()` | default = `<HERMES_HOME>/kanban.db`; named board = `kanban/boards/<slug>/kanban.db` | hand-built paths means silently zero reads and writes |
| card field `completion_contract` | `hermes_cli.kanban_pr_acceptance.validate_contract()` | **absent normalises to `local-only`, not NULL** (`validate_contract(None) -> 'local-only'`); the gate exempts `local-only` from (c) | assuming "no contract means (c) does not apply" — in fact the default contract is exempt; writing `local-only` explicitly is identical to omitting it |
| card fields `model_override` / `provider_override` | `gateway/session_kanban.py` (worker session argv assembly) | injected only when the card carries them; otherwise `policy.model is None` → `_resolve_gateway_model()` falls back to the host default, **silently** | on a multi-model host the card runs on the wrong model; the only symptom is "slow", and the card log never records the model actually used |
| dispatch flag pairing | `hermes_cli.kanban_db._validate_model_override()` | `provider` without `model` → `ValueError: provider_override requires a model_override`; both create and set-model go through it | `--provider` alone fails; the two must be given together |
| dispatch flags available on create | `hermes_cli/kanban_parser.py` | `--model` / `--provider` / `--completion-contract` / `--triage`; **no** `--reasoning` | a different worker model can only be set on the card, never from the session default |
| event table | `task_events(id, task_id, run_id, kind, payload, created_at)` | incremental cursor by `id` | cursor semantics need redesigning |
| how to import the kernel | `hermes_bootstrap` (source root, pure stdlib) | `import hermes_bootstrap` first; it puts `installs/<id>/environments/<hash>/venv/lib/python3.X/site-packages` on `sys.path`. **Switching interpreters does not fix missing dependencies** (both the system python and Hermes' bundled python lack `ruamel`) | a direct `import hermes_cli.kanban_db` fails ⇒ silent fallback to the portable path |
| lazy-install trigger | `hermes_cli/venv_sync.py`; switch `HERMES_DISABLE_LAZY_INSTALLS=1` | with `HERMES_HOME` unset, importing `hermes_cli` resolves a default home and **really installs** (one probe pulled a 1.7GB toolchain into `~/.hermes`) | any import can turn into a downloader; timer jobs must set `HERMES_DISABLE_LAZY_INSTALLS` and require `HERMES_HOME` |
| board root | `hermes_cli.kanban_db.kanban_home()` | `HERMES_KANBAN_HOME` first, else the **machine default root**; it **deliberately ignores `HERMES_HOME`** (boards are shared across profiles) | a test that sets `HERMES_HOME` in a fixture reads and writes the real boards |
| plugin discovery root | `hermes_cli/plugins.py` → `get_hermes_home()` | one per profile: default = `<HERMES_HOME>/plugins`, others = `<HERMES_HOME>/profiles/<name>/plugins` | installing only into default leaves every worker profile with no gate, and nothing reports it |
| plugin "enabled" | `hermes_cli/plugins_discovery.py::_get_enabled_plugins()` → `plugins.enabled` in `config.yaml` | stored **per profile**; `hermes plugins list --json` is the authoritative read | assuming one enable applies to the whole host |
| how a worker starts | `hermes_cli/kanban_db_dispatch.py::_worker_argv()` | `[python, "-m", "hermes_cli.kanban_worker_client"]`, **without `-p`**; the profile home is passed to the worker in-process | assuming the worker rewrites `HERMES_HOME` via `hermes -p` |
| worker environment | `gateway/session_kanban.py::bind_worker_context()` | sets `HERMES_PROFILE` / `HERMES_KANBAN_*` / `HERMES_SESSION_SOURCE`, **not `HERMES_HOME`** | deciding a worker's profile from `HERMES_HOME` gets it wrong |

## The correct way to import the kernel (anywhere outside the process)

```python
src = os.environ["HAWKEYE_KERNEL_SRC"]                 # a Hermes source checkout
sys.path.insert(0, src)
prev = os.environ.get("HERMES_DISABLE_LAZY_INSTALLS")
os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = "1"        # no installs while importing
try:
    import hermes_bootstrap                             # the point: mounts the env's site-packages
    from hermes_cli import kanban_db
finally:
    if prev is None:
        os.environ.pop("HERMES_DISABLE_LAZY_INSTALLS", None)
    else:
        os.environ["HERMES_DISABLE_LAZY_INSTALLS"] = prev
```

- Check `HERMES_HOME` first; with it unset, do not boot at all (see the lazy-install row above).
- The first resolve costs about 0.8s (it pulls in the whole dependency graph); after that it is
  cached per board — 1000 lookups in 1.1ms. **Re-resolving on every tick is an incident.**
- To compare against the fallback path: `HAWKEYE_NO_KERNEL=1` (forces the portable path even when
  the kernel is importable).
- Diagnostic shape: `kernel-probe.py <src>` prints `available: <default board path>` or
  `unavailable: <reason>`; `install.sh` uses it to tell a new host which path it will take.

## Two structural facts about this deployment (architecture, not bugs)

1. **WebUI is not a gateway platform adapter.** The gateway has no `webui` adapter, therefore
   - the `platform=webui` subscription that `kanban.auto_subscribe_on_create` writes for a WebUI
     session has **nobody to deliver it**;
   - cron's `deliver=origin` also goes through an adapter and cannot reach a WebUI session.

   ⇒ do not treat "platform notification" as a wake mechanism; use `notify_on_complete`.
2. **A wake turn may need a refresh before it is visible** (only when the browser session is still
   on an older render context). A server-side turn is persisted into the session; the WebUI we
   tested advances on its own (no refresh needed), but that is not a kernel guarantee — do not put
   it in a delivery promise, and say "no refresh needed on this host" honestly in reports.

## Post-upgrade re-check

```bash
cd <project> && python3 tests/selftest.py && python3 tests/gate_selftest.py
```

Watch these in particular: the job keeps running after a SIGKILLed phase / doctor can restart and
finish it / crash loops are capped / default-board events are readable and reported exactly once /
the database path is right for both the default and a named board. Any failure means a row above
changed its semantics: fix that layer before trusting long jobs again.

## Related native surfaces we did not adopt

- `kanban_notify_subs` (`delivery_mode=notify|notify+wake|wake`) plus
  `hermes kanban notify-subscribe`: a good channel for **gateway platforms** (api_server, Telegram,
  and so on); it cannot reach a WebUI session (see above).
- `hermes kanban watch`: good for a human watching, unsuitable as a wake source (no debouncing,
  one event at a time).
