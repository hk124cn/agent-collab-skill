#!/usr/bin/env bash
# collab_search.sh — 全局搜索
# 用法: collab_search.sh <keyword>
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

KW="${1:?用法: collab_search.sh <keyword>}"
resp=$(collab_search "$KW")
echo "$resp" | python3 -c "
import json,sys
data=json.load(sys.stdin)
print(f'=== 搜索 \"${1}\" 命中 {len(data)} 条 ===')
for m in data[:50]:
    ts=m.get('timestamp','')[:16]
    au=m.get('author_name','')
    tp=m.get('topic','')
    c=m.get('content','')[:100]
    print(f'  [{ts}] {au} ({tp}) {c}')
"
