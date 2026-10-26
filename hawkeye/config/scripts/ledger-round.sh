#!/bin/bash
# ledger-round.sh — the single allocator for ledger round numbers.
#
# Why this exists as a shipped script instead of a convention: the rule that a cron tick, a wake turn
# and a human all agree on "which round is this" only holds if they all ask the same allocator. When
# the rule lived only in prose, a fresh install could not follow it at all.
#
# Verbs:
#   peek                 print the current round number (0 when nothing has been allocated yet)
#   next                 allocate and print the next number
#   reuse N              pin the counter to N (back-filling a record written before the rule existed)
#   detect <raw.json>    allocate a round and keep the payload on disk (the ledger is not written:
#                        an issue-metadata dump is unreadable to the person the ledger is for)
#   record "<title>" <body-file>
#                        append "第 N 轮 · <title>" using the current round (allocates one if none)
#
# Environment:
#   HERMES_HOME   required unless IW_STATE_DIR is set; state lives in
#                 <HERMES_HOME>/cache/hawkeye/issue-ledger
#   IW_STATE_DIR  override the state directory
#   IW_LEDGER     child session id the entries are appended to (required by record)
#   IW_HK         the hk binary to call (default: hk from PATH)
#   IW_REPO       repository label kept beside a detect payload (informational)
#
# Five ways to lose a number silently, all handled here:
#   * counter unparsable                -> refuse; a truncated write is not "start over at 1"
#   * counter in an unexpected place     -> say where this one is and where another one lives
#   * the append fails                   -> keep the entry file, exit non-zero, do not consume a round
#   * two callers at once                -> flock; a read and its append are one critical section
#   * nobody can tell what happened      -> state path and round number go to stderr on every call
set -u

STATE_DIR=${IW_STATE_DIR:-${HERMES_HOME:?HERMES_HOME (or IW_STATE_DIR) is required}/cache/hawkeye/issue-ledger}
COUNTER="$STATE_DIR/round.current"
CLOSED="$STATE_DIR/round.closed"     # the last round that already has a written entry
PINNED="$STATE_DIR/round.pinned"     # an explicit `reuse N` waiting to be consumed by `record`
LOCK="$STATE_DIR/round.lock"
HK=${IW_HK:-hk}
CURRENT=

_die() { echo "ledger-round: $*" >&2; exit 2; }

usage() {
  sed -n '2,31p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

mkdir -p "$STATE_DIR" || { echo "ledger-round: cannot create $STATE_DIR" >&2; exit 2; }

# Sets CURRENT. 0 means "no counter yet"; a counter that exists but cannot be read as a non-negative
# integer is a different fact (a truncated write) and must not be silently reported as "nothing
# allocated yet", or the allocator starts handing out numbers the ledger already contains.
_read_counter() {
  [ -f "$COUNTER" ] || { CURRENT=0; return 0; }
  local n
  n=$(tr -d '[:space:]' < "$COUNTER" 2>/dev/null)
  case "$n" in
    ''|*[!0-9]*) _die "counter $COUNTER is not a non-negative integer (\"$n\"); refusing to renumber" ;;
  esac
  CURRENT=$n
}

# The ledger has two writers: this script, and anyone (a wake turn, a human, another agent) calling
# `hk report --into` directly. `round.closed` only knows about the first kind, so the highest round
# actually present in the ledger is read too -- otherwise a record happily writes 第 5 轮 twice
# because it cannot see the 第 6 轮 somebody else appended.
_read_ledger_max() {
  LEDGER_MAX=0
  [ -n "${IW_LEDGER:-}" ] || return 0
  [ -f "${HERMES_HOME:-}/state.db" ] || return 0
  local out
  if ! out=$(IW_DB="${HERMES_HOME}/state.db" IW_SID="$IW_LEDGER" "${IW_PYTHON:-python3}" -c '
import os, re, sqlite3, sys
try:
    con = sqlite3.connect("file:%s?mode=ro" % os.environ["IW_DB"], uri=True)
    rows = [r[0] for r in con.execute(
        "select content from messages where session_id = ?", (os.environ["IW_SID"],))]
except Exception as exc:
    print(exc, file=sys.stderr)
    sys.exit(3)
# Count only entry *headings*. A ledger body routinely quotes round numbers ("the old bug wrote
# 第 19 轮 twice"), and an unanchored scan reads that sentence as the maximum and skips every round
# in between. Fenced blocks are dropped for the same reason.
fence = re.compile(r"```.*?```", re.S)
head = re.compile(r"^\s*(?:#{1,6}\s*)?(?:\[[^\]\n]{0,40}\]\s*)?第\s*(\d+)\s*轮", re.M)
best = 0
for text in rows:
    if not text:
        continue
    for m in head.finditer(fence.sub("", text)):
        n = int(m.group(1))
        if n > best:
            best = n
print(best)
' 2>/dev/null); then
    # "Cannot read it" must not mean "it is empty": the ledger is a writer too, and guessing 0 here
    # silently produces a duplicate round (the same class as the corrupt counter).
    _die "cannot read ledger $IW_LEDGER to find the highest round; refusing to guess"
  fi
  case "${out:-}" in ''|*[!0-9]*) _die "unexpected ledger scan result \"$out\"; refusing to guess" ;; esac
  LEDGER_MAX=$out
}

_lock() { exec 9>>"$LOCK"; flock 9; }
_unlock() { flock -u 9; exec 9>&-; }

# Both verbs run the read inside the lock. `record` keeps holding it across the append: a number read
# outside the lock is stale by the time the message lands, and the ledger then reads 32 before 31.
_alloc() {
  _lock
  _read_counter
  local lmax
  _read_ledger_max; lmax=$LEDGER_MAX
  [ "$lmax" -gt "$CURRENT" ] && CURRENT=$lmax
  CURRENT=$((CURRENT + 1))
  printf '%s\n' "$CURRENT" > "$COUNTER"
  _unlock
}

_pin() {
  case "$1" in ''|*[!0-9]*) _die "reuse needs a non-negative integer, got \"$1\"" ;; esac
  _lock
  CURRENT=$1
  printf '%s\n' "$CURRENT" > "$COUNTER"
  # A pin is the caller stating which round this is, so `record` must honour it: it writes
  # `round.pinned`, and `round.closed` is set to N-1 so a successful pinned write leaves consistent
  # bookkeeping (a *second* record then allocates N+1 as usual).
  if [ "$CURRENT" -gt 0 ]; then
    printf '%s\n' "$((CURRENT - 1))" > "$CLOSED"
    printf '%s\n' "$CURRENT" > "$PINNED"
  else
    rm -f "$PINNED"
  fi
  _unlock
}

# A counter in a second location means two parallel numberings, and the ledger then reads as if history
# restarted. It cannot be detected from the output ("next" answers 1 as confidently as 6), so say it.
_warn_split() {
  [ -n "${IW_STATE_DIR:-}" ] && return 0     # an explicit directory is the caller's own decision
  [ -f "$COUNTER" ] && return 0               # this location is already the one in use
  local alt n
  for alt in "$HERMES_HOME/cron/state/hawkeye-ledger/round.current" \
             "$HERMES_HOME/cache/hawkeye/ledger/round.current"; do
    if [ -f "$alt" ]; then
      n=$(tr -d '[:space:]' < "$alt" 2>/dev/null)
      echo "ledger-round: no counter in $STATE_DIR; found one in $(dirname "$alt") (${n:-?})" \
           "-- set IW_STATE_DIR to use it" >&2
      return 0
    fi
  done
}

_say() { printf 'ledger-round: state=%s round=%s\n' "$STATE_DIR" "$CURRENT" >&2; }

_report() {
  "$HK" report --into "$IW_LEDGER" --file "$1" --title "$2"
}

_warn_split

case "${1:-}" in
  peek)
    _read_counter; _say; echo "$CURRENT" ;;
  next)
    _alloc; _say; echo "$CURRENT" ;;
  reuse)
    [ $# -eq 2 ] || usage
    _pin "$2"; _say; echo "$CURRENT" ;;
  detect)
    [ $# -eq 2 ] || usage
    raw=$2
    [ -f "$raw" ] || _die "no such file: $raw"
    _alloc
    rm -f "$PINNED"          # a fresh detection supersedes any back-fill pin
    kept="$STATE_DIR/round-$CURRENT.raw.json"
    cp "$raw" "$kept" || _die "could not keep the payload at $kept"
    _say
    printf '第 %d 轮：原文已存 %s（账本不写原文，只写结论）\n' "$CURRENT" "$kept"
    ;;
  record)
    [ $# -eq 3 ] || usage
    [ -n "${IW_LEDGER:-}" ] || _die "IW_LEDGER is not set; refusing to write an unaddressed entry"
    title=$2
    body=$3
    [ -f "$body" ] || _die "no such body: $body"
    _lock
    _read_counter
    closed=0
    [ -f "$CLOSED" ] && closed=$(tr -d '[:space:]' < "$CLOSED" 2>/dev/null)
    case "$closed" in ''|*[!0-9]*) closed=0 ;; esac
    pinned=0
    [ -f "$PINNED" ] && pinned=$(tr -d '[:space:]' < "$PINNED" 2>/dev/null)
    case "$pinned" in ''|*[!0-9]*) pinned=0 ;; esac
    _read_ledger_max; lmax=$LEDGER_MAX
    if [ "$pinned" -gt 0 ]; then
      # An explicit `reuse N` is the caller saying which round this is. Honouring it is the point of
      # the verb -- but it must not resurrect a number the ledger already holds, or the record becomes
      # the duplicate the allocator exists to prevent. Refusing keeps both promises.
      if [ "$lmax" -ge "$pinned" ]; then
        _unlock
        _die "第 $pinned 轮 already exists in the ledger (highest is $lmax); pin a number above it"
      fi
      CURRENT=$pinned
    elif [ "$CURRENT" -eq 0 ] || [ "$closed" -ge "$CURRENT" ] || [ "$lmax" -ge "$CURRENT" ]; then
      # Either nothing was allocated yet, this round already has its entry, or somebody else (a wake
      # turn, a human, another agent) already appended this number through `hk report --into`. A new
      # round is the honest answer in all three cases: one entry per round, never below the ledger.
      [ "$closed" -gt "$CURRENT" ] && CURRENT=$closed
      [ "$lmax" -gt "$CURRENT" ] && CURRENT=$lmax
      CURRENT=$((CURRENT + 1))
      printf '%s\n' "$CURRENT" > "$COUNTER"
    fi
    entry="$STATE_DIR/round-$CURRENT-record.md"
    {
      printf '## 第 %d 轮 · %s\n\n' "$CURRENT" "$title"
      cat "$body"
    } > "$entry"
    if ! _report "$entry" "第 $CURRENT 轮 · $title"; then
      _unlock
      # The number is already allocated, so keep the file: a retry is `hk report --into … --file <kept>`.
      echo "ledger-round: report failed; entry kept at $entry (round $CURRENT is allocated)" >&2
      exit 3
    fi
    _unlock
    printf '%s\n' "$CURRENT" > "$CLOSED"
    rm -f "$PINNED"          # a pin is consumed by the write it described
    rm -f "$entry"
    _say
    ;;
  ""|-h|--help|help)
    usage ;;
  *)
    _die "unknown verb: $1" ;;
esac
