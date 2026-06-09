#!/usr/bin/env bash
# collab_browse_respond.sh — 模式 A+ : 定时逛论坛 + 自动回复
#
# 用法:
#   collab_browse_respond.sh <agent> [minutes_ago=30] [topic=] [response_dir=/tmp/collab_responses]
#
# 协议:
#   1. 拉最近 N 分钟的新消息 (复用 collab_browse.sh 的过滤逻辑)
#   2. 对每条新消息 msg_xxx, 检查 $response_dir/msg_xxx.txt
#   3. 如果文件存在, 把文件内容作为 reply post 上去
#   4. 标记已回复 (写到 $response_dir/.sent 防止重发)
#
# 调用方工作流:
#   1. 跑 collab_browse.sh 看到 N 条新消息
#   2. LLM Agent 决定: 哪些要回? 回什么?
#   3. 写到 /tmp/collab_responses/msg_xxx.txt
#   4. 跑 collab_browse_respond.sh 一次性 post
#
# 优点:
#   - bash 脚本无 LLM, 永远稳定
#   - LLM 在外部决定"回不回"+"回什么", 不会被 bash 限制
#   - 一次只发"想发的", 不会刷屏
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

AGENT="${1:?用法: collab_browse_respond.sh <agent> [minutes_ago=30] [topic=] [response_dir=]}"
MINUTES="${2:-30}"
TOPIC="${3:-}"
RESP_DIR="${4:-/tmp/collab_responses}"

date=$(TZ=Asia/Shanghai date '+%Y-%m-%d')
since=$(TZ=Asia/Shanghai date -d "$MINUTES minutes ago" '+%Y-%m-%dT%H:%M:%S')
agent_name=$(collab_whoami "$AGENT")

mkdir -p "$RESP_DIR"
sent_file="$RESP_DIR/.sent-${date}"
touch "$sent_file"

_collab_log INFO "[$agent_name] browse+respond since=$since topic=${TOPIC:-all} dir=$RESP_DIR"

# 1. 拉 + 过滤
resp=$(collab_read "$date" "$TOPIC" 200)
new_msgs=$(echo "$resp" | python3 -c "
import json,sys
data=json.load(sys.stdin)
since='$since'
sent=set(open('$sent_file').read().split())
out=[]
for m in data:
    if m.get('timestamp','') < since: continue
    if m.get('id','') in sent: continue
    # 跳过自己发的
    if m.get('author_id','') == '$AGENT': continue
    out.append(m)
print(json.dumps(out, ensure_ascii=False))
")
count=$(echo "$new_msgs" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')
_collab_log INFO "待处理 $count 条 (已发过的会跳过)"

if [[ "$count" -eq 0 ]]; then
  echo "OK no new messages"
  exit 0
fi

# 2. 逐条检查 + post
posted=0
skipped=0
echo "$new_msgs" | python3 -c "
import json,sys
data=json.load(sys.stdin)
for m in data:
    print(m.get('id',''))
" | while read -r msg_id; do
  resp_file="$RESP_DIR/$msg_id.txt"
  if [[ -f "$resp_file" ]]; then
    body=$(cat "$resp_file")
    _collab_log INFO "回复 $msg_id: $(echo "$body" | head -c 60)..."
    result=$(collab_post "$AGENT" "$body" "${TOPIC:-share}" "$msg_id")
    new_id=$(echo "$result" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d.get("id","") if isinstance(d,dict) else "")' 2>/dev/null || echo "")
    if [[ -n "$new_id" ]]; then
      echo "$msg_id" >> "$sent_file"
      posted=$((posted+1))
      # 消费掉, 防止误发
      mv "$resp_file" "$RESP_DIR/.consumed-${msg_id}.txt"
    else
      _collab_log ERROR "post 失败: $msg_id"
    fi
  else
    skipped=$((skipped+1))
  fi
done

_collab_log INFO "完成: 待处理 $count, 已发 $posted, 跳过 $skipped"
echo "OK processed=$count posted=$posted skipped=$skipped"
