#!/usr/bin/env bash
# Периодический прогон: переобучение ранжирования + оценка качества по эталону.
#
# Вызывается launchd (com.tradesoft.kb-rank-eval.plist) ежедневно.
# 1. rank_train.py — переобучает веса по неявным сигналам (клики) и пишет
#    cache/rank_weights.json (активирует модель, только если накоплено >= 50
#    запросов и >= 200 пар И NDCG не деградирует).
# 2. evaluate.py --json — прогоняет эталон (eval_queries.json) и сохраняет
#    отчёт (logs/eval_report_*.json) для отслеживания качества.
#
# Использует venv scripts/.venv (в нём стоит pymorphy3 и весь стек поиска).
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KB_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PY="$KB_ROOT/scripts/.venv/bin/python"
LOGS="$KB_ROOT/logs"
STAMP="$(date '+%Y%m%d_%H%M%S')"
LOG_FILE="$LOGS/rank_eval.log"

mkdir -p "$LOGS"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$LOG_FILE"; }

if [ ! -x "$PY" ]; then
    log "ERROR: venv python не найден: $PY"
    exit 1
fi

log "=== rank_train ==="
if (cd "$KB_ROOT/scripts" && "$PY" rank_train.py); then
    log "rank_train: ok"
else
    log "rank_train: FAIL rc=$?"
fi

log "=== evaluate (hybrid) ==="
REPORT="$LOGS/eval_report_${STAMP}.json"
if (cd "$KB_ROOT/scripts" && "$PY" evaluate.py --mode hybrid --top 10 --json "$REPORT"); then
    log "evaluate: ok -> $REPORT"
else
    log "evaluate: FAIL rc=$?"
fi

log "=== done ==="
