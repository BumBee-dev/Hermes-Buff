#!/usr/bin/env bash
# hawkeye-v2 installer for the optional pieces.
#
# The program itself needs no installation: bin/hk runs in place from this
# directory. What does need setting up is everything around it, and doing that by
# hand means editing four paths inside a systemd unit before the timer will start.
# This script renders those units from the templates in config/systemd/ with the
# values this host actually has, and can drop the skill into the Hermes skills
# directory.
#
# Usage:
#   ./install.sh                          # render units, enable the timer
#   ./install.sh --no-enable              # render only, print the enable command
#   ./install.sh --print-only             # show the rendered units, write nothing
#   ./install.sh --skip-skill             # units only
#   ./install.sh --help
#
# Overrides (env or flag):
#   HERMES_HOME        --hermes-home DIR   Hermes home (default: $HOME/.hermes)
#   HAWKEYE_V2_CONFIG  --config FILE       hawkeye.toml path (default:
#                                          $HERMES_HOME/hawkeye-v2/hawkeye.toml,
#                                          created from the example)
#   HAWKEYE_KERNEL_SRC --kernel-src DIR    Hermes source checkout, for board resolution
#                                          (default: unset -> the unit omits it)

set -euo pipefail

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
UNIT_SRC="$REPO_DIR/config/systemd"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
HAWKEYE_V2_CONFIG="${HAWKEYE_V2_CONFIG:-}"
HAWKEYE_KERNEL_SRC="${HAWKEYE_KERNEL_SRC:-}"
SKILL_DEST=""
DO_ENABLE=1
DO_SKILL=1
PRINT_ONLY=0

note() { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

usage() {
    sed -n '2,26p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --hermes-home)  HERMES_HOME="$2"; shift 2 ;;
        --config)       HAWKEYE_V2_CONFIG="$2"; shift 2 ;;
        --kernel-src)   HAWKEYE_KERNEL_SRC="$2"; shift 2 ;;
        --unit-dir)     UNIT_DIR="$2"; shift 2 ;;
        --skill-dir)    SKILL_DEST="$2"; shift 2 ;;
        --no-enable)    DO_ENABLE=0; shift ;;
        --skip-skill)   DO_SKILL=0; shift ;;
        --print-only)   PRINT_ONLY=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              die "unknown argument: $1 (try --help)" ;;
    esac
done

[ -f "$UNIT_SRC/hawkeye-doctor.service" ] || die "missing $UNIT_SRC/hawkeye-doctor.service"
[ -x "$REPO_DIR/bin/hk" ] || die "$REPO_DIR/bin/hk is not executable"
command -v python3 >/dev/null || die "python3 not found (hawkeye needs >= 3.11)"
python3 - <<'PY' || die "python3 is older than 3.11 (hawkeye reads TOML with tomllib)"
import sys
sys.exit(0 if sys.version_info >= (3, 11) else 1)
PY

HERMES_HOME="${HERMES_HOME/#\~/$HOME}"
# Derive the config default only now: --hermes-home may have just moved HERMES_HOME,
# and resolving it up top silently kept an inherited value instead (measured: with
# HERMES_HOME exported in the environment, --hermes-home /tmp/x still wrote to the
# inherited home).
HAWKEYE_V2_CONFIG="${HAWKEYE_V2_CONFIG:-$HERMES_HOME/hawkeye-v2/hawkeye.toml}"
HAWKEYE_V2_CONFIG="${HAWKEYE_V2_CONFIG/#\~/$HOME}"
SKILL_DEST="${SKILL_DEST:-$HERMES_HOME/skills/software-development/hermes-hawkeye-v2}"

render_service() {
    # Only the four lines that differ per host are rewritten; everything else in
    # the template (schedule, SuccessExitStatus, timeout) stays authoritative.
    sed -e "s|^Environment=HERMES_HOME=.*|Environment=HERMES_HOME=$HERMES_HOME|" \
        -e "s|^Environment=HAWKEYE_V2_CONFIG=.*|Environment=HAWKEYE_V2_CONFIG=$HAWKEYE_V2_CONFIG|" \
        -e "s|^Environment=HAWKEYE_KERNEL_SRC=.*|# Environment=HAWKEYE_KERNEL_SRC is not set (board resolution falls back)|" \
        -e "s|^ExecStart=.* doctor --fix|ExecStart=$REPO_DIR/bin/hk doctor --fix|" \
        "$UNIT_SRC/hawkeye-doctor.service" \
      | { if [ -n "$HAWKEYE_KERNEL_SRC" ]; then
              sed "s|^\# Environment=HAWKEYE_KERNEL_SRC is not set.*|Environment=HAWKEYE_KERNEL_SRC=$HAWKEYE_KERNEL_SRC|"
          else
              cat
          fi
        }
}

step "1/4 preflight"
note "repo      : $REPO_DIR"
note "hermes    : $HERMES_HOME"
note "config    : $HAWKEYE_V2_CONFIG"
note "kernel src: ${HAWKEYE_KERNEL_SRC:-<unset>}"
note "units     : $UNIT_DIR"

if [ "$PRINT_ONLY" = "1" ]; then
    step "rendered hawkeye-doctor.service"
    render_service
    step "hawkeye-doctor.timer (unchanged)"
    cat "$UNIT_SRC/hawkeye-doctor.timer"
    exit 0
fi

step "2/4 config file"
if [ -f "$HAWKEYE_V2_CONFIG" ]; then
    note "exists, left untouched: $HAWKEYE_V2_CONFIG"
else
    mkdir -p "$(dirname "$HAWKEYE_V2_CONFIG")"
    cp "$REPO_DIR/config/hawkeye.toml.example" "$HAWKEYE_V2_CONFIG"
    note "created from the example: $HAWKEYE_V2_CONFIG"
fi

step "3/4 systemd user units"
mkdir -p "$UNIT_DIR"
render_service > "$UNIT_DIR/hawkeye-doctor.service"
cp "$UNIT_SRC/hawkeye-doctor.timer" "$UNIT_DIR/hawkeye-doctor.timer"
note "wrote $UNIT_DIR/hawkeye-doctor.service"
note "wrote $UNIT_DIR/hawkeye-doctor.timer"
if command -v systemctl >/dev/null; then
    systemctl --user daemon-reload && note "daemon-reload done"
    if [ "$DO_ENABLE" = "1" ]; then
        systemctl --user enable --now hawkeye-doctor.timer >/dev/null
        note "timer enabled and started"
        systemctl --user list-timers hawkeye-doctor.timer --no-pager 2>/dev/null | sed -n '2p' \
            | sed 's/^/  NEXT: /' || true
    else
        note "left disabled; enable later with: systemctl --user enable --now hawkeye-doctor.timer"
    fi
else
    note "systemctl not found: units written but not enabled"
fi

step "4/4 agent skill"
if [ "$DO_SKILL" = "0" ]; then
    note "skipped (--skip-skill)"
elif [ -f "$REPO_DIR/skill/hermes-hawkeye-v2/SKILL.md" ]; then
    mkdir -p "$SKILL_DEST"
    cp "$REPO_DIR/skill/hermes-hawkeye-v2/SKILL.md" "$SKILL_DEST/SKILL.md"
    note "installed to $SKILL_DEST/SKILL.md"
else
    note "repository has no skill/ directory, nothing to install"
fi

cat <<EOF

Done. What is now in place:

  bin/hk                  runs in place, nothing installed
  doctor timer            every 2 minutes, restarts dead phases, files incidents
  skill                   the agent-side procedure (lease first, 1-2 line reports)

Verify:
  $REPO_DIR/bin/hk status --all
  systemctl --user start hawkeye-doctor.service   # exit code 1 means "an incident exists"

Remove:
  systemctl --user disable --now hawkeye-doctor.timer
  rm -f $UNIT_DIR/hawkeye-doctor.{service,timer} && systemctl --user daemon-reload
EOF
