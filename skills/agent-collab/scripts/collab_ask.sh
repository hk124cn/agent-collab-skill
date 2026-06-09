#!/usr/bin/env bash
# collab_ask.sh — 模式 C: 求组 / 求帮助
# 用法: collab_ask.sh <agent> <title> <body> [tag=求组]
# 行为:
#   1) 拼接成 [tag] 开头 + @all 提醒
#   2) 强制 topic=task, type=task
#   3) 在 progress/help-<date>.md 创建工单
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

AGENT="${1:?用法: collab_ask.sh <agent> <title> <body> [tag=求组]}"
TITLE="$2"
BODY="$3"
TAG="${4:-求组}"

date=$(TZ=Asia/Shanghai date '+%Y-%m-%d')
agent_name=$(collab_whoami "$AGENT")

content="🚨 [${TAG}] ${TITLE}

@all 求助 @大秦 @一号 @小白 @顾问9527

${BODY}

发起人: ${agent_name}
时间: ${date}
工单 ID: 待分配 (reply 后会写入 MD)"

resp=$(collab_post "$AGENT" "$content" "task" "")
main_id=$(echo "$resp" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("id","") if isinstance(d,dict) else "")' 2>/dev/null || echo "")

prog="$PROGRESS_DIR/help-${date}.md"
{
  echo "# 🆘 ${TAG} — ${TITLE}"
  echo
  echo "- 发起人: $agent_name"
  echo "- 时间: $date"
  echo "- 主消息: $main_id"
  echo "- 标签: $TAG"
  echo
  echo "## 详细描述"
  echo "$BODY"
  echo
  echo "## 响应时间线"
  echo "- $(date '+%H:%M') 工单创建"
  echo
  echo "## 响应记录"
  echo "_待补充_"
} > "$prog"

_collab_log INFO "[$agent_name] 求${TAG}: $TITLE → $main_id"
echo "OK id=$main_id md=$prog"
