#!/usr/bin/env bash
# 一条本地启动命令：重置演示数据（含卡滞/换车/复检失败样例）并启动服务
set -euo pipefail
cd "$(dirname "$0")"
PORT="${PORT:-8080}"
exec python3 -m ramp_system.server --reset --port "$PORT"
