# Testing, installing and releasing hawkeye

Mechanics that have cost real time on this project. Follow them in order.

## 1. A regression test is not a test until it fails against the old code

Always run the new/changed test against the pre-fix code and see it red, then against the
fix and see it green. Two ways this goes wrong:

- **Green before the fix.** The test never exercised the branch you thought. Isolating the
  portable path with `python3 -S` stopped working the moment the kernel import started
  booting through `hermes_bootstrap` (bootstrap wires paths without site-packages), so the
  test silently became a kernel test and passed both ways. Force the branch explicitly
  (`HAWKEYE_NO_KERNEL=1`), never by manipulating the interpreter environment and hoping.
- **A fixture that can reach production.** Anything that resolves a board must not inherit
  the real kernel: `kanban_home()` ignores `HERMES_HOME`, so a fixture home plus a live
  kernel writes to the real board. Set the opt-out in every fixture-based board test.

Red/green procedure: `cp` the file, `git checkout --` it, run the one test, restore, rerun.

## 2. Never make a test's timing depend on process startup

A supervisor/doctor test that expects "the lease is stale" must wait for the two conditions
it depends on (the artifact exists, the clock has passed the grace window), not assume that
spawning a subprocess is slower than the window. Sharing one constant between the config
under test and the wait keeps them from drifting apart (`WAKE_GRACE`).

## 3. Installers: resolve derived defaults AFTER parsing flags

A default computed at the top of the script from `$HERMES_HOME` is stale the moment
`--hermes-home` changes it; the failure is silent because the old value still resolves.
Compute config paths, skill destinations and unit targets after the flag loop, then expand
`~`.

## 4. Test an installer in a sandbox, never against the live host

```bash
# stub the external CLI the installer calls, log every call
mkdir -p /tmp/inst/bin && cat > /tmp/inst/bin/hermes <<'SH'
#!/bin/sh
echo "$*" >> /tmp/inst/calls.log
case "$*" in *"plugins list --json"*) echo '[]' ;; *"plugins enable"*) echo enabled ;; esac
SH
chmod +x /tmp/inst/bin/hermes

PATH=/tmp/inst/bin:$PATH HERMES_HOME=/tmp/inst/home \
  ./install.sh --gate --unit-dir /tmp/inst/units --no-enable --skip-skill
```

- Always pass `--unit-dir` and a temp `HERMES_HOME`; then assert the real unit file is still
  byte-identical (`grep ExecStart` or `sha256sum`) before claiming "existing things untouched".
- Exercise both shapes: `--all-profiles` (expect every profile installed) and an explicit
  subset (expect a coverage gap reported, script still exits 0). A missing name must fail
  loudly with a non-zero code — check `$?` of the script itself, not of a pipe (`| tail`
  returns tail's status and will lie to you).
- `bash -n` first; it catches quoting damage from rapid patches.

## 5. Verifying a push: the API, not raw.githubusercontent

`raw.githubusercontent.com` is CDN-cached for minutes: a file pushed a moment ago can still
serve the old bytes, which looks exactly like a failed push. Read it back through
`GET /repos/<owner>/<repo>/contents/<path>?ref=main` (base64) or `git ls-remote` for the SHA.

Also: a substring check can pass on the wrong line. Grepping for `REPO_DIR/config/hawkeye.toml`
matched the `.example` template and made a clean file look dirty. Assert on the exact fragment
that only the new code contains.

## 6. Commit messages go through a file when they contain backticks

`git commit -m "... `python -m hermes_cli.x` ..."` executes the backticked command (it spawned a
kanban worker and stripped the text out of the message). Write the message with `write_file`
and use `git commit -F <file>`; if it already shipped, `git commit --amend -F <file>` plus
`git push --force-with-lease`, then re-verify both ends and check whether the accidental
command left residue (run dirs, logs).

## 7. One source of truth for the skill

The published skill file lives in the repository (`hawkeye/skill/hermes-hawkeye-v2/SKILL.md`);
the installed skill is a symlink to it, so the agent and the public contract cannot drift.
Editing the installed path therefore edits the repo — expect a repo diff to commit, and keep
host-specific prose out of it.

## 8. Handling an inbound bug report or PR

1. Verify every claim against the source, not against the report: open the named functions
   and quote the line that decides the behaviour.
2. Reproduce on a throwaway clone (`git clone --depth 1 --branch <branch> <fork>` under
   `/tmp`) and run both suites there; never merge on the strength of the description.
3. Separate "valid in effect" from "right mechanism". Fix the effect, and correct the
   mechanism in the reply — a rule whose reason is wrong gets misapplied later.
4. When a report exposes a silent gap, the durable fix is an installer step plus a check
   command that fails loudly; documentation alone leaves the same hole for the next host.
5. Reply after the fix lands, with the commit, the measured output, and the corrections.
   Keep the thread open until the reporter confirms on their own host.
