#!/usr/bin/env bash
# Build the auxiliary-llm image.
#
#   ./config/buildCMD/build.sh                       # auto-detect the local GPU (recommended)
#   ./config/buildCMD/build.sh --arch 75             # explicit (Turing=75, Pascal=61, V100=70)
#   ./config/buildCMD/build.sh --arch "75;80;86;89"  # multi-arch, one image for several GPU generations
#   ./config/buildCMD/build.sh --arch all            # every supported arch (slowest, largest)
#   ./config/buildCMD/build.sh --tag mytag
#   ./config/buildCMD/build.sh --no-cache            # force a rebuild after editing patches
#   ./config/buildCMD/build.sh --dry-run             # print the plan, build nothing
#   APT_MIRROR=https://mirrors.aliyun.com/ubuntu ./config/buildCMD/build.sh
#
# Compute capability must match the GPU you run on, otherwise inference fails with
#   CUDA error: no kernel image is available for execution on the device
# Rough cost: about 17 minutes per architecture on 16 cores. Every extra arch
# recompiles the CUDA kernels.
#
# Output is streamed live on purpose: a silent terminal during a 17-minute build
# is indistinguishable from a hang, and people kill it.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."

if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

IMAGE="${AUX_IMAGE:-auxiliary-llm:latest}"
TQ_REF="${TQ_REF:-tqp-v0.4.0}"
TQ_COMMIT="${TQ_COMMIT:-bcb85fc3ae85efa0f5f392c6c880dfc524923860}"
NOCACHE=""
CUDA_ARCH="${CUDA_ARCH:-}"
DRY_RUN=""

# Conservative default for machines with no visible GPU (CI, download-only hosts).
FALLBACK_ARCHS="75;80;86;89"
LOG="config/buildCMD/build.log"

while [ $# -gt 0 ]; do
    case "$1" in
        --arch)      CUDA_ARCH="$2"; shift 2 ;;
        --tag)       IMAGE="$2"; shift 2 ;;
        --no-cache)  NOCACHE="--no-cache"; shift ;;
        --dry-run)   DRY_RUN=1; shift ;;
        -h|--help)   sed -n '2,22p' "$0"; exit 0 ;;
        *)           echo "unknown option: $1"; exit 1 ;;
    esac
done

command -v docker >/dev/null || { echo "docker is required"; exit 1; }

# ── decide the target architectures ─────────────────────────────────────────
ARCH_SOURCE=""
DETECTED=""

if [ -z "$CUDA_ARCH" ]; then
    if command -v nvidia-smi >/dev/null 2>&1; then
        DETECTED=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null \
                   | tr -d ' .' | sort -u | tr '\n' ';' | sed 's/;$//')
    fi
    if [ -n "$DETECTED" ]; then
        CUDA_ARCH="$DETECTED"
        ARCH_SOURCE="auto-detected from the local GPU"
    else
        CUDA_ARCH="$FALLBACK_ARCHS"
        ARCH_SOURCE="no GPU detected, using the built-in multi-arch default"
    fi
else
    ARCH_SOURCE="explicitly requested"
fi

# Validate: a list of numbers the CUDA 12.8 toolchain really supports, or all/native.
KNOWN_ARCHS="50 52 53 60 61 62 70 72 75 80 86 87 89 90 100 101 120"
case "$CUDA_ARCH" in
    all|native) ;;
    *)
        IFS=';' read -ra ARCH_LIST <<< "$CUDA_ARCH"
        for a in "${ARCH_LIST[@]}"; do
            case "$a" in
                ''|*[!0-9]*) echo "invalid compute capability: '$a' (use numbers like 75 / 80 / 86, separate with ;)"; exit 1 ;;
            esac
            if ! echo " $KNOWN_ARCHS " | grep -q " $a "; then
                echo "CUDA 12.8 does not support sm_$a. Known values: $KNOWN_ARCHS"
                echo "(nvcc --list-gpu-arch prints what your toolchain supports)"
                exit 1
            fi
        done
        ;;
esac

ARCH_COUNT=$(echo "$CUDA_ARCH" | tr ';' '\n' | grep -c . || true)

echo "────────────────────────────────────────────"
echo " image     : $IMAGE"
echo " source    : $TQ_REF ($TQ_COMMIT)"
echo " arch      : $CUDA_ARCH  [$ARCH_SOURCE]"
echo " patches   : $(ls config/buildCMD/patches/*.patch 2>/dev/null | wc -l)"

if [ "$CUDA_ARCH" = "all" ]; then
    echo " estimate  : 60–120 minutes, notably larger image"
elif [ "$CUDA_ARCH" = "native" ]; then
    echo " estimate  : native needs the GPU visible at build time (buildx + --gpus);"
    echo "             the legacy builder will fail"
elif [ "$ARCH_COUNT" -eq 1 ]; then
    echo " estimate  : about 17 minutes on 16 cores"
    echo " note      : only sm_$CUDA_ARCH is compiled. It runs on that generation and"
    echo "             newer cards (via PTX JIT, slower at first start); older cards need"
    echo "             a rebuild or --arch \"$CUDA_ARCH;75;80\"."
else
    echo " estimate  : about 17 minutes x $ARCH_COUNT archs; covering: $(echo "$CUDA_ARCH" | tr ';' '\n' | sed 's/^/sm_/' | paste -sd' ' -)"
fi
echo "────────────────────────────────────────────"

if [ -n "$DRY_RUN" ]; then
    echo "(--dry-run: printed the plan only, nothing was built)"
    exit 0
fi

START=$(date +%s)
mkdir -p "$(dirname "$LOG")"

on_fail() {
    echo
    echo "build failed after $(( ($(date +%s) - START) / 60 ))min — full log: $LOG"
    echo "the tail of it is above; nothing was tagged as $IMAGE"
    exit 1
}
trap on_fail ERR

# Prefer buildx (prints a readable, timestamped stream with --progress=plain).
# Fall back to the legacy builder, which also streams but with coarser output.
if docker buildx version >/dev/null 2>&1; then
    BUILD=(docker buildx build --progress=plain)
else
    echo "note: buildx not available, using the legacy builder"
    BUILD=(docker build)
fi

echo "building — output is streamed below and mirrored to $LOG"
echo

"${BUILD[@]}" \
    -f config/buildCMD/Dockerfile \
    -t "$IMAGE" \
    ${NOCACHE} \
    --build-arg TQ_REF="$TQ_REF" \
    --build-arg TQ_COMMIT="$TQ_COMMIT" \
    --build-arg CUDA_ARCH="$CUDA_ARCH" \
    --build-arg APT_MIRROR="${APT_MIRROR:-}" \
    . 2>&1 | tee "$LOG"

trap - ERR

echo
echo "built in $(( ($(date +%s) - START) / 60 ))min"
docker images "$IMAGE" --format '   {{.Repository}}:{{.Tag}}  {{.Size}}  {{.ID}}'
echo "   arch baked into the image: $(docker inspect "$IMAGE" --format '{{index .Config.Labels "io.auxiliary-llm.cuda-arch"}}' 2>/dev/null)"
echo
echo "next: docker compose up -d && ./config/scripts/verify.sh"
