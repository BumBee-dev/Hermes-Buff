# Publishing this tool (and siblings) to the public repo

Use when shipping a change to `Hermes-Buff`, adding a new tool beside `hawkeye/`, or
re-creating the remote on a fresh machine.

## Shape

One repository, one directory per tool, nothing shared between them:

```
Hermes-Buff/
├── LICENSE          MIT, copyright the account holder, applies to every tool
├── README.md        index: purpose, tool table, "how to add a tool"
├── .gitignore       python caches, runtime state (runs/, data/), .env
└── <tool>/          self-contained: README, bin, lib, tests, config
```

Adding a tool = new directory + one row in the index table + its runtime state path
in `.gitignore`. Do not create a second repository per tool.

## Pre-flight (before the first commit, and before every release)

```bash
# 1. No CJK anywhere, including full-width punctuation and CJK symbols
grep -rlP '[\x{3000}-\x{303f}\x{4e00}-\x{9fff}\x{f900}-\x{faff}\x{ff00}-\x{ffef}]' . \
  --exclude-dir=.git --exclude-dir=__pycache__

# 2. No machine-specific paths, internal IPs, hostnames or user names
grep -rnE '/root/|/home/[a-z]|172\.16\.|192\.168\.' . \
  --exclude-dir=.git --exclude-dir=__pycache__

# 3. No secrets
grep -rniE 'passwd|password|token|api[_-]?key|ghp_|github_pat' . --exclude-dir=.git

# 4. Executable bits survive git
git ls-files -s <tool>/bin/<entrypoint>     # expect 100755

# 5. Every path the docs promise actually exists in the tree
#    (docs drift; a doc that announces a file is judged as "file missing")
comm -23 \
  <(grep -ohE 'references/[a-z0-9-]+\.md' $(git ls-files '*.md') | sort -u) \
  <(git ls-files | grep -E 'references/' | sort -u)
# expect no output
```

Step 5 exists because a symlinked skill makes this the one failure the other checks cannot see:
the repository copy of `SKILL.md` is the file the agent loads, so every reference it names reads as
shipped until someone tries to open it. Run it after **every** edit to a doc that points at support
files, not only at release time.

Both greps must return nothing. Substitute paths with documented placeholders
(`<HERMES_HOME>`, `<REPO_DIR>`, `/opt/<tool>`, `/home/<user>`) and explain them in a
short legend section of the tool README.

## Create the remote without the `gh` CLI

The token lives in `$HERMES_HOME/.env` as `GITHUB_TOKEN`; read it into a shell variable
and never echo it.

```bash
T=$(grep -oP '^\s*GITHUB_TOKEN\s*=\s*\K.*' "$HERMES_HOME/.env" | tr -d '"' | tr -d '\r' | head -1)
curl -s -o /tmp/repo.json -w '%{http_code}\n' -X POST \
  -H "Authorization: Bearer $T" -H 'Accept: application/vnd.github+json' \
  https://api.github.com/user/repos \
  -d '{"name":"<Repo>","description":"...","private":false,"has_issues":true,"has_wiki":false}'
```

`201` = created, `422` = already exists (then just push). The response body carries
`full_name` and `html_url`; read those instead of assuming the URL.

## Push without leaking the token

Never put the token in `git remote add` — it persists in `.git/config` forever.
Use a throwaway credential store file:

```bash
printf 'https://x-access-token:%s@github.com\n' "$T" > /tmp/.gc_git && chmod 600 /tmp/.gc_git
git remote add origin https://github.com/<owner>/<Repo>.git
git -c credential.helper="store --file=/tmp/.gc_git" push -u origin main
rm -f /tmp/.gc_git
git remote -v        # must show the plain https URL, no token
```

## Verify from the remote, not from the local tree

A successful `git push` proves nothing about content. Check anonymously (also proves the
repo is really public):

```bash
curl -s https://api.github.com/repos/<owner>/<Repo> \
  | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['private'],d['default_branch'],(d.get('license') or {}).get('spdx_id'))"

curl -s "https://api.github.com/repos/<owner>/<Repo>/git/trees/main?recursive=1" \
  | python3 -c "import json,sys;print(len([t for t in json.load(sys.stdin)['tree'] if t['type']=='blob']))"

# strongest check: pull the published tarball and grep it
curl -sL https://github.com/<owner>/<Repo>/archive/refs/heads/main.tar.gz | tar xz -C /tmp
# then re-run the two greps from Pre-flight against /tmp/<Repo>-main
```

Expect `private=False`, the right licence SPDX id, a file count matching the local commit,
and zero hits from both greps.

Optional discoverability:

```bash
curl -s -X PUT -H "Authorization: Bearer $T" -H 'Accept: application/vnd.github+json' \
  https://api.github.com/repos/<owner>/<Repo>/topics \
  -d '{"names":["hermes","automation","python"]}'
```

## Translating an existing tree to English at scale

An existing tree in another language needs every comment, docstring, CLI string and test
label rewritten. Fan this out to subagents, but partition by *coupling*, because translation
changes both sides of an assertion at once:

- **Agent A**: the modules that emit user-visible messages **plus the tests that assert on
  those messages**. They must move together or the assertions drift.
- **Agent B**: library modules with no message coupling, the plugin, and its own test.
- **Agent C**: README, config templates, systemd units (these also need the placeholder-path
  rewrite, so keep them away from the code agents).

Prompt rules that actually matter:

- "Translate only; do not change logic, identifiers, dict/YAML keys, JSON field names, env var
  names, numbers or file paths — even paths that look machine-specific." Path de-hardcoding is a
  separate pass with design decisions (see below) and belongs to you, not to a translator.
- "Keep the message *shape*: same prefixes, separators, field order and column widths; only the
  language changes." Operators pattern-match on those.
- "Same set of checks in the tests, same count — labels change, meaning does not."
- Do not let two agents run the suites concurrently: a syntax error in another agent's
  half-written file reads as your failure. Tell them to run only their own suite, and only at
  the end, and to report (not fix) failures that trace outside their file list.

Per-file gate before you accept the work: CJK grep (full range, see Pre-flight) returns 0,
`py_compile` passes, that agent's suite passes.

## De-hardcoding paths (do this yourself)

A translator will faithfully preserve a bad default. After translation, remove machine paths from
*code*, not just docs:

- library default arguments: drop the default, read `HAWKEYE_*` env, and let the kernel import come
  from `sys.path` (a real Hermes process already has it);
- tests: build fixtures in-process instead of copying a live database — a test that needs a file on
  the author's disk is not a test;
- when a lookup cannot be resolved, return "unknown" and skip the check rather than guessing a
  similar path — guessing re-creates the original silent-wrong-answer bug;
- optional kernel coverage goes behind env-gated assertions that SKIP (not fail) when the kernel is
  absent.

## Moving a project into the repository

When an existing working tree is relocated into `<Repo>/<tool>/`, four things break at once:

1. the installed systemd unit (`ExecStart`, any `HAWKEYE_*` path) — patch it, `daemon-reload`,
   then `systemctl --user start` once and check `Result=success`;
2. the skill that points at the old absolute path;
3. relative imports inside the tests (add the test dir to `sys.path` explicitly);
4. documentation numbers — re-count assertions and line counts and update the README instead of
   letting docs drift from code.

Run both test suites from the new location before committing.

## Pitfalls

- Token pasted into the remote URL, or printed by an `echo` of the env var — both leak into
  `.git/config` / shell history. Use the credential-file form above and never echo `$T`.
- `__pycache__` slipping into the commit: stage, then check `git status --short | grep -c pycache`
  is 0 before committing (a `.gitignore` is not enough if files were force-added earlier).
- README claims that were true when written (test counts, line counts, default modes) silently
  become lies. Re-derive them from the code at release time.
- Committing the runtime state directory (`runs/`, `data/`) — it holds pids and timestamps from
  one machine and is useless (and confusing) anywhere else.
