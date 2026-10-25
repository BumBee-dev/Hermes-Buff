# Legacy reproduction: hawkeye v1's silent failures

These two scripts reproduce the bugs that made the previous generation of
hawkeye (the `hermes-hawkeye` 0.2.0 bundle) fail **without any error**. They are
kept as regression evidence, not as tests of the current code: the current
implementation is expected to pass the same scenarios, and `../selftest.py` plus
`../gate_selftest.py` assert that.

Both scripts need the v1 plugin directory, which is not part of this repository:

```bash
HAWKEYE_LEGACY_PLUGIN=/path/to/v1/deployed/plugins/hermes-hawkeye \
    python3 tests/legacy/repro_v1_default_board.py
HAWKEYE_LEGACY_PLUGIN=/path/to/v1/deployed/plugins/hermes-hawkeye \
    python3 tests/legacy/repro_v1_gate_contract.py
```

Without that variable they print a skip line and exit 0, so they never break a
smoke run.

## The bug in one sentence

v1 located a board's database by string concatenation,
`boards_root()/<board>/kanban.db`. The kernel stores the **default** board's
database at `<HERMES_HOME>/kanban.db` instead, so for the default board that
path never exists — and v1's "when ownership is unknown, do not write" guard
then wrote nothing at all.

| Scenario | v1 behaviour | Consequence |
|---|---|---|
| Event stream (L1) on the default board | probes a path that cannot exist, refuses to write | the whole event stream is dead, with no error anywhere |
| Completion gate (L2) on the default board | cannot read `completion_contract`, treats it as unknown, skips clause (c) | a card with a PR contract and no acceptance record passes in `block` mode |

Both scripts demonstrate it as an A/B pair: the same card, once with the kernel
layout (`<home>/kanban.db`) and once with the named-board layout
(`<home>/kanban/boards/default/kanban.db`). The first is silent, the second
works — which is exactly how the bug survived v1's own test suite, because
every v1 test used a non-default board name.

## How the current version avoids it

`lib/hawkeye/kanban.py` resolves the path through the kernel's
`kanban_db_path(board)` and never concatenates one; when the kernel is not
importable it falls back to a board-aware lookup that returns "unknown" for a
named board that does not exist rather than guessing the default database.
