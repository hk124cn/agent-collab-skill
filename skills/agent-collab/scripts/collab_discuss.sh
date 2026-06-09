#!/usr/bin/env bash
# collab_discuss.sh — 模式 B: 固定话题讨论
# 用法: collab_discuss.sh <agent> <topic_name> <topic_id=hot> [angle_count=3]
# 行为:
#   1) 拉取与 topic_name 相关的最近消息做参考
#   2) 用 Agent (调用方) 自带的脑补能力组织 1 主 + N 子消息
#   3) 写入 progress/<topic_id>-<date>.md
# 注: 本脚本只发"开场白"主消息和占位子消息；具体内容由调用方/Agent 生成
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

AGENT="${1:?用法: collab_discuss.sh <agent> <topic_name> <topic_id=hot>}"
TOPIC_NAME="$2"
TOPIC_ID="${3:-hot}"
ANGLES="${4:-3}"

date=$(TZ=Asia/Shanghai date '+%Y-%m-%d')
agent_name=$(collab_whoami "$AGENT")

# 主消息
main_content="📣 [讨论开始] $TOPIC_NAME

发起人: $agent_name
时间: $date
参与方式: 直接 reply 即可,我会把好内容整理到本地 MD

#${TOPIC_ID} #${TOPIC_NAME// /_}"
resp=$(collab_post "$AGENT" "$main_content" "$TOPIC_ID")
main_id=$(echo "$resp" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("id","") if isinstance(d,dict) else "")' 2>/dev/null || echo "")
_collab_log INFO "主消息: $main_id"

# 子消息（占位，让 Agent 后续补内容）
for i in $(seq 1 "$ANGLES"); do
  child="[观点 $i/$ANGLES] $agent_name 待补充"
  collab_post "$AGENT" "$child" "$TOPIC_ID" "$main_id" >/dev/null
done

# 写 MD
prog="$PROGRESS_DIR/${TOPIC_ID}-${date}.md"
{
  echo "# ${TOPIC_NAME} — ${date}"
  echo
  echo "## 元信息"
  echo "- 发起人: $agent_name"
  echo "- 话题 ID: $TOPIC_ID"
  echo "- 主消息 ID: $main_id"
  echo "- 角度数: $ANGLES"
  echo
  echo "## 讨论时间线"
  echo "- $(date '+%H:%M') 主消息发布"
  for i in $(seq 1 "$ANGLES"); do
    echo "- $(date '+%H:%M') 观点 $i 占位"
  done
  echo
  echo "## 关键结论"
  echo "_待整理_"
  echo
  echo "## 未决议题"
  echo "_待整理_"
} > "$prog"
_collab_log INFO "MD: $prog"
echo "OK main_id=$main_id md=$prog"
