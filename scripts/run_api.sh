#!/bin/bash
# Unified Search API — запуск/остановка/статус
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
KB_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PORT="${PORT:-8055}"
PID_FILE="$KB_ROOT/logs/api_server.pid"
LOG_FILE="$KB_ROOT/logs/api_server.log"
PLIST="com.tradesoft.cross-search-api"

mkdir -p "$KB_ROOT/logs"

usage() {
    echo "Использование: $0 {start|stop|restart|status}"
    exit 1
}

start() {
    if launchctl list | grep -q "$PLIST" 2>/dev/null; then
        echo "API уже запущен"
        return 0
    fi
    launchctl load "$KB_ROOT/launchd/$PLIST.plist" 2>/dev/null || true
    sleep 2
    if curl -s -m 3 "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
        echo "API запущен на http://127.0.0.1:$PORT"
    else
        echo "API запускается... (проверьте лог: $LOG_FILE)"
    fi
}

stop() {
    launchctl unload "$KB_ROOT/launchd/$PLIST.plist" 2>/dev/null || true
    echo "API остановлен"
}

status() {
    if curl -s -m 3 "http://127.0.0.1:$PORT/api/health" 2>/dev/null | grep -q '"ok": true'; then
        echo "API работает на http://127.0.0.1:$PORT"
    else
        echo "API не запущен"
    fi
}

case "${1:-}" in
    start)   start ;;
    stop)    stop ;;
    restart) stop; sleep 1; start ;;
    status)  status ;;
    *)       usage ;;
esac
