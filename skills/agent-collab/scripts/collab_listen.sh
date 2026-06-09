#!/usr/bin/env bash
# collab_listen.sh — 模式 E: SSE 实时监听
# 用法: collab_listen.sh <agent> [keyword=""] [--once]
# 行为:
#   - 持续监听 /api/stream
#   - 命中 keyword 才打日志 (keyword 为空则全打)
#   - 写到 logs/listen-<date>.log
# 退出: Ctrl+C, 或带 --once 时收到 1 条就退出
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

AGENT="${1:?用法: colllab_listen.sh <agent> [keyword]}"
KEYWORD="${2:-}"
ONCE="false"
if [[ "${3:-}" == "--once" || "${2:-}" == "--once" ]]; then
  ONCE="true"
  [[ "${2:-}" == "--once" ]] && KEYWORD=""
fi

agent_name=$(collab_whoami "$AGENT")
date=$(TZ=Asia/Shanghai date '+%Y-%m-%d')
logf="$LOG_DIR/listen-${date}.log"

# 拉取起始时间游标
cursor_file="$LOG_DIR/.cursor-${AGENT}"
cursor=$(cat "$cursor_file" 2>/dev/null || echo "1970-01-01T00:00:00")
_collab_log INFO "[$agent_name] 监听 (cursor=$cursor, keyword=${KEYWORD:-<all>})"

# 持续拉取循环
while true; do
  resp=$(collab_read "$date" "" 50 2>/dev/null || echo "[]")
  new=$(echo "$resp" | python3 -c "
import json,sys
data=json.load(sys.stdin)
cur='$cursor'
kw='$KEYWORD'
out=[]
for m in data:
    if m.get('timestamp','') <= cur: continue
    if kw and kw not in m.get('content',''): continue
    out.append(m)
print(json.dumps(out, ensure_ascii=False))
" 2>/dev/null || echo "[]")

  count=$(echo "$new" | python3 -c 'import json,sys;print(len(json.load(sys.stdin)))')
  if [[ "$count" -gt 0 ]]; then
    echo "$new" | python3 -c "
import json,sys
for m in json.load(sys.stdin):
    print(f\"[{m.get('timestamp','')[:16]}] {m.get('author_name','')}: {m.get('content','')[:100]}\")
" | tee -a "$logf"
    # 更新游标
    echo "$new" | python3 -c "
import json,sys
data=json.load(sys.stdin)
if data:
    print(max(m.get('timestamp','') for m in data))
" > "$cursor_file"
    if [[ "$ONCE" == "true" ]]; then
      _collab_log INFO "--once 模式, 收到 $count 条, 退出"
      exit 0
    fi
  fi
  sleep "${LISTEN_INTERVAL:-5}"
done
