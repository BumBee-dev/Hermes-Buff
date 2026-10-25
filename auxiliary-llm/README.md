# auxiliary-llm

A self-hosted, OpenAI-compatible inference stack: **chat + embeddings + reranking**
from a single llama.cpp build, in three containers that share one image.

It exists because agent/memory stacks need all three roles at once, and most
setups end up maintaining three separate stacks — vLLM for chat, a Python
sentence-transformers service for embeddings, another for reranking. This one
builds once, serves all three, and keeps the memory footprint small enough to
fit next to the agent on a single consumer GPU.

| What you get | Endpoint (default) | Role |
|---|---|---|
| Chat completions | `http://127.0.0.1:8888/v1` | MTP speculative decoding, `response_format=json_object` |
| Embeddings | `http://127.0.0.1:1888/v1` | bge-m3, 1024-dim, `--pooling cls` |
| Reranking | `http://127.0.0.1:1999/v1` | bge-reranker-v2-m3 cross-encoder |

All three speak the OpenAI wire format (`/v1/chat/completions`, `/v1/embeddings`,
`/v1/rerank`), so any OpenAI client or agent framework can talk to them.

## What it is for

- **Agent memory / RAG**: embed documents, rerank candidates, answer with a small
  local model — no data leaves the host.
- **A drop-in local backend** for tools that expect an OpenAI base URL.
- **Long-context work on a small GPU**: the KV cache is quantised (TurboQuant
  4-bit on the V side, q8_0 on K), so a 4B model fits 64K–128K of context in
  ~12 GB of VRAM.
- **JSON-only pipelines**: the image carries a fix for bare
  `response_format={"type":"json_object"}`, which upstream llama.cpp silently
  ignores (see [NOTICE](NOTICE) and the patch in `config/buildCMD/patches/`).

It is *not* a training stack, a multi-tenant gateway, or a high-throughput
serving engine. One GPU, one model per role, requests served serially per role
(`-np 1`), which is what makes speculative decoding worthwhile.

## Requirements

### Hardware (reference, not measured on your machine)

| Item | Minimum | Comfortable |
|---|---|---|
| GPU | NVIDIA, compute capability ≥ 7.5 (Turing or newer) | same |
| VRAM | **8 GB** for the LLM role alone | **12 GB** for all three roles at 64K context; **16 GB** at 128K |
| CPU | 8 cores | 16 cores (build time is CPU-bound: ~17 min per GPU arch) |
| RAM | 16 GB | 32 GB |
| Disk | 25 GB free | 40 GB (image + models + build cache) |

CPU-only inference works (`-ngl 0`) but is far too slow to be useful for an
agent loop. Apple/AMD GPUs are not covered by this build.

### Software

- Docker Engine 24+ with the compose plugin (or `docker-compose` 2.x)
- NVIDIA driver with CUDA 12.8 support
- [nvidia-container-toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html)
  configured as a Docker runtime

## Install

One command. It clones the source, builds the image, starts the containers,
runs the self-check and prints the endpoints:

```bash
curl -fsSL https://raw.githubusercontent.com/eSun-ws/Hermes-Buff/main/auxiliary-llm/install.sh | bash
```

Choose the install directory with **`AUX_INS_HOME`** (default `$HOME/Aux-llm`):

```bash
curl -fsSL .../install.sh | AUX_INS_HOME=/opt/Aux-llm bash
```

Reuse a model directory you already have, and skip rebuilding:

```bash
curl -fsSL .../install.sh | MODEL_CACHE=/data/models bash -s -- --no-build
```

| Variable | Default | Meaning |
|---|---|---|
| `AUX_INS_HOME` | `$HOME/Aux-llm` | Where the stack is installed |
| `AUX_INS_REPO` | this repository | Source to clone |
| `AUX_INS_REF` | `main` | Branch or tag to install |
| `MODEL_CACHE` | `$AUX_INS_HOME/models` | Directory holding the `.gguf` files |
| `CUDA_ARCH` | auto-detected | Target compute capability for the build |

Installer flags (pass after `bash -s --`): `--dir PATH`, `--ref TAG`,
`--no-build`, `--no-start`, `--dry-run`, `--help`.

The installer stops before building if a model is missing and prints the exact
download command for each one — see the table below.

### Manual install

```bash
git clone --depth 1 --filter=blob:none --sparse https://github.com/eSun-ws/Hermes-Buff.git
cd Hermes-Buff && git sparse-checkout set auxiliary-llm
cd auxiliary-llm

cp config/.env.example .env          # then edit if you want non-default ports/models
./config/buildCMD/build.sh           # ~17 min per GPU arch, output streamed live
docker compose up -d
./config/scripts/verify.sh           # 8 checks: health, aliases, chat, JSON, MTP, dim, rerank, arch
```

## Models

Model weights are **not** included in this repository or in the image. Download
these three files into `models/` (or whatever `MODELS_DIR` points at). URLs and
sizes were verified against the Hugging Face API; the file names match the
defaults in `config/.env.example`, so no renaming is needed.

| Role | File | Size | Download |
|---|---|---|---|
| LLM (with MTP head) | `qwen3.5-4b-q8_0.gguf` | 4.61 GB | https://huggingface.co/unsloth/Qwen3.5-4B-MTP-GGUF/resolve/main/Qwen3.5-4B-Q8_0.gguf |
| Embeddings | `bge-m3-q8_0.gguf` | 634 MB | https://huggingface.co/ggml-org/bge-m3-Q8_0-GGUF/resolve/main/bge-m3-q8_0.gguf |
| Reranking | `bge-reranker-v2-m3-q8_0.gguf` | 636 MB | https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF/resolve/main/bge-reranker-v2-m3-Q8_0.gguf |
| Reranking (**optional**, stronger but slower) | `qwen3-reranker-0.6b-q8_0.gguf` | 639 MB | https://huggingface.co/mradermacher/Qwen3-Reranker-0.6B-GGUF/resolve/main/Qwen3-Reranker-0.6B.Q8_0.gguf |

```bash
mkdir -p models
curl -L --fail -o models/qwen3.5-4b-q8_0.gguf \
  https://huggingface.co/unsloth/Qwen3.5-4B-MTP-GGUF/resolve/main/Qwen3.5-4B-Q8_0.gguf
curl -L --fail -o models/bge-m3-q8_0.gguf \
  https://huggingface.co/ggml-org/bge-m3-Q8_0-GGUF/resolve/main/bge-m3-q8_0.gguf
curl -L --fail -o models/bge-reranker-v2-m3-q8_0.gguf \
  https://huggingface.co/gpustack/bge-reranker-v2-m3-GGUF/resolve/main/bge-reranker-v2-m3-Q8_0.gguf
```

Notes:

- The LLM **must** be an MTP-capable build for speculative decoding to work.
  Any GGUF with an MTP head works; without one, set `LLM_SPEC_TYPE=none` or the
  server reports `draft_n=0` and you lose the speedup (not correctness).
- Larger models work if they fit: raise `LLM_CTX`/`LLM_N_PREDICT` down or the
  quantisation up as needed. The names above are defaults, not requirements.
- Model licences are yours to comply with: the Qwen weights carry their own
  licence, bge-m3 and bge-reranker are MIT from BAAI.

### Optional: Qwen3-Reranker-0.6B instead of bge-reranker-v2-m3

Both rerankers speak the same `/v1/rerank` API, so this is a one-line swap. The
Qwen model ranks better and is noticeably slower — on the author's own NFCorpus
run, nDCG@10 was **0.7001 for Qwen3-Reranker-0.6B** against **0.6340 for
bge-reranker-v2-m3**, at roughly three times the latency per query. Pick it when
recall quality matters more than the millisecond budget.

```bash
# 1. download (a third-party GGUF conversion of the official Apache-2.0 weights)
curl -L --fail -o models/qwen3-reranker-0.6b-q8_0.gguf \
  https://huggingface.co/mradermacher/Qwen3-Reranker-0.6B-GGUF/resolve/main/Qwen3-Reranker-0.6B.Q8_0.gguf

# 2. point the reranker at it (keep the alias so clients need no change)
sed -i 's|^RERANK_MODEL=.*|RERANK_MODEL=qwen3-reranker-0.6b-q8_0.gguf|' .env

# 3. recreate only that container
docker compose up -d reranker
./config/scripts/verify.sh
```

About the weights: Qwen publishes the model as **safetensors**, not GGUF
(https://huggingface.co/Qwen/Qwen3-Reranker-0.6B, Apache-2.0). The link above is
a community conversion; GGUF conversions of the same model differ by a few KB
depending on the converter and llama.cpp version, which is expected. If you would
rather not trust a third-party artifact, convert it yourself:

```bash
pip install -r https://raw.githubusercontent.com/ggml-org/llama.cpp/master/requirements/requirements-convert_hf_to_gguf.txt
python convert_hf_to_gguf.py /path/to/Qwen3-Reranker-0.6B --outtype q8_0 --outfile models/qwen3-reranker-0.6b-q8_0.gguf
```

`verify.sh` checks rerank ordering, not which reranker is loaded, so run it after
either swap.

## Configuration

Everything lives in `.env` at the project root; `config/.env.example` documents
each key. The ones people actually touch:

| Key | Default | Why you would change it |
|---|---|---|
| `AUX_IMAGE` | `auxiliary-llm:latest` | Tag for a locally built image |
| `CUDA_ARCH` | empty (auto) | Force a specific compute capability; empty = detect the local GPU |
| `LLM_PORT` / `EMBED_PORT` / `RERANK_PORT` | 8888 / 1888 / 1999 | Host ports |
| `MODELS_DIR` | `./models` | Point at an existing model directory |
| `*_MODEL` / `*_ALIAS` | see template | File name inside `MODELS_DIR` / the name clients call |
| `LLM_CTX` | 65536 | Context window; the main VRAM lever |
| `LLM_KV_V` | `turbo4` | 4-bit KV cache. Set `q8_0` to trade VRAM for a little accuracy |
| `LLM_SPEC_TYPE` | `draft-mtp` | `none` disables speculative decoding |
| `LLM_NGL` | 99 | GPU layers; lower it for CPU inference |

After any change: `docker compose up -d` — not `restart`, which does not re-read
`.env`.

## Using it

Chat:

```bash
curl -s http://127.0.0.1:8888/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "aux-llm",
  "messages": [{"role": "user", "content": "Explain vector search in one sentence."}],
  "max_tokens": 120
}'
```

JSON mode (the patched path):

```bash
curl -s http://127.0.0.1:8888/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "aux-llm",
  "messages": [{"role": "user", "content": "Describe today'\''s weather as JSON with fields city and condition"}],
  "response_format": {"type": "json_object"}
}'
```

Embeddings and reranking:

```bash
curl -s http://127.0.0.1:1888/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model":"aux-embedding","input":"vector search"}'

curl -s http://127.0.0.1:1999/v1/rerank -H 'Content-Type: application/json' -d '{
  "model": "aux-reranker",
  "query": "how to reduce KV cache memory",
  "documents": ["I had ramen for lunch", "quantise the KV cache to 4 bits", "it will rain"]
}'
```

Any OpenAI-compatible client works: set `base_url` to one of the three URLs and
the model name to the alias, with a dummy API key.

## Operations

```bash
docker compose ps                       # state of the three containers
docker compose logs -f --tail=50 llm     # startup is verbose and live (stdbuf is baked in)
docker compose stop                      # stop, keep containers
docker compose down                      # remove containers (image and models stay)
./config/scripts/verify.sh              # self-check; exit code 1 on failure
./config/scripts/mtp-ab.sh spec-on      # decode benchmark, appends to data/results/mtp-ab.csv
./config/buildCMD/build.sh --dry-run    # show what the build would do
```

Health endpoints: `GET /health` on each port. First start takes 1–3 minutes
(weight load + CUDA graph capture); the healthcheck has a 180 s grace period.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `CUDA error: no kernel image is available` | image built for a different compute capability | rebuild: `CUDA_ARCH=<yours> ./config/buildCMD/build.sh`, or leave `CUDA_ARCH` empty |
| `draft_n=0` in timings | no MTP head in the model, or the old flag name | use an MTP GGUF; `LLM_SPEC_TYPE=draft-mtp` (not `mtp`) |
| JSON mode returns prose | image built without the patch | rebuild with `./config/buildCMD/build.sh --no-cache`; the build log must contain `patch verified` |
| Embeddings look wrong, retrieval quality drops | wrong pooling | keep `--pooling cls` for bge-m3 |
| Container restarts with OOM | context or batching too large for VRAM | lower `LLM_CTX`, `LLM_BATCH`, `LLM_UBATCH`, or set `LLM_KV_V=q8_0` |
| `docker logs` empty for minutes | something removed `stdbuf` from the entrypoint | keep the entrypoint wrapper; llama-server buffers fully when piped |
| `could not select device driver "" with capabilities: [[gpu]]` | no nvidia runtime | install nvidia-container-toolkit, restart docker |

## Layout

```
auxiliary-llm/
├── install.sh                     one-shot installer (curl | bash)
├── docker-compose.yml             the only orchestration file
├── .env                           generated from config/.env.example (not committed)
├── config/
│   ├── .env.example               every tunable, documented
│   ├── buildCMD/
│   │   ├── Dockerfile             two-stage: CUDA devel build -> CUDA runtime
│   │   ├── build.sh               arch detection, validation, live-streamed build
│   │   ├── entrypoint.sh          compute-capability guard + stdbuf
│   │   └── patches/               local fix for bare json_object requests
│   └── scripts/
│       ├── verify.sh              end-to-end self-check
│       └── mtp-ab.sh              speculative-decoding A/B, writes CSV
└── models/                        weights (not committed)
```

## Licence

MIT, see [../LICENSE](../LICENSE). Third-party components and their licences are
listed in [NOTICE](NOTICE). TurboQuant and the MTP integration come from the
pinned upstream fork; this project claims no authorship over either.
