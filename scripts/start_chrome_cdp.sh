#!/bin/bash
# Chrome CDP 安全啟動腳本
# 用法: bash scripts/start_chrome_cdp.sh
#
# 改動：
# 1. --remote-allow-origins=http://localhost:* (限本機，取代 *)
# 2. profile 隔離在 /tmp/chrome-desktop-profile
# 3. 關閉不必要的功能降低指紋辨識

CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE_DIR="/tmp/chrome-desktop-profile"
CDP_PORT=9222

# 如果已經在跑就不重複啟動
if curl -s -m 2 http://localhost:${CDP_PORT}/json/version > /dev/null 2>&1; then
    echo "Chrome CDP already running on port ${CDP_PORT}"
    curl -s http://localhost:${CDP_PORT}/json/version | python3 -m json.tool 2>/dev/null
    exit 0
fi

echo "Starting Chrome with CDP on port ${CDP_PORT}..."

"${CHROME}" \
    --remote-debugging-port=${CDP_PORT} \
    --remote-allow-origins=http://localhost:*,http://127.0.0.1:* \
    --user-data-dir="${PROFILE_DIR}" \
    --disable-background-timer-throttling \
    --disable-backgrounding-occluded-windows \
    --disable-renderer-backgrounding \
    --no-first-run \
    --disable-default-apps \
    --disable-translate \
    --disable-sync \
    --metrics-recording-only \
    &

sleep 3

if curl -s -m 2 http://localhost:${CDP_PORT}/json/version > /dev/null 2>&1; then
    echo "✅ Chrome CDP started successfully"
    curl -s http://localhost:${CDP_PORT}/json/version | python3 -m json.tool 2>/dev/null
else
    echo "❌ Failed to start Chrome CDP"
    exit 1
fi
