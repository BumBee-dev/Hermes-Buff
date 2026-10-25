#!/usr/bin/env bash
# End-to-end self-check: three services, the JSON patch, MTP, embedding width, rerank order.
#
#   ./config/scripts/verify.sh                  # uses ports/aliases from .env
#   LLM_PORT=8888 ./config/scripts/verify.sh
#   ./config/scripts/verify.sh --skip-wait      # services are known to be up
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
[ -f .env ] && { set -a; . ./.env; set +a; }

LLM_PORT="${LLM_PORT:-8888}"
EMBED_PORT="${EMBED_PORT:-1888}"
RERANK_PORT="${RERANK_PORT:-1999}"
LLM_ALIAS="${LLM_ALIAS:-aux-llm}"
EMBED_ALIAS="${EMBED_ALIAS:-aux-embedding}"
RERANK_ALIAS="${RERANK_ALIAS:-aux-reranker}"

SKIP_WAIT=""
[ "${1:-}" = "--skip-wait" ] && SKIP_WAIT=1

PASS=0; FAIL=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; [ -n "${2:-}" ] && echo "        -> $2"; FAIL=$((FAIL+1)); }
head_() { echo; echo "-- $1 --"; }

need() { command -v "$1" >/dev/null || { echo "missing dependency: $1"; exit 1; }; }
need curl; need python3

# ── 0. wait for model load ───────────────────────────────────────────────────
wait_up() {
    local name="$1" url="$2" i=0
    while [ $i -lt 60 ]; do
        if curl -sf --max-time 3 "$url" >/dev/null 2>&1; then
            ok "$name is up"; return 0
        fi
        sleep 5; i=$((i+1))
    done
    bad "$name did not come up within 300s" "$url"
    return 1
}

if [ -z "$SKIP_WAIT" ]; then
    head_ "waiting for services (model load can take minutes, timeout 300s)"
    wait_up "LLM       :$LLM_PORT"    "http://127.0.0.1:$LLM_PORT/health"
    wait_up "Embedding :$EMBED_PORT"  "http://127.0.0.1:$EMBED_PORT/health"
    wait_up "Reranker  :$RERANK_PORT" "http://127.0.0.1:$RERANK_PORT/health"
fi

# ── 1. aliases ──────────────────────────────────────────────────────────────
head_ "model aliases (clients call them by name, they must match)"
for pair in "$LLM_PORT:$LLM_ALIAS" "$EMBED_PORT:$EMBED_ALIAS" "$RERANK_PORT:$RERANK_ALIAS"; do
    port="${pair%%:*}"; alias_="${pair##*:}"
    got=$(curl -s --max-time 10 "http://127.0.0.1:$port/v1/models" \
          | python3 -c 'import sys,json;print(",".join(m["id"] for m in json.load(sys.stdin).get("data",[])))' 2>/dev/null)
    case ",$got," in
        *",$alias_,"*) ok ":$port serves $alias_" ;;
        *)             bad ":$port is missing alias $alias_" "got: ${got:-<no response>}" ;;
    esac
done

# ── 2. basic chat ───────────────────────────────────────────────────────────
head_ "chat completion"
resp=$(curl -s --max-time 180 "http://127.0.0.1:$LLM_PORT/v1/chat/completions" \
  -H 'Content-Type: application/json' -d "{
    \"model\": \"$LLM_ALIAS\",
    \"messages\": [{\"role\":\"user\",\"content\":\"Explain vector search in one sentence.\"}],
    \"max_tokens\": 120, \"temperature\": 0
}")
content=$(printf '%s' "$resp" | python3 -c 'import sys,json;print(json.load(sys.stdin)["choices"][0]["message"]["content"])' 2>/dev/null)
if [ -n "$content" ]; then ok "returned ${#content} characters"; else bad "no answer" "$(printf '%s' "$resp" | head -c 200)"; fi

# ── 3. JSON mode (is the patch actually in the image?) ──────────────────────
head_ "JSON mode (response_format=json_object must yield parseable JSON)"
json_ok=0
for i in 1 2 3; do
    body=$(curl -s --max-time 180 "http://127.0.0.1:$LLM_PORT/v1/chat/completions" \
      -H 'Content-Type: application/json' -d "{
        \"model\": \"$LLM_ALIAS\",
        \"messages\": [{\"role\":\"user\",\"content\":\"Describe today's weather as JSON with fields city and condition\"}],
        \"response_format\": {\"type\": \"json_object\"},
        \"max_tokens\": 300, \"temperature\": 0
      }")
    if printf '%s' "$body" | python3 -c '
import sys,json
c=json.load(sys.stdin)["choices"][0]["message"]["content"]
json.loads(c)
' 2>/dev/null; then
        json_ok=$((json_ok+1))
    fi
done
if [ "$json_ok" -eq 3 ]; then ok "3/3 responses were valid JSON"
elif [ "$json_ok" -gt 0 ]; then bad "only $json_ok/3 valid" "the json-object patch may not have been applied"
else bad "0/3 valid — the patch is not in this image" "rebuild with ./config/buildCMD/build.sh --no-cache"; fi

# ── 4. MTP speculative decoding ─────────────────────────────────────────────
head_ "MTP speculative decoding (draft_n > 0 means it is really on)"
mtp=$(curl -s --max-time 180 "http://127.0.0.1:$LLM_PORT/v1/chat/completions" \
  -H 'Content-Type: application/json' -d "{
    \"model\": \"$LLM_ALIAS\",
    \"messages\": [{\"role\":\"user\",\"content\":\"Count from 1 to 40, comma separated.\"}],
    \"max_tokens\": 300, \"temperature\": 0
  }" | python3 -c '
import sys,json
t=json.load(sys.stdin).get("timings",{})
n=t.get("draft_n") or 0; a=t.get("draft_n_accepted") or 0
print("%s %s %.1f" % (n, a, t.get("predicted_per_second",0)))
' 2>/dev/null)
draft_n=$(echo "$mtp" | awk '{print $1}'); acc=$(echo "$mtp" | awk '{print $2}'); tps=$(echo "$mtp" | awk '{print $3}')
if [ "${draft_n:-0}" -gt 0 ] 2>/dev/null; then
    rate=$(python3 -c "print(f'{100*$acc/$draft_n:.0f}%')" 2>/dev/null)
    ok "drafted $draft_n, accepted $acc ($rate), decode ${tps} tok/s"
else
    bad "draft_n=0 — MTP is not active" "model has no MTP head, or LLM_SPEC_TYPE uses the old name 'mtp' (v0.4.0 wants 'draft-mtp')"
fi

# ── 5. embedding width ──────────────────────────────────────────────────────
head_ "embeddings (bge-m3 must be 1024-dimensional)"
dim=$(curl -s --max-time 60 "http://127.0.0.1:$EMBED_PORT/v1/embeddings" \
  -H 'Content-Type: application/json' -d "{\"model\":\"$EMBED_ALIAS\",\"input\":\"vector search smoke test\"}" \
  | python3 -c 'import sys,json;print(len(json.load(sys.stdin)["data"][0]["embedding"]))' 2>/dev/null)
if [ "${dim:-0}" = "1024" ]; then ok "dimension 1024"
elif [ -n "${dim:-}" ]; then bad "dimension is $dim, not 1024" "the client's expected dimension must match"
else bad "no embedding response"; fi

# ── 6. rerank ordering ──────────────────────────────────────────────────────
head_ "reranker (the relevant document must come first)"
rr=$(curl -s --max-time 90 "http://127.0.0.1:$RERANK_PORT/v1/rerank" \
  -H 'Content-Type: application/json' -d "{
    \"model\": \"$RERANK_ALIAS\",
    \"query\": \"how to reduce KV cache memory usage\",
    \"documents\": [\"I had ramen for lunch\", \"TurboQuant rotates the KV cache with a WHT and quantises it to 4 bits\", \"it will rain tomorrow\"]
  }")
if [ -z "$rr" ]; then
    rr=$(curl -s --max-time 90 "http://127.0.0.1:$RERANK_PORT/rerank" \
      -H 'Content-Type: application/json' -d "{
        \"model\": \"$RERANK_ALIAS\",
        \"query\": \"how to reduce KV cache memory usage\",
        \"documents\": [\"I had ramen for lunch\", \"TurboQuant rotates the KV cache with a WHT and quantises it to 4 bits\", \"it will rain tomorrow\"]
      }")
fi
top=$(printf '%s' "$rr" | python3 -c '
import sys,json
rs=sorted(json.load(sys.stdin)["results"], key=lambda r: r.get("relevance_score",0), reverse=True)
print(rs[0]["index"], "%.3f" % rs[0].get("relevance_score",0))
' 2>/dev/null)
if [ "${top%% *}" = "1" ]; then ok "relevant document ranked first (score $(echo $top|awk '{print $2}'))"
elif [ -n "$top" ]; then bad "unexpected ordering, first is index ${top%% *}" "check that the reranker weights are loaded, not the embedding ones"
else bad "no rerank response"; fi

# ── 7. arch guard ───────────────────────────────────────────────────────────
head_ "image compute capability vs local GPU"
img_arch=$(docker inspect "${AUX_IMAGE:-auxiliary-llm:latest}" \
             --format '{{index .Config.Labels "io.auxiliary-llm.cuda-arch"}}' 2>/dev/null)
dev_cc=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | tr -d ' .' | sort -u | tr '\n' ' ')
if [ -n "$img_arch" ] && [ -n "$dev_cc" ]; then
    echo "  image: sm_$img_arch   host: $(echo $dev_cc | tr ' ' '\n' | sed 's/^/sm_/' | paste -sd' ' -)"
    if [ "$img_arch" = "all" ] || [ "$img_arch" = "native" ]; then
        ok "image is built for all archs"
    else
        miss=""
        for cc in $dev_cc; do
            case ";$img_arch;" in
                *";$cc;"*) ;;
                *) miss="$miss $cc" ;;
            esac
        done
        if [ -n "$miss" ]; then
            bad "image has no kernels for sm$(echo $miss | tr ' ' ',')" \
                "rebuild: CUDA_ARCH=$(echo $miss | sed 's/^ //' | tr ' ' ';') ./config/buildCMD/build.sh"
        else
            ok "architectures match"
        fi
    fi
else
    echo "  (skipped: no image label or no nvidia-smi)"
fi

# ── summary ─────────────────────────────────────────────────────────────────
echo
echo "════════════════════════════════════"
echo "  $PASS passed, $FAIL failed"
echo "════════════════════════════════════"
[ "$FAIL" -eq 0 ] || exit 1
