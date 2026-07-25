#!/bin/bash
# CC Manager 개발용 실행기 - 파일 변경 감지해서 자동 재시작
# (일반 사용은 "CC Manager.command" 사용)

cd "$(dirname "$0")"

echo "════════════════════════════════════════"
echo "  🔧 CC Manager (개발 모드)"
echo "  파일 변경 시 자동 재시작"
echo "  종료: Ctrl+C"
echo "════════════════════════════════════════"

SERVER_PID=""

start_server() {
    python3 sims_cc_manager.py &
    SERVER_PID=$!
    echo "▶  서버 시작 (PID $SERVER_PID)"
}

stop_server() {
    if [ -n "$SERVER_PID" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null
        wait "$SERVER_PID" 2>/dev/null
    fi
}

# Ctrl+C 시 정리하고 종료
cleanup() {
    echo ""
    echo "🛑 종료 중..."
    stop_server
    exit 0
}
trap cleanup INT TERM

# 감시할 파일
WATCH_FILE="sims_cc_manager.py"

start_server
LAST_MTIME=$(stat -f "%m" "$WATCH_FILE")

while true; do
    sleep 1
    if [ ! -f "$WATCH_FILE" ]; then continue; fi
    CURRENT_MTIME=$(stat -f "%m" "$WATCH_FILE")
    if [ "$CURRENT_MTIME" != "$LAST_MTIME" ]; then
        echo ""
        echo "🔄 파일 변경 감지 ($(date +%H:%M:%S)) → 재시작"
        stop_server
        sleep 0.3
        # 문법 체크 먼저
        if ! python3 -c "import ast; ast.parse(open('$WATCH_FILE').read())" 2>/dev/null; then
            echo "⚠️  Python 문법 에러 - 재시작 스킵 (파일 고치면 다시 시도)"
        else
            start_server
        fi
        LAST_MTIME=$CURRENT_MTIME
    fi
done
