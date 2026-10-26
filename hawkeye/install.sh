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
#   ./install.sh                          # render units, enable the timer, install the skill
#   ./install.sh --gate                   # ... and install the completion gate in EVERY profile
#   ./install.sh --gate --profiles default,dev-coder
#   ./install.sh --no-enable              # render only, print the enable command
#   ./install.sh --print-only             # show the rendered units, write nothing
#   ./install.sh --skip-skill             # units only
#   ./install.sh --help
#
# The completion gate loads per profile, so `--gate` alone covers every profile found
# under <HERMES_HOME>/profiles/*/ — that is the point: a single install in the default
# profile leaves every worker in a worker profile ungated.
#
# Overrides (env or flag):
#   HERMES_HOME        --hermes-home DIR   Hermes home (default: $HOME/.hermes)
#   HAWKEYE_V2_CONFIG  --config FILE       hawkeye.toml path (default:
#                                          $XDG_CONFIG_HOME/hawkeye/hawkeye.toml,
#                                          created from the example)
#   HAWKEYE_KERNEL_SRC --kernel-src DIR    Hermes source checkout, for board resolution
#                                          (default: unset -> the unit omits it)
#   HAWKEYE_PYTHON     --python BIN        Interpreter hk should run under (default: the
#                                          python3 on PATH). Set it when the system python
#                                          is older than 3.11.

set -euo pipefail

REPO_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
UNIT_SRC="$REPO_DIR/config/systemd"
DEFAULT_UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
# What each path flag would be without the flag: a flag only *redirects* the install when its
# value really differs from this (see the unit-dir block below).
DEFAULT_HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DEFAULT_CONFIG="${HAWKEYE_V2_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/hawkeye/hawkeye.toml}"
UNIT_DIR="$DEFAULT_UNIT_DIR"
UNIT_DIR_GIVEN=0
HOME_GIVEN=0
CONFIG_GIVEN=0
SKILL_GIVEN=0
HERMES_HOME="$DEFAULT_HERMES_HOME"
HAWKEYE_V2_CONFIG="${HAWKEYE_V2_CONFIG:-}"
HAWKEYE_KERNEL_SRC="${HAWKEYE_KERNEL_SRC:-}"
HAWKEYE_PYTHON="${HAWKEYE_PYTHON:-}"
SKILL_DEST=""
DO_ENABLE=1
DO_SKILL=1
DO_GATE=0
GATE_PROFILES=""          # empty with --gate means: every profile found
PRINT_ONLY=0

note() { printf '  %s\n' "$*"; }
step() { printf '\n== %s\n' "$*"; }
die()  { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

# "name|home" lines: the root home IS the default profile.
gate_profile_list() {
    printf 'default|%s\n' "$HERMES_HOME"
    if [ -d "$HERMES_HOME/profiles" ]; then
        for d in "$HERMES_HOME"/profiles/*/; do
            [ -f "${d}config.yaml" ] || continue
            printf '%s|%s\n' "$(basename "$d")" "${d%/}"
        done
    fi
}

# Copy the plugin, then enable it *per profile*: "enabled" is per-profile config, and
# replacing the files of an already-enabled plugin does not reload it, so the enable
# call is also what hot-loads the new copy into the running gateway for that profile.
install_gate_into() {
    g_name="$1"; g_home="$2"
    mkdir -p "$g_home/plugins"
    rm -rf "$g_home/plugins/hermes-hawkeye-gate.tmp"
    cp -a "$REPO_DIR/plugins/hermes-hawkeye-gate" "$g_home/plugins/hermes-hawkeye-gate.tmp"
    rm -rf "$g_home/plugins/hermes-hawkeye-gate"
    mv "$g_home/plugins/hermes-hawkeye-gate.tmp" "$g_home/plugins/hermes-hawkeye-gate"
    if [ "$g_name" = "default" ]; then
        HERMES_HOME="$g_home" hermes plugins enable hermes-hawkeye-gate \
            --no-allow-tool-override >/dev/null 2>&1
    else
        HERMES_HOME="$HERMES_HOME" hermes -p "$g_name" plugins enable hermes-hawkeye-gate \
            --no-allow-tool-override >/dev/null 2>&1
    fi
}

usage() {
    sed -n '2,/^set -euo pipefail$/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --hermes-home)  HERMES_HOME="$2"; HOME_GIVEN=1; shift 2 ;;
        --config)       HAWKEYE_V2_CONFIG="$2"; CONFIG_GIVEN=1; shift 2 ;;
        --kernel-src)   HAWKEYE_KERNEL_SRC="$2"; shift 2 ;;
        --python)       HAWKEYE_PYTHON="$2"; shift 2 ;;
        --unit-dir)     UNIT_DIR="$2"; UNIT_DIR_GIVEN=1; shift 2 ;;
        --skill-dir)    SKILL_DEST="$2"; SKILL_GIVEN=1; shift 2 ;;
        --gate)         DO_GATE=1; shift ;;
        --profiles)     GATE_PROFILES="$2"; DO_GATE=1; shift 2 ;;
        --all-profiles) GATE_PROFILES=""; DO_GATE=1; shift ;;
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

# A redirected install must never rewrite the units of the host it is testing on: they name
# HERMES_HOME and the hk path, so pointing them at a fixture silently disables the doctor on the
# real machine (measured: 65 minutes of 203/EXEC while the timer kept firing). Derive a unit dir
# under the redirect instead of refusing, and say so; --unit-dir still wins.
#
# "Redirected" means a flag *moved the path it names*, not "a flag was passed". The documented
# one-liner spells the default config out (it lives outside HERMES_HOME on this host), and
# treating that as a fixture install moved a real host's units out of ~/.config/systemd/user --
# the doctor then stops being started and nothing says why: #9's failure, one flag further in.
_trim_slash() { printf '%s' "${1%/}"; }
REDIRECTED=0
if [ "$HOME_GIVEN" = "1" ] && [ "$(_trim_slash "$HERMES_HOME")" != "$(_trim_slash "$DEFAULT_HERMES_HOME")" ]; then
    REDIRECTED=1
fi
if [ "$CONFIG_GIVEN" = "1" ] && [ "$(_trim_slash "${HAWKEYE_V2_CONFIG/#\~/$HOME}")" != "$(_trim_slash "$DEFAULT_CONFIG")" ]; then
    REDIRECTED=1
fi
if [ "$SKILL_GIVEN" = "1" ] && [ "$(_trim_slash "${SKILL_DEST/#\~/$HOME}")" != "$(_trim_slash "$HERMES_HOME/skills/software-development/hermes-hawkeye-v2")" ]; then
    REDIRECTED=1
fi
if [ "$UNIT_DIR_GIVEN" = "0" ] && [ "$REDIRECTED" = "1" ]; then
    UNIT_DIR="$HERMES_HOME/systemd/user"
    note "redirected install: units go to $UNIT_DIR (pass --unit-dir to choose)"
fi
# Derive the config default only now: --hermes-home may have just moved HERMES_HOME,
# and resolving it up top silently kept an inherited value instead (measured: with
# HERMES_HOME exported in the environment, --hermes-home /tmp/x still wrote to the
# inherited home).
HAWKEYE_V2_CONFIG="${HAWKEYE_V2_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/hawkeye/hawkeye.toml}"
HAWKEYE_V2_CONFIG="${HAWKEYE_V2_CONFIG/#\~/$HOME}"

# Old installs kept the config inside HERMES_HOME. Move it rather than starting over: a silent
# fall back to defaults looks exactly like "all my tuning was ignored".
LEGACY_CONFIG="$HERMES_HOME/hawkeye-v2/hawkeye.toml"
if [ ! -f "$HAWKEYE_V2_CONFIG" ] && [ -f "$LEGACY_CONFIG" ]; then
    mkdir -p "$(dirname "$HAWKEYE_V2_CONFIG")"
    mv "$LEGACY_CONFIG" "$HAWKEYE_V2_CONFIG"
    note "migrated config out of HERMES_HOME: $LEGACY_CONFIG -> $HAWKEYE_V2_CONFIG"
    rmdir "$HERMES_HOME/hawkeye-v2" 2>/dev/null || true
fi

# Run directories move too, but never out from under a live phase: a finished run is a record, an
# unfinished one is a process. Moving the second is how you orphan a job (#8) and leaving both is
# how the layout change made in-flight runs invisible to status and doctor (#9).
LEGACY_RUNS="$HERMES_HOME/hawkeye-v2/runs"
NEW_RUNS="$HERMES_HOME/cache/hawkeye/runs"
if [ -d "$LEGACY_RUNS" ]; then
    mkdir -p "$NEW_RUNS"
    moved=0
    left=0
    left_names=""
    for rd in "$LEGACY_RUNS"/*; do
        [ -d "$rd" ] || continue
        if grep -q '"finished": *true' "$rd/state.json" 2>/dev/null; then
            [ -e "$NEW_RUNS/$(basename "$rd")" ] && continue
            mv "$rd" "$NEW_RUNS/" && moved=$((moved + 1))
        else
            left=$((left + 1))
            left_names="$left_names $(basename "$rd")"
        fi
    done
    [ "$moved" -gt 0 ] && note "migrated $moved finished run(s) to $NEW_RUNS"
    if [ "$left" -gt 0 ]; then
        note "left $left live run(s) in $LEGACY_RUNS:$left_names"
        note "  they keep running there; hk status names them, and doctor will not restart them"
    fi
    rmdir "$LEGACY_RUNS" "$HERMES_HOME/hawkeye-v2" 2>/dev/null || true
fi
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
        } \
      | { if [ -n "$HAWKEYE_PYTHON" ]; then
              sed "/^\[Service\]/a Environment=HAWKEYE_PYTHON=$HAWKEYE_PYTHON"
          else
              cat
          fi
        }
}

step "1/5 preflight"
note "repo      : $REPO_DIR"
note "hermes    : $HERMES_HOME"
note "config    : $HAWKEYE_V2_CONFIG"
note "kernel src: ${HAWKEYE_KERNEL_SRC:-<unset>}"
note "units     : $UNIT_DIR"
# Whatever moved them, never let a non-default unit dir pass silently: a doctor installed where
# nobody looks is the failure this whole block exists to prevent.
if [ "$(_trim_slash "$UNIT_DIR")" != "$(_trim_slash "$DEFAULT_UNIT_DIR")" ]; then
    note "              (default is $DEFAULT_UNIT_DIR)"
fi
# Board resolution either goes through the Hermes kernel or through hawkeye's portable
# fallback. A path that looks wrong used to cost the kernel branch silently, so probe it
# instead of trusting the string: the import needs hermes_bootstrap (which wires the
# environment's site-packages), not just the right directory name.
if [ -z "$HAWKEYE_KERNEL_SRC" ]; then
    note "kernel    : not configured (board resolution uses the portable fallback)"
elif [ ! -d "$HAWKEYE_KERNEL_SRC/hermes_cli" ]; then
    note "kernel    : $HAWKEYE_KERNEL_SRC is not a Hermes checkout (no hermes_cli/)"
    note "            board resolution uses the portable fallback"
else
    probe=$("${HAWKEYE_PYTHON:-python3}" "$REPO_DIR/config/scripts/kernel-probe.py" \
            "$HAWKEYE_KERNEL_SRC" 2>&1 | head -1)
    case "$probe" in
        available*)   note "kernel    : $probe" ;;
        unavailable*) note "kernel    : $probe"
                      note "            board resolution uses the portable fallback" ;;
        *)            note "kernel    : probe failed ($probe)" ;;
    esac
fi

if [ "$PRINT_ONLY" = "1" ]; then
    step "rendered hawkeye-doctor.service"
    render_service
    step "hawkeye-doctor.timer (unchanged)"
    cat "$UNIT_SRC/hawkeye-doctor.timer"
    exit 0
fi

step "2/5 config file"
if [ -f "$HAWKEYE_V2_CONFIG" ]; then
    note "exists, left untouched: $HAWKEYE_V2_CONFIG"
else
    mkdir -p "$(dirname "$HAWKEYE_V2_CONFIG")"
    cp "$REPO_DIR/config/hawkeye.toml.example" "$HAWKEYE_V2_CONFIG"
    note "created from the example: $HAWKEYE_V2_CONFIG"
fi

step "3/5 systemd user units"
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

step "4/5 agent skill"
if [ "$DO_SKILL" = "0" ]; then
    note "skipped (--skip-skill)"
elif [ -d "$REPO_DIR/skill/hermes-hawkeye-v2" ]; then
    mkdir -p "$SKILL_DEST"
    # Copy the directory, not the entry file: SKILL.md points at references/*.md, and installing
    # only SKILL.md left those pointers dangling on every fresh host.
    cp -a "$REPO_DIR/skill/hermes-hawkeye-v2/." "$SKILL_DEST/"
    for ref in $(grep -o 'references/[A-Za-z0-9._-]*\.md' "$REPO_DIR/skill/hermes-hawkeye-v2/SKILL.md" | sort -u); do
        if [ ! -f "$SKILL_DEST/$ref" ]; then
            die "skill install incomplete: SKILL.md points at $ref but it was not installed"
        fi
    done
    note "installed to $SKILL_DEST/ (SKILL.md + references/)"
else
    note "repository has no skill/ directory, nothing to install"
fi

step "5/5 completion gate"
if [ "$DO_GATE" = "0" ]; then
    note "skipped (--gate installs it, --profiles/--all-profiles picks where)"
else
    [ -d "$REPO_DIR/plugins/hermes-hawkeye-gate" ] || die "missing plugins/hermes-hawkeye-gate"
    command -v hermes >/dev/null || die "hermes CLI not found; the gate is enabled per profile through it"
    gate_list=$(mktemp)
    if [ -n "$GATE_PROFILES" ]; then
        : > "$gate_list"
        IFS=',' read -r -a wanted_profiles <<< "$GATE_PROFILES"
        for p_name in "${wanted_profiles[@]}"; do
            p_name=$(printf '%s' "$p_name" | tr -d ' ')
            [ -n "$p_name" ] || continue
            if [ "$p_name" = "default" ]; then
                printf 'default|%s\n' "$HERMES_HOME" >> "$gate_list"
            elif [ -f "$HERMES_HOME/profiles/$p_name/config.yaml" ]; then
                printf '%s|%s\n' "$p_name" "$HERMES_HOME/profiles/$p_name" >> "$gate_list"
            else
                die "no such profile: $p_name (expected $HERMES_HOME/profiles/$p_name)"
            fi
        done
    else
        gate_profile_list > "$gate_list"
    fi
    while IFS='|' read -r p_name p_home; do
        [ -n "$p_name" ] || continue
        if install_gate_into "$p_name" "$p_home"; then
            printf '  %-14s installed + enabled\n' "$p_name"
        else
            printf '  %-14s files copied, ENABLE FAILED\n' "$p_name"
        fi
    done < "$gate_list"
    rm -f "$gate_list"
    note ""
    note "coverage (this is the point of the step: one profile is not enough):"
    HERMES_HOME="$HERMES_HOME" "$REPO_DIR/bin/hk" gate-check || \
        note "  ^ ungated profiles above run workers with no gate at all"
fi

cat <<EOF

Done. What is now in place:

  bin/hk                  runs in place, nothing installed
  doctor timer            every 2 minutes, restarts dead phases, files incidents
  skill                   the agent-side procedure (lease first, 1-2 line reports)
  completion gate         $([ "$DO_GATE" = "1" ] && echo "installed per profile (see the coverage table above)" || echo "not installed (--gate)")

Verify:
  $REPO_DIR/bin/hk status --all
  $REPO_DIR/bin/hk gate-check          # per-profile gate coverage; exit 1 when a profile is ungated
  systemctl --user start hawkeye-doctor.service   # exit code 1 means "an incident exists"

Remove:
  systemctl --user disable --now hawkeye-doctor.timer
  rm -f $UNIT_DIR/hawkeye-doctor.{service,timer} && systemctl --user daemon-reload
EOF
