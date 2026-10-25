#!/bin/sh
# Runtime compute-capability guard.
#
# Why this exists: the CUDA kernels inside the image are compiled for specific
# compute capabilities. Running an sm_86 image on an sm_75 or sm_61 card makes
# llama.cpp print exactly one line —
#   CUDA error: no kernel image is available for execution on the device
# — and says nothing about rebuilding. This check runs before the server starts
# and says the useful part out loud.
#
# Warn only, never block: the image may contain PTX that a newer driver can JIT,
# so a hard failure here would break setups that actually work.

ARCHS_FILE=/app/.cuda-arch
LLAMA=/app/llama-server

if [ -f "$ARCHS_FILE" ] && [ -z "${AUX_SKIP_ARCH_CHECK:-}" ]; then
    COMPILED=$(tr -d ' \n' < "$ARCHS_FILE" 2>/dev/null)

    # Built with all/native: nothing to compare against.
    case "$COMPILED" in
        *all*|*native*|"") ;;
        *)
            DEVICE_CC=""
            if command -v nvidia-smi >/dev/null 2>&1; then
                DEVICE_CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null \
                            | tr -d ' .' | tr '\n' ';' | sed 's/;$//')
            fi

            if [ -n "${DEVICE_CC:-}" ]; then
                MISSING=""
                for cc in $(echo "$DEVICE_CC" | tr ';' ' '); do
                    case ";$COMPILED;" in
                        *";$cc;"*) ;;
                        *) MISSING="${MISSING}${MISSING:+;}${cc}" ;;
                    esac
                done

                if [ -n "$MISSING" ]; then
                    echo "──────────────────────────────────────────────────────────" >&2
                    echo "compute capability mismatch: image built for sm_$COMPILED," >&2
                    echo "this GPU is sm$(echo $MISSING | tr ';' ',')" >&2
                    echo "" >&2
                    echo "as it is, inference will fail with:" >&2
                    echo "  CUDA error: no kernel image is available for execution on the device" >&2
                    echo "" >&2
                    echo "rebuild for this card (about 17 minutes per arch):" >&2
                    echo "  CUDA_ARCH=$MISSING ./config/buildCMD/build.sh" >&2
                    echo "" >&2
                    echo "or leave CUDA_ARCH empty in .env and let build.sh detect it" >&2
                    echo "skip this check: -e AUX_SKIP_ARCH_CHECK=1" >&2
                    echo "──────────────────────────────────────────────────────────" >&2
                fi
            fi
            ;;
    esac
fi

# stdbuf: llama-server fully buffers stdout when it is a pipe, so without this
# layer `docker logs` shows nothing during model load. See the Dockerfile.
exec /usr/bin/stdbuf -oL -eL "$LLAMA" "$@"
