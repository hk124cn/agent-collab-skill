#!/usr/bin/env bash
# collab_react.sh — Emoji 反应
# 用法: collab_react.sh <agent> <msg_id> <emoji>
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

AGENT="${1:?用法: collab_react.sh <agent> <msg_id> <emoji>}"
MSG_ID="$2"
EMOJI="$3"

resp=$(collab_react "$AGENT" "$MSG_ID" "$EMOJI")
echo "$resp" | python3 -m json.tool
