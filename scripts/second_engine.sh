#!/usr/bin/env bash
# Stand up a SECOND real llama.cpp engine, materially different from the
# reference :8080 server, so ComputeConnect can demonstrate real heterogeneous
# placement (not simulation). See docs/STATUS.md "Abandonment re-evaluation".
#
#   :8080  reference engine  — Qwen3-30B-A3B MoE, Q4_K_M, 16k ctx  (managed elsewhere)
#   :8091  THIS engine       — Qwen3-4B dense,    Q4_K_M,  8k ctx  (faster, smaller window)
#
# Different model family (dense vs MoE), different size (4B vs 30B), different
# context window (8k vs 16k) — a real second node of a different shape on the
# same box. Read-only w.r.t. the :8080 unit; this starts an INDEPENDENT process.
#
# Usage:  scripts/second_engine.sh   [MODEL_GGUF] [ALIAS] [PORT] [CTX]
set -euo pipefail
DIR="${LLAMA_DIR:-$HOME/llama-cix}"
BIN="${LLAMA_BIN:-$DIR/bin/llama-server}"
MODEL="${1:-$DIR/models/Qwen3-4B-Q4_K_M.gguf}"
ALIAS="${2:-qwen3-4b}"
PORT="${3:-8091}"
CTX="${4:-8192}"
export LD_LIBRARY_PATH="$DIR/bin:${LD_LIBRARY_PATH:-}"
echo "starting second engine: $ALIAS on :$PORT (ctx $CTX) from $MODEL"
exec "$BIN" -m "$MODEL" --alias "$ALIAS" \
  --host 127.0.0.1 --port "$PORT" \
  -c "$CTX" -t 4 -tb 6 --jinja --reasoning off
