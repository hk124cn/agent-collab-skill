#!/usr/bin/env bash
# collab_progress.sh — 模式 D: 把指定 topic 当天消息汇总成 MD
# 用法: collab_progress.sh <topic_id> [date=today] [output_path=auto]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

TOPIC="${1:?用法: collab_progress.sh <topic_id> [date] [output]}"
DATE="${2:-today}"
OUT="${3:-}"

if [[ "$DATE" == "today" ]]; then
  DATE=$(TZ=Asia/Shanghai date '+%Y-%m-%d')
fi
[[ -z "$OUT" ]] && OUT="$PROGRESS_DIR/${TOPIC}-${DATE}.md"

resp=$(collab_read "$DATE" "$TOPIC" 500)
echo "$resp" | python3 -c "
import json,sys,os
from collections import defaultdict
data=json.load(sys.stdin)
date='$DATE'
topic='$TOPIC'
out='$OUT'

# 时间线
timeline=defaultdict(list)
for m in data:
    ts=m.get('timestamp','')[:16]
    timeline[ts].append(m)

# 参与人
authors=defaultdict(int)
for m in data:
    a=m.get('author_name','?')
    authors[a]+=1

# 找出主消息和回复
mains=[m for m in data if not m.get('reply_to')]
replies=[m for m in data if m.get('reply_to')]

# 提取关键字 (粗略)
keywords=defaultdict(int)
import re
for m in data:
    for w in re.findall(r'[一-龥]{2,8}|[A-Za-z]{3,15}', m.get('content','')):
        if len(w)>=2:
            keywords[w]+=1
top_kw=sorted(keywords.items(), key=lambda x:-x[1])[:10]

lines=[]
lines.append(f'# {topic} 讨论汇总 — {date}')
lines.append('')
lines.append('## 概览')
lines.append(f'- 总消息数: {len(data)} (主消息 {len(mains)} / 回复 {len(replies)})')
lines.append(f'- 参与人数: {len(authors)}')
lines.append(f'- 时间跨度: {min(timeline.keys()) if timeline else \"—\"} → {max(timeline.keys()) if timeline else \"—\"}')
lines.append('')
lines.append('## 参与人贡献')
for a,c in sorted(authors.items(), key=lambda x:-x[1]):
    lines.append(f'- {a}: {c} 条')
lines.append('')
lines.append('## 高频词 (Top 10)')
for w,c in top_kw:
    lines.append(f'- {w} ({c})')
lines.append('')
lines.append('## 主消息')
for m in mains:
    lines.append(f\"### [{m.get('timestamp','')[:16]}] {m.get('author_name','')} — {m.get('id','')}\")
    lines.append(f\"  {m.get('content','')}\")
    lines.append('')
lines.append('## 回复时间线')
for ts in sorted(timeline.keys()):
    for m in timeline[ts]:
        if m.get('reply_to'):
            lines.append(f\"- {ts} **{m.get('author_name','')}** → {m.get('reply_to','')[:20]}: {m.get('content','')[:80]}\")
lines.append('')
lines.append('## 关键结论')
lines.append('_由 Agent 后续整理_')
lines.append('')
lines.append('## 未决议题')
lines.append('_由 Agent 后续整理_')
lines.append('')

os.makedirs(os.path.dirname(out), exist_ok=True)
open(out,'w').write('\\n'.join(lines))
print(f'WROTE {out} ({len(data)} msgs)')
"
_collab_log INFO "汇总完成: $OUT"
