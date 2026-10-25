#!/usr/bin/env bash
# MTP A/B: record decode performance for whatever configuration is running now.
#
#   ./config/scripts/mtp-ab.sh spec-on
#   # edit .env: LLM_SPEC_TYPE=none, then docker compose up -d llm
#   ./config/scripts/mtp-ab.sh spec-off
#   cat data/results/mtp-ab.csv
#
# Why this does not switch configurations by itself: restarting the container
# reloads the model (tens of seconds to minutes), and folding that into the same
# run makes the numbers easy to fool yourself with. Run each side separately.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/../.."
[ -f .env ] && { set -a; . ./.env; set +a; }

LABEL="${1:-run-$(date +%H%M%S)}"
LLM_PORT="${LLM_PORT:-8888}"
LLM_ALIAS="${LLM_ALIAS:-aux-llm}"
URL="http://127.0.0.1:$LLM_PORT/v1/chat/completions"
REPS="${REPS:-3}"
OUT_DIR=data/results
OUT="$OUT_DIR/mtp-ab.csv"

mkdir -p "$OUT_DIR"
[ -f "$OUT" ] || echo "label,timestamp,ctx,kv_v,spec_type,spec_n,prompt,decode_tok_s,draft_n,accept_rate" > "$OUT"

CTX="${LLM_CTX:-65536}"
KV_V="${LLM_KV_V:-turbo4}"
SPEC_TYPE="${LLM_SPEC_TYPE:-draft-mtp}"
SPEC_N="${LLM_SPEC_N_MAX:-2}"

curl -sf --max-time 5 "http://127.0.0.1:$LLM_PORT/health" >/dev/null || {
    echo "service not ready: http://127.0.0.1:$LLM_PORT/health"; exit 1; }

run_one() {
    local prompt="$1" max_tokens="$2" tag="$3"
    local csv="" out med n rate
    for i in $(seq 1 "$REPS"); do
        out=$(curl -s --max-time 300 "$URL" -H 'Content-Type: application/json' -d "$(python3 -c "
import json,sys
print(json.dumps({'model':'$LLM_ALIAS','messages':[{'role':'user','content':sys.argv[1]}],
 'max_tokens':$max_tokens,'temperature':0.6,'top_p':0.7,'top_k':20}))" "$prompt")" \
        | python3 -c '
import sys,json
d=json.load(sys.stdin); t=d.get("timings",{})
n=t.get("draft_n") or 0; a=t.get("draft_n_accepted") or 0
print(t.get("predicted_per_second",0), n, (a/n if n else 0))
' 2>/dev/null)
        [ -n "$out" ] && csv="$csv
$out"
    done
    med=$(printf '%s\n' "$csv" | grep -v '^$' | awk '{print $1}' | sort -n | awk '{a[NR]=$1} END{print (NR%2? a[int(NR/2)+1] : (a[NR/2]+a[NR/2+1])/2)}')
    n=$(printf '%s\n' "$csv" | grep -v '^$' | awk '{print $2}' | sort -n | tail -1)
    rate=$(printf '%s\n' "$csv" | grep -v '^$' | awk '{s+=$3; c++} END{if(c) printf "%.3f", s/c; else print 0}')
    echo "  [$tag] decode median ${med:-0} tok/s | draft_n=$n | accept $(python3 -c "print(f'{100*${rate:-0}:.0f}%')" 2>/dev/null)" >&2
    echo "$LABEL,$(date -Iseconds),$CTX,$KV_V,$SPEC_TYPE,$SPEC_N,$tag,${med:-0},$n,${rate:-0}" >> "$OUT"
    echo "${med:-0}"
}

echo "──────────────────────────────────────"
echo " config: ctx=$CTX kv_v=$KV_V spec=$SPEC_TYPE n_max=$SPEC_N"
echo " repeats: ${REPS} per scenario"
echo "──────────────────────────────────────"

SHORT="Explain vector search in about 150 words."
LONG=$(python3 -c 'print("Below is a conversation log. Extract the facts and list them one per line: " + "the user is discussing local inference deployment, covering VRAM, batching and context length. " * 200)')

echo "scenario 1/2: short prompt (~30 input tokens)"
S=$(run_one "$SHORT" 256 "short")
echo
echo "scenario 2/2: long prompt (~4K input tokens)"
L=$(run_one "$LONG" 256 "long")

echo
echo "══════════════════════════════════════"
python3 -c "
s=$S or 0; l=$L or 0
print(f'  short prompt decode: {s:.1f} tok/s')
print(f'  long prompt decode:  {l:.1f} tok/s')
"
echo "  details appended to: $OUT"
echo "══════════════════════════════════════"
echo
echo "next round: change LLM_SPEC_TYPE in .env (draft-mtp / none), then"
echo "            docker compose up -d llm && ./config/scripts/mtp-ab.sh <label>"
