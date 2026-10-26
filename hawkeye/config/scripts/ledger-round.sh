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
# One of the two documented forms is *required*: with both prefixes optional a body paragraph that
# starts at column 0 with "第 12 轮 · …" counted as an entry heading and inflated the maximum.
head = re.compile(r"^\s*(?:#{1,6}\s*|\[[^\]\n]{0,40}\]\s*)第\s*(\d+)\s*轮", re.M)
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

# `round.closed` is read by three verbs and is the only memory of the highest written round once a
# message is summarised, so it gets the same rule as the counter: absent or empty means "never
# written" (0), present-but-unparsable means refuse. The two files describing one fact cannot answer
# the same damage differently.
_read_closed() {
  CLOSED_VALUE=0
  [ -f "$CLOSED" ] || return 0
  local n
  n=$(tr -d '[:space:]' < "$CLOSED" 2>/dev/null)
  [ -z "$n" ] && return 0
  case "$n" in
    ''|*[!0-9]*) _die "round.closed is not a non-negative integer (\"$n\"); refusing to renumber" ;;
  esac
  CLOSED_VALUE=$n
}

_lock() { exec 9>>"$LOCK"; flock 9; }
_unlock() { flock -u 9; exec 9>&-; }

# Both verbs run the read inside the lock. `record` keeps holding it across the append: a number read
# outside the lock is stale by the time the message lands, and the ledger then reads 32 before 31.
_alloc() {
  _lock
  _read_counter
  local lmax closed
  _read_ledger_max; lmax=$LEDGER_MAX
  _read_closed; closed=$CLOSED_VALUE
  # `closed` counts: it is the only record that survives a summarised message losing its heading, and
  # an allocator that forgets it rewinds into rounds the ledger already has (reported: pinned write of
  # 20, ledger trimmed, `next` -> 7).
  [ "$closed" -gt "$CURRENT" ] && CURRENT=$closed
  [ "$lmax" -gt "$CURRENT" ] && CURRENT=$lmax
  CURRENT=$((CURRENT + 1))
  printf '%s\n' "$CURRENT" > "$COUNTER"
  _unlock
}

_pin() {
  case "$1" in ''|*[!0-9]*) _die "reuse needs a non-negative integer, got \"$1\"" ;; esac
  _lock
  CURRENT=$1
  # A pin touches nothing else: writing the counter here made an *abandoned* pin steer the next
  # `detect` (reuse 20, then detect -> 21 on a ledger holding 1..6). `peek` overlays the pin so the
  # caller still sees it, and `record` consumes it.
  if [ "$CURRENT" -gt 0 ]; then
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
    _read_counter
    if [ -f "$PINNED" ]; then
      p=$(tr -d '[:space:]' < "$PINNED" 2>/dev/null)
      case "$p" in ''|*[!0-9]*) p=0 ;; esac
      # A pin that `record` would refuse must not be reported as if it were writable: peek answers
      # "which round is next", and the answer is the pin only when the pin is usable.
      _read_closed; closed=$CLOSED_VALUE
      _read_ledger_max
      floor=$closed
      [ "$LEDGER_MAX" -gt "$floor" ] && floor=$LEDGER_MAX
      if [ "$p" -gt "$floor" ]; then
        CURRENT=$p
      else
        echo "ledger-round: pin $p is not above the last written round ($floor);"\
" record will refuse it unless it is re-pinned" >&2
        [ "$floor" -gt "$CURRENT" ] && CURRENT=$floor
      fi
    else
      # A consumed pin leaves `closed` ahead of the counter, so peeking the counter alone reported a
      # number rounds behind the ledger (14 in the reported case). Same fact, same three sources.
      _read_closed; closed=$CLOSED_VALUE
      _read_ledger_max
      [ "$closed" -gt "$CURRENT" ] && CURRENT=$closed
      [ "$LEDGER_MAX" -gt "$CURRENT" ] && CURRENT=$LEDGER_MAX
    fi
    _say; echo "$CURRENT" ;;
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
    _read_closed; closed=$CLOSED_VALUE
    pinned=0
    [ -f "$PINNED" ] && pinned=$(tr -d '[:space:]' < "$PINNED" 2>/dev/null)
    case "$pinned" in ''|*[!0-9]*) pinned=0 ;; esac
    _read_ledger_max; lmax=$LEDGER_MAX
    if [ "$pinned" -gt 0 ]; then
      # An explicit `reuse N` is the caller saying which round this is. Honouring it is the point of
      # the verb -- but it must not resurrect a number the ledger already holds, or the record becomes
      # the duplicate the allocator exists to prevent. Refusing keeps both promises.
      # The floor is max(closed, lmax), not lmax: on a compaction path the ledger's own maximum drops
      # while `closed` still remembers the highest round written, and guarding on lmax alone let a pin
      # re-write a round that already existed -- and pulled `closed` backwards with it.
      floor=$lmax
      [ "$closed" -gt "$floor" ] && floor=$closed
      if [ "$pinned" -le "$floor" ]; then
        _unlock
        _die "第 $pinned 轮 is not above the last written round ($floor);"\
" pin a higher number (the pin is retained -- re-pin to change it)"
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
    # "The last round that already has an entry" can only move forward; writing it unconditionally let a
    # lower pin pull it back and re-open the rewind.
    if [ "$CURRENT" -gt "$closed" ]; then
      printf '%s\n' "$CURRENT" > "$CLOSED"
    fi
    # The counter has to move with the write as well, or the next `peek` reports the pre-pin value.
    printf '%s\n' "$CURRENT" > "$COUNTER"
    rm -f "$PINNED"          # a pin is consumed by the write it described
    rm -f "$entry"
    _say
    ;;
  ""|-h|--help|help)
    usage ;;
  *)
    _die "unknown verb: $1" ;;
esac
