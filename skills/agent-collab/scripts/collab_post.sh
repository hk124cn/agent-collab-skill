#!/usr/bin/env bash
# collab_post.sh — 命令行发消息封装（debug 用，推荐直接 source lib.sh 用 collab_post）
# 用法: collab_post.sh <agent> <content> [topic=share] [reply_to=]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

AGENT="${1:?用法: collab_post.sh <agent> <content> [topic] [reply_to]}"
CONTENT="$2"
TOPIC="${3:-share}"
REPLY_TO="${4:-}"

resp=$(collab_post "$AGENT" "$CONTENT" "$TOPIC" "$REPLY_TO")
echo "$resp" | python3 -m json.tool
