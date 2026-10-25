# syntax=docker/dockerfile:1
#
# auxiliary-llm — two-stage build
#   stage 1 (build)  : CUDA devel image, fetch pinned source, apply patches, cmake build
#   stage 2 (runtime): CUDA runtime image with only the binary and .so files (~half the size)
#
# Design notes
#   * Source is pinned by commit; tag/commit mismatch aborts the build, so a moved
#     tag can never silently change what you compiled.
#   * Every patch is verified by content after applying; a patch that did not land
#     fails the build instead of producing a green build with none of the fixes.
#   * The entrypoint wraps llama-server in stdbuf: its stdout is fully buffered when
#     piped, so without that you cannot see startup logs at all.

ARG UBUNTU_VERSION=24.04
ARG CUDA_VERSION=12.8.1

# ═══════════════════════ stage 1: compile ═══════════════════════
FROM nvidia/cuda:${CUDA_VERSION}-devel-ubuntu${UBUNTU_VERSION} AS build

ARG TQ_REPO=https://github.com/TheTom/llama-cpp-turboquant.git
ARG TQ_REF=tqp-v0.4.0
ARG TQ_COMMIT=bcb85fc3ae85efa0f5f392c6c880dfc524923860
# Target compute capability. Must match the GPU you will run on, otherwise
# inference fails with "CUDA error: no kernel image is available ...".
#
#   Turing (RTX20/T4)=75  Volta (V100)=70  Pascal (P40)=61  Pascal (P100)=60
#   Ampere (A100)=80  Ampere (RTX30/A3000)=86  Ada (RTX40/L40)=89
#   Hopper (H100)=90  Blackwell (RTX50/B200)=120
#   Multiple values with semicolons: 75;80;86;89
#   all = every arch the toolchain supports (slowest, largest image)
#
# build.sh detects this automatically and overrides the default below; the
# default only matters when you call `docker build` directly.
ARG CUDA_ARCH=75;80;86;89
ARG GCC_VERSION=14
# Ubuntu mirror (empty = official). Useful behind a slow link or a firewall:
#   APT_MIRROR=https://mirrors.aliyun.com/ubuntu ./config/buildCMD/build.sh
ARG APT_MIRROR=

RUN set -eux; \
    if [ -n "${APT_MIRROR}" ]; then \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources; do \
            [ -f "$f" ] || continue; \
            sed -i "s|http://archive.ubuntu.com/ubuntu|${APT_MIRROR}|g; \
                    s|http://security.ubuntu.com/ubuntu|${APT_MIRROR}|g" "$f"; \
        done; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
        gcc-${GCC_VERSION} g++-${GCC_VERSION} build-essential cmake \
        python3 python3-pip git patch libssl-dev libgomp1 curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV CC=gcc-${GCC_VERSION} \
    CXX=g++-${GCC_VERSION} \
    CUDAHOSTCXX=g++-${GCC_VERSION}

WORKDIR /src

# Fetch the single pinned commit only: no history, no tags, less time and size.
RUN set -eux; \
    git init -q .; \
    git remote add origin "${TQ_REPO}"; \
    git fetch -q --depth 1 origin "refs/tags/${TQ_REF}"; \
    git checkout -q FETCH_HEAD; \
    GOT="$(git rev-parse HEAD)"; \
    echo "expected ${TQ_COMMIT}"; echo "got      ${GOT}"; \
    [ "${GOT}" = "${TQ_COMMIT}" ] || { echo "FATAL: source commit does not match the pin"; exit 1; }

# Patches: any one failing aborts the build. A green build without the patches is
# worse than a red build, because the regression only shows up at runtime.
COPY config/buildCMD/patches/ /tmp/patches/
RUN set -eux; \
    for p in /tmp/patches/*.patch; do \
        echo "=== applying ${p}"; \
        git apply --verbose "${p}"; \
    done; \
    grep -q 'json::parse' tools/server/server-common.cpp || { echo "FATAL: json-object patch did not land"; exit 1; }; \
    echo "patch verified"

RUN echo "=== CUDA_ARCH = ${CUDA_ARCH}" && \
    cmake -B build \
        -DGGML_NATIVE=OFF \
        -DGGML_CUDA=ON \
        -DGGML_BACKEND_DL=ON \
        -DGGML_CPU_ALL_VARIANTS=ON \
        -DLLAMA_BUILD_TESTS=OFF \
        -DCMAKE_CUDA_ARCHITECTURES="${CUDA_ARCH}" \
        -DCMAKE_EXE_LINKER_FLAGS=-Wl,--allow-shlib-undefined \
        . \
    && cmake --build build --config Release -j"$(nproc)"

# Collect the binary plus all shared libraries: with BACKEND_DL the backends are
# separate .so files, and a missing one only fails at run time.
RUN mkdir -p /out && \
    cp -r build/bin /out/bin && \
    mkdir -p /out/lib && \
    find build -name "*.so*" -exec cp -P {} /out/lib/ \; && \
    chmod +x /out/bin/llama-server && \
    printf '%s' "${CUDA_ARCH}" > /out/.cuda-arch && \
    echo "built for: $(cat /out/.cuda-arch)"

# ═══════════════════════ stage 2: runtime ═══════════════════════
FROM nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu${UBUNTU_VERSION} AS runtime

ARG TQ_REF=tqp-v0.4.0
ARG APT_MIRROR=
ARG CUDA_ARCH=unknown

LABEL org.opencontainers.image.title="auxiliary-llm" \
      org.opencontainers.image.description="llama.cpp with TurboQuant KV and MTP: OpenAI-compatible chat, embeddings and reranking in one image" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="nvidia/cuda:${CUDA_VERSION}-runtime-ubuntu${UBUNTU_VERSION}" \
      org.opencontainers.image.version="${TQ_REF}" \
      io.auxiliary-llm.cuda-arch="${CUDA_ARCH}"

RUN set -eux; \
    if [ -n "${APT_MIRROR}" ]; then \
        for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.sources; do \
            [ -f "$f" ] || continue; \
            sed -i "s|http://archive.ubuntu.com/ubuntu|${APT_MIRROR}|g; \
                    s|http://security.ubuntu.com/ubuntu|${APT_MIRROR}|g" "$f"; \
        done; \
    fi; \
    apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 curl ca-certificates coreutils \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /out/lib/ /app/
COPY --from=build /out/bin/ /app/
COPY --from=build /out/.cuda-arch /app/.cuda-arch
COPY config/buildCMD/entrypoint.sh /app/docker-entrypoint.sh
RUN chmod +x /app/docker-entrypoint.sh /app/llama-server

WORKDIR /app
ENV LD_LIBRARY_PATH=/app

# The entrypoint compares the arch the image was built for against the local GPU
# and explains the fix, instead of letting llama.cpp emit one opaque CUDA error.
# It also wraps the server in stdbuf so startup logging is visible.
ENTRYPOINT ["/app/docker-entrypoint.sh"]
