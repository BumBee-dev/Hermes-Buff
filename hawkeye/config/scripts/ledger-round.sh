#!/bin/bash
# ledger-round.sh — the single allocator for ledger round numbers.
#
# Why this exists as a shipped script instead of a convention: the rule that a cron tick, a wake turn
# and a human all agree on "which round is this" only holds if they all ask the same allocator. When
# the rule lived only in prose, a fresh install could not follow it at all — the reader had no way to
# tell whether to look for a helper, write one, or number by hand.
#
# Verbs:
#   peek                 print the current round number (0 when nothing has been allocated yet)
#   next                 allocate and print the next number
#   reuse N              pin the counter to N (back-filling a record written before the rule existed)
#   detect <raw.json>    allocate a round and append "第 N 轮 · 检测到新活动" with the raw payload
#   record "<title>" <body-file>
#                        append "第 N 轮 · <title>" using the current round (allocates one if none)
#
# Environment:
#   HERMES_HOME   required; state lives in <HERMES_HOME>/cache/hawkeye/issue-ledger
#   IW_STATE_DIR  override the state directory
#   IW_LEDGER     child session id the entries are appended to (required by detect/record)
#   IW_HK         the hk binary to call (default: hk from PATH)
#   IW_REPO       repository label written into a detect entry (default: unset)
#
# Allocation is `flock`-serialised on a lock file next to the counter, because two callers in one tick
# (a cron tick that detects while a wake turn records) would otherwise race on a read-modify-write and
# both get the same number.
set -u

usage() {
  sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

STATE_DIR=${IW_STATE_DIR:-${HERMES_HOME:?HERMES_HOME is required}/cache/hawkeye/issue-ledger}
COUNTER="$STATE_DIR/round.current"
LOCK="$STATE_DIR/round.lock"
HK=${IW_HK:-hk}

mkdir -p "$STATE_DIR" || { echo "ledger-round: cannot create $STATE_DIR" >&2; exit 2; }

_current() {
  local n
  n=$(cat "$COUNTER" 2>/dev/null || echo 0)
  case "$n" in ''|*[!0-9]*) n=0 ;; esac
  printf '%s' "$n"
}

# read-modify-write under one lock: peek is cheap but must not observe a half-written counter
_alloc() {
  local n
  exec 9>>"$LOCK"
  flock 9
  n=$(_current)
  n=$((n + 1))
  printf '%s\n' "$n" > "$COUNTER"
  flock -u 9
  exec 9>&-
  printf '%s' "$n"
}

_pin() {
  local n=$1
  case "$n" in ''|*[!0-9]*) echo "ledger-round: reuse needs a non-negative integer" >&2; exit 2 ;; esac
  exec 9>>"$LOCK"
  flock 9
  printf '%s\n' "$n" > "$COUNTER"
  flock -u 9
  exec 9>&-
  printf '%s' "$n"
}

_report() {
  local file=$1 title=$2
  if [ -z "${IW_LEDGER:-}" ]; then
    echo "ledger-round: IW_LEDGER is not set; refusing to write an unaddressed entry" >&2
    exit 2
  fi
  "$HK" report --into "$IW_LEDGER" --file "$file" --title "$title"
}

cmd=${1:-}
case "$cmd" in
  peek)
    _current; echo ;;
  next)
    _alloc; echo ;;
  reuse)
    [ $# -eq 2 ] || usage
    _pin "$2"; echo ;;
  detect)
    [ $# -eq 2 ] || usage
    raw=$2
    [ -f "$raw" ] || { echo "ledger-round: no such file: $raw" >&2; exit 2; }
    n=$(_alloc)
    # The payload is for the agent, not for the ledger: a JSON dump of issue metadata is unreadable to
    # the person the ledger exists for, and the ledger stops being worth opening. So the raw stays on
    # disk (where the wake turn reads it) and the ledger receives exactly one human-readable entry per
    # round, written by `record` after the work is done.
    kept="$STATE_DIR/round-$n.raw.json"
    cp "$raw" "$kept"
    printf '第 %d 轮：原文已存 %s（账本不写原文，只写结论）\n' "$n" "$kept"
    ;;
  record)
    [ $# -eq 3 ] || usage
    [ -n "${IW_LEDGER:-}" ] || { echo "ledger-round: IW_LEDGER is not set; refusing to write an unaddressed entry" >&2; exit 2; }
    title=$2
    body=$3
    [ -f "$body" ] || { echo "ledger-round: no such body: $body" >&2; exit 2; }
    n=$(_current)
    if [ "$n" -eq 0 ]; then
      # A record with no detection before it is still a round: allocate rather than write an
      # unnumbered entry, which is the drift this allocator exists to prevent.
      n=$(_alloc)
    fi
    entry="$STATE_DIR/round-$n-record.md"
    {
      printf '## 第 %d 轮 · %s\n\n' "$n" "$title"
      cat "$body"
    } > "$entry"
    _report "$entry" "第 $n 轮 · $title"
    rm -f "$entry"
    ;;
  ""|-h|--help|help)
    usage ;;
  *)
    echo "ledger-round: unknown verb: $cmd" >&2
    usage ;;
esac
