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

_lock() { exec 9>>"$LOCK"; flock 9; }
_unlock() { flock -u 9; exec 9>&-; }

# Both verbs run the read inside the lock. `record` keeps holding it across the append: a number read
# outside the lock is stale by the time the message lands, and the ledger then reads 32 before 31.
_alloc() {
  _lock
  _read_counter
  CURRENT=$((CURRENT + 1))
  printf '%s\n' "$CURRENT" > "$COUNTER"
  _unlock
}

_pin() {
  case "$1" in ''|*[!0-9]*) _die "reuse needs a non-negative integer, got \"$1\"" ;; esac
  _lock
  CURRENT=$1
  printf '%s\n' "$CURRENT" > "$COUNTER"
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
    if [ "$CURRENT" -eq 0 ] || [ "$closed" -ge "$CURRENT" ]; then
      # Either nothing was allocated yet, or this round already has its entry (two records in a row
      # would otherwise both be written as "第 19 轮", making the ledger ambiguous). A new round is
      # the honest answer in both cases: one entry per round.
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
    rm -f "$entry"
    _say
    ;;
  ""|-h|--help|help)
    usage ;;
  *)
    _die "unknown verb: $1" ;;
esac
