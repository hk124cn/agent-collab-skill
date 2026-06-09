#!/usr/bin/env bash
# collab_read.sh — 拉取消息（命令行版）
# 用法: collab_read.sh [date=today] [topic=] [limit=50]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

DATE="${1:-today}"
TOPIC="${2:-}"
LIMIT="${3:-50}"

resp=$(collab_read "$DATE" "$TOPIC" "$LIMIT")
echo "$resp" | python3 -m json.tool
