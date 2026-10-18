#!/usr/bin/env bash
# Wio Terminal AI 配额仪表盘 — 一键启动 / 停止 / 查看状态
#
# 用法:
#   ./dashboard.sh start    启动 BLE 服务器 + 数据同步循环(后台)
#   ./dashboard.sh stop     停止所有相关进程
#   ./dashboard.sh status   查看进程与最近日志
#   ./dashboard.sh web      打开网页预览

set -uo pipefail
cd "$(dirname "$0")"

PY=./.venv/bin/python
LOG_DIR=/tmp/scai
mkdir -p "$LOG_DIR"

start() {
  if [[ ! -x "$PY" ]]; then
    echo "ERROR: 虚拟环境不存在，请先执行:"
    echo "  python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt"
    exit 1
  fi
  # 先停掉旧实例，避免端口冲突
  stop >/dev/null 2>&1 || true
  sleep 1

  nohup "$PY" -u server.py --ble --ble-interval 15 > "$LOG_DIR/server.log" 2>&1 &
  echo "server.py   已启动  PID $!"
  nohup "$PY" -u tools/auto_sync.py --interval 60 > "$LOG_DIR/autosync.log" 2>&1 &
  echo "auto_sync   已启动  PID $!"

  sleep 4
  echo
  echo "Web 预览:   http://127.0.0.1:8765/preview.html"
  echo "日志:       $LOG_DIR/server.log  /  $LOG_DIR/autosync.log"
  echo
  echo "屏幕应显示 OpenCode Go 额度 + 今日用量。若套餐额度一直不变，检查环境变量是否已设置:"
  echo "  export OPENCODE_GO_API_KEY=你的 Go 订阅 API Key"
  echo "(Key 过期需在 OpenCode Console 重新生成: https://opencode.ai/console)"
}

stop() {
  pkill -f "server.py --ble" 2>/dev/null && echo "已停止 server.py" || true
  pkill -f "tools/auto_sync.py" 2>/dev/null && echo "已停止 auto_sync.py" || true
}

status() {
  echo "=== 进程 ==="
  pgrep -fl "server.py --ble|tools/auto_sync.py" || echo "(没有运行中的进程)"
  echo
  echo "=== server.py 最近 BLE 推送 ==="
  grep -E "synced|error|Error" "$LOG_DIR/server.log" 2>/dev/null | tail -3
  echo
  echo "=== auto_sync.py 最近同步 ==="
  tail -2 "$LOG_DIR/autosync.log" 2>/dev/null
}

case "${1:-start}" in
  start) start ;;
  stop)  stop ;;
  status) status ;;
  web)   open "http://127.0.0.1:8765/preview.html" ;;
  *) echo "用法: $0 {start|stop|status|web}"; exit 1 ;;
esac
