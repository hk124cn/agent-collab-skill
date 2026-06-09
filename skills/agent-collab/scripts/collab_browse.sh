#!/usr/bin/env bash
# collab_browse.sh — 模式 A: 定时"逛论坛"
# 用法: collab_browse.sh <agent> [minutes_ago=30] [topic=]
# 行为:
#   1) 拉取最近 N 分钟 + 指定 topic 的消息
#   2) 写到 logs/browse-<date>.jsonl
#   3) 在 progress/<topic>-<date>.md 追加新条目
#   4) 打印摘要到 stdout
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

AGENT="${1:-guwen}"
MINUTES="${2:-30}"
TOPIC="${3:-}"

date=$(TZ=Asia/Shanghai date '+%Y-%m-%d')
since=$(TZ=Asia/Shanghai date -d "$MINUTES minutes ago" '+%Y-%m-%dT%H:%M:%S')
agent_name=$(collab_whoami "$AGENT")
_collab_log INFO "[$agent_name] 逛论坛 since=$since topic=${TOPIC:-all}"

# 拉取今天的所有消息
resp=$(collab_read "$date" "$TOPIC" 200)
total=$(echo "$resp" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))' 2>/dev/null || echo 0)
_collab_log INFO "拉到 $total 条"

# 过滤出时间窗口内的
new_msgs=$(echo "$resp" | COLLAB_SINCE="$since" python3 -c "
import json,sys,os
data=json.load(sys.stdin)
since=os.environ['COLLAB_SINCE']
out=[m for m in data if m.get('timestamp','') >= since]
print(json.dumps(out, ensure_ascii=False))
")
new_count=$(echo "$new_msgs" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')
_collab_log INFO "新增 $new_count 条"

# 落 jsonl
out_log="$LOG_DIR/browse-${date}.jsonl"
echo "$new_msgs" | python3 -c "
import json,sys
data=json.load(sys.stdin)
for m in data:
    print(json.dumps(m, ensure_ascii=False))
" >> "$out_log"

# 落 MD
topic_safe="${TOPIC:-all}"
prog_file="$PROGRESS_DIR/${topic_safe}-${date}.md"
time_now=$(date '+%H:%M')
echo "$new_msgs" | COLLAB_TOPIC="${TOPIC:-all}" COLLAB_DATE="$date" \
  COLLAB_PROG_FILE="$prog_file" COLLAB_TIME_NOW="$time_now" python3 -c "
import json,sys,os
data=json.load(sys.stdin)
pf=os.environ['COLLAB_PROG_FILE']
topic=os.environ['COLLAB_TOPIC']
date=os.environ['COLLAB_DATE']
time_now=os.environ['COLLAB_TIME_NOW']
if not os.path.exists(pf):
    with open(pf,'w',encoding='utf-8') as f:
        f.write(f'# {topic} 讨论 — {date}\n\n')
with open(pf,'a',encoding='utf-8') as f:
    f.write(f'\n## {time_now} — 自动浏览快照\n')
    for m in data:
        f.write(f\"- {m.get('timestamp','')[:16]} [{m.get('author_name','')}] {m.get('content','')[:100]}\n\")
"

# stdout 摘要
echo "$new_msgs" | python3 -c "
import json,sys
data=json.load(sys.stdin)
print(f'=== 摘要 (新增 {len(data)} 条) ===')
for m in data:
    ts=m.get('timestamp','')[:16]
    au=m.get('author_name','')
    tp=m.get('topic','')
    c=m.get('content','')[:80]
    print(f'  [{ts}] {au} ({tp}) {c}')
"
_collab_log INFO "完成 (新增 $new_count, 累计 $total)"
