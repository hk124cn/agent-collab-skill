#!/usr/bin/env bash
# collab_pin.sh — 置顶 (toggle, admin only)
# 用法: collab_pin.sh <agent> <msg_id>
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

AGENT="${1:?用法: collab_pin.sh <agent> <msg_id>}"
MSG_ID="$2"

resp=$(collab_pin "$AGENT" "$MSG_ID")
echo "$resp" | python3 -m json.tool
