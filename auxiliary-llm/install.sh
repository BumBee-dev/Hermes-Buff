#!/usr/bin/env bash
# auxiliary-llm — one-shot installer
#
#   curl -fsSL https://raw.githubusercontent.com/eSun-ws/Hermes-Buff/main/auxiliary-llm/install.sh | bash
#
# Custom install directory (default: $HOME/Aux-llm):
#   curl -fsSL .../install.sh | AUX_INS_HOME=/opt/Aux-llm bash
#
# Skip stages or point at models you already have:
#   curl -fsSL .../install.sh | bash -s -- --no-build
#   curl -fsSL .../install.sh | MODEL_CACHE=/data/models bash
#
# What it does, in order: preflight -> fetch source -> configure -> check models
# -> build image -> start containers -> verify -> print endpoints. Every stage
# streams its output; nothing is buffered behind a spinner.
#
# Environment:
#   AUX_INS_HOME   install directory            (default: $HOME/Aux-llm)
#   AUX_INS_REPO   source repository            (default: this project's upstream)
#   AUX_INS_REF    branch/tag to install        (default: main)
#   MODEL_CACHE    directory holding the .ggufs (default: $AUX_INS_HOME/models)
#   CUDA_ARCH      override GPU arch detection  (passed through to build.sh)
set -euo pipefail

AUX_INS_HOME="${AUX_INS_HOME:-$HOME/Aux-llm}"
AUX_INS_REPO="${AUX_INS_REPO:-https://github.com/eSun-ws/Hermes-Buff.git}"
AUX_INS_REF="${AUX_INS_REF:-main}"
SUBDIR="auxiliary-llm"
MODEL_CACHE="${MODEL_CACHE:-}"

DO_BUILD=1
DO_START=1
DRY_RUN=0

LLM_FILE="qwen3.5-4b-q8_0.gguf"
EMBED_FILE="bge-m3-q8_0.gguf"
RERANK_FILE="bge-reranker-v2-m3-q8_0.gguf"

LLM_URL="https://huggingface.co/unsloth/Qwen3.5-4B-MTP-GGUF/resolve/main/Qwen3.5-4B-Q8_0.gguf"
EMBED_URL="https://huggingface.co/ggml-org/bge-m3-Q8_0-GGUF/resolve/main/bge-m3-q8_0.gguf"
RERANK_URL="https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF/resolve/main/bge-reranker-v2-m3-Q8_0.gguf"

while [ $# -gt 0 ]; do
    case "$1" in
        --dir)        AUX_INS_HOME="$2"; shift 2 ;;
        --ref)        AUX_INS_REF="$2"; shift 2 ;;
        --no-build)   DO_BUILD=0; shift ;;
        --no-start)   DO_START=0; shift ;;
        --dry-run)    DRY_RUN=1; shift ;;
        -h|--help)    sed -n '2,26p' "$0"; exit 0 ;;
        *)            echo "unknown option: $1 (try --help)"; exit 1 ;;
    esac
done

say()  { printf '\n== %s\n' "$*"; }
note() { printf '   %s\n' "$*"; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

# ── compose command (v2 plugin or the standalone binary) ────────────────────
detect_compose() {
    if docker compose version >/dev/null 2>&1; then
        COMPOSE=(docker compose)
    elif have docker-compose; then
        COMPOSE=(docker-compose)
    else
        die "neither 'docker compose' nor 'docker-compose' is available"
    fi
}

# ── 0. plan ─────────────────────────────────────────────────────────────────
say "auxiliary-llm installer"
note "install dir : $AUX_INS_HOME"
note "source      : $AUX_INS_REPO ($AUX_INS_REF)"
note "models      : ${MODEL_CACHE:-$AUX_INS_HOME/models}"

# ── 1. preflight ────────────────────────────────────────────────────────────
say "1/7 preflight"
have docker || die "docker is required: https://docs.docker.com/engine/install/"
note "docker  : $(docker --version 2>/dev/null | head -1)"
detect_compose
note "compose : $("${COMPOSE[@]}" version 2>/dev/null | head -1)"

if have nvidia-smi; then
    note "gpu     : $(nvidia-smi --query-gpu=name,compute_cap,memory.total --format=csv,noheader 2>/dev/null | head -1)"
    if ! docker info 2>/dev/null | grep -qi nvidia; then
        note "WARNING : the docker runtime does not advertise an NVIDIA driver yet."
        note "          install nvidia-container-toolkit and restart docker, or"
        note "          the containers will fail with 'could not select device driver'."
    fi
else
    note "WARNING : nvidia-smi not found — this stack expects an NVIDIA GPU."
fi

if have git; then FETCH=git; else FETCH=curl; note "git not found, falling back to a tarball download"; fi

if [ "$DRY_RUN" -eq 1 ]; then
    say "dry run: stopping here (nothing cloned, built or started)"
    exit 0
fi

# ── 2. fetch the source ─────────────────────────────────────────────────────
say "2/7 fetching source"
mkdir -p "$AUX_INS_HOME"
TMP=$(mktemp -d "${TMPDIR:-/tmp}/auxiliary-llm.XXXXXX")
trap 'rm -rf "$TMP"' EXIT

if [ "$FETCH" = git ]; then
    git clone --depth 1 --branch "$AUX_INS_REF" --filter=blob:none --sparse \
        "$AUX_INS_REPO" "$TMP/src" >/dev/null
    ( cd "$TMP/src" && git sparse-checkout set "$SUBDIR" >/dev/null )
    SRC="$TMP/src/$SUBDIR"
    COMMIT=$(cd "$TMP/src" && git rev-parse --short HEAD)
else
    have curl || die "curl is required"
    curl -fsSL "$AUX_INS_REPO/archive/refs/heads/$AUX_INS_REF.tar.gz" -o "$TMP/src.tgz" \
        || die "could not download the source archive"
    tar xzf "$TMP/src.tgz" -C "$TMP"
    SRC=$(find "$TMP" -maxdepth 1 -type d -name '*Hermes-Buff*' | head -1)/"$SUBDIR"
    [ -d "$SRC" ] || die "the archive does not contain $SUBDIR/"
    COMMIT="archive:$AUX_INS_REF"
fi

[ -f "$SRC/docker-compose.yml" ] || die "unexpected source layout: $SRC"

# Copy in place, keeping any .env and models directory the user already has.
( cd "$SRC" && tar cf - --exclude=.git . ) | ( cd "$AUX_INS_HOME" && tar xf - )
note "source   : $AUX_INS_HOME ($COMMIT)"

chmod +x "$AUX_INS_HOME"/config/buildCMD/*.sh "$AUX_INS_HOME"/config/scripts/*.sh

# ── 3. configuration ────────────────────────────────────────────────────────
say "3/7 configuration"
if [ -f "$AUX_INS_HOME/.env" ]; then
    note ".env exists, keeping it untouched"
else
    cp "$AUX_INS_HOME/config/.env.example" "$AUX_INS_HOME/.env"
    note "created .env from the template"
fi

if [ -n "$MODEL_CACHE" ]; then
    if grep -q '^MODELS_DIR=' "$AUX_INS_HOME/.env"; then
        sed -i.bak "s|^MODELS_DIR=.*|MODELS_DIR=$MODEL_CACHE|" "$AUX_INS_HOME/.env"
        rm -f "$AUX_INS_HOME/.env.bak"
    else
        echo "MODELS_DIR=$MODEL_CACHE" >> "$AUX_INS_HOME/.env"
    fi
    note "MODELS_DIR -> $MODEL_CACHE"
fi
note "arch     : ${CUDA_ARCH:-auto-detect at build time}"

# ── 4. models ───────────────────────────────────────────────────────────────
say "4/7 checking models"
( cd "$AUX_INS_HOME" && set -a && . ./.env && set +a
  MODELS_DIR="${MODELS_DIR:-./models}"
  echo "$MODELS_DIR" > .models-dir )

MODELS_ABS=$(cd "$AUX_INS_HOME" && read -r MODELS_DIR < .models-dir && cd "$MODELS_DIR" 2>/dev/null && pwd || true)
rm -f "$AUX_INS_HOME/.models-dir"
if [ -z "${MODELS_ABS:-}" ]; then
    MODELS_ABS="$AUX_INS_HOME/models"
    mkdir -p "$MODELS_ABS"
fi

missing=0
check_model() {
    local file="$1" url="$2"
    if [ -f "$MODELS_ABS/$file" ]; then
        note "found    : $file ($(du -h "$MODELS_ABS/$file" | cut -f1))"
    else
        note "missing  : $file"
        echo "             curl -L --fail -o \"$MODELS_ABS/$file\" \\"
        echo "                  \"$url\""
        missing=1
    fi
}
echo
check_model "$LLM_FILE" "$LLM_URL"
check_model "$EMBED_FILE" "$EMBED_URL"
check_model "$RERANK_FILE" "$RERANK_URL"

if [ "$missing" -ne 0 ]; then
    echo
    die "download the models above into $MODELS_ABS, then re-run this installer
       (or point MODEL_CACHE at a directory that already has them)"
fi

# ── 5. build ────────────────────────────────────────────────────────────────
if [ "$DO_BUILD" -eq 1 ]; then
    say "5/7 building the image (streamed; expect ~17 minutes per GPU arch)"
    ( cd "$AUX_INS_HOME" && ./config/buildCMD/build.sh )
else
    say "5/7 build skipped (--no-build); the image must already exist"
fi

# ── 6. start ────────────────────────────────────────────────────────────────
if [ "$DO_START" -eq 1 ]; then
    say "6/7 starting containers"
    ( cd "$AUX_INS_HOME" && "${COMPOSE[@]}" up -d )
else
    say "6/7 start skipped (--no-start)"
    exit 0
fi

# ── 7. verify ───────────────────────────────────────────────────────────────
say "7/7 verifying (model load can take a few minutes)"
set +e
( cd "$AUX_INS_HOME" && ./config/scripts/verify.sh )
RC=$?
set -e

( cd "$AUX_INS_HOME" && set -a && . ./.env && set +a
  echo
  echo "════════════════════════════════════════════"
  echo " install dir : $AUX_INS_HOME"
  echo " chat        : http://127.0.0.1:${LLM_PORT:-8888}/v1   model ${LLM_ALIAS:-aux-llm}"
  echo " embeddings  : http://127.0.0.1:${EMBED_PORT:-1888}/v1   model ${EMBED_ALIAS:-aux-embedding}"
  echo " rerank      : http://127.0.0.1:${RERANK_PORT:-1999}/v1   model ${RERANK_ALIAS:-aux-reranker}"
  echo " logs        : cd $AUX_INS_HOME && ${COMPOSE[*]} logs -f llm"
  echo " stop        : cd $AUX_INS_HOME && ${COMPOSE[*]} down"
  echo "════════════════════════════════════════════" )

if [ "$RC" -eq 0 ]; then
    echo "all checks passed"
else
    echo "install finished but verification reported failures (see above);"
    echo "logs: cd $AUX_INS_HOME && docker compose logs --tail=100 llm"
    exit "$RC"
fi
