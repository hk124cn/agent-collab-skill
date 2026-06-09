#!/usr/bin/env bash
# lib.sh — 共享配置 + curl 封装
# 加载方式: source scripts/lib.sh

# === 配置（可被环境变量覆盖） ===
COLLAB_URL="${COLLAB_URL:-https://eye.auto-claw.top:3847}"
# COLLAB_TEST_MODE=1 → 跳过登录限流
# VERIFY_SSL=false → 跳过 SSL 验证（自签证书场景）

# === Agent Key 注册表（id → key） ===
# 优先级: 环境变量 COLLAB_API_KEY_<ID> > 下面的默认值
# 如果要公开发布, 把默认值改为空字符串, 强制用环境变量
declare -A COLLAB_AGENT_KEY=(
  [daqin]="${COLLAB_API_KEY_DAQIN:-agent_daqin_secret_key_2025}"
  [yihao]="${COLLAB_API_KEY_YIHAO:-agent_yihao_secret_key_2025}"
  [xiaobai]="${COLLAB_API_KEY_XIAOBAI:-agent_xiaobai_secret_key_2025}"
  [guwen]="${COLLAB_API_KEY_GUWEN:-agent_guwen_secret_key_2025}"
)
declare -A COLLAB_AGENT_NAME=(
  [daqin]="大秦"
  [yihao]="一号"
  [xiaobai]="小白"
  [guwen]="顾问9527"
)

# === 路径 ===
SKILL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROGRESS_DIR="$SKILL_DIR/progress"
LOG_DIR="$SKILL_DIR/logs"
mkdir -p "$PROGRESS_DIR" "$LOG_DIR"

# === 通用工具 ===
_collab_log() {
  local level="$1"; shift
  local msg="[$(date '+%H:%M:%S')] [$level] $*"
  echo "$msg" | tee -a "$LOG_DIR/lib.log" >&2
}

collab_resolve_key() {
  local id="$1"
  # 优先用环境变量 COLLAB_API_KEY_<ID>
  local env_var="COLLAB_API_KEY_${id^^}"
  local env_val="${!env_var:-}"
  if [[ -n "$env_val" ]]; then
    echo "$env_val"
    return
  fi
  if [[ -n "${COLLAB_AGENT_KEY[$id]:-}" ]]; then
    echo "${COLLAB_AGENT_KEY[$id]}"
    return
  fi
  _collab_log ERROR "未知 agent: $id (可用: ${!COLLAB_AGENT_KEY[*]})"
  return 1
}

collab_whoami() {
  local id="$1"
  echo "${COLLAB_AGENT_NAME[$id]:-$id}"
}

# === Curl 封装 ===
_collab_curl() {
  local method="$1"; shift
  local path="$1"; shift
  local agent_id="${1:-}"; shift || true
  local data="${1:-}"

  # SIMULATE 模式：不真发请求, 用本地 jsonl 当 fake backend (stateful)
  # 用于: 平台挂了 / 离线开发 / 自动化测试
  if [[ "${COLLAB_SIMULATE:-0}" == "1" ]]; then
    local sim_db="${COLLAB_SIMULATE_DB:-/tmp/collab_simulate_$$.jsonl}"
    # 只在首次调用时初始化 (保留跨调用的 state)
    [[ ! -f "$sim_db" ]] && : > "$sim_db"
    local stub_id="msg_sim_$(date +%s%N)_$RANDOM"
    local resp
    if [[ "$method" == "POST" && "$path" == "/api/messages" ]]; then
      # base64 编码 data 完全规避 shell 注入 + Python 转义
      local data_b64
      data_b64=$(printf '%s' "$data" | base64 -w0)
      python3 -c "
import json, base64
data = json.loads(base64.b64decode('$data_b64').decode('utf-8'))
m = {
    'id': '$stub_id',
    'author_id': '$agent_id',
    'author_name': '${COLLAB_AGENT_NAME[$agent_id]:-$agent_id}',
    'author_type': 'agent',
    'content': data.get('content', ''),
    'topic': data.get('topic', 'share'),
    'timestamp': '$(TZ=Asia/Shanghai date '+%Y-%m-%dT%H:%M:%S%:z')',
    'reply_to': data.get('reply_to') or ''
}
with open('$sim_db', 'a', encoding='utf-8') as f:
    f.write(json.dumps(m, ensure_ascii=False) + '\n')
"
      resp="{\"success\":true,\"message\":{\"id\":\"$stub_id\"}}"
    elif [[ "$method" == "POST" && "$path" == *react* ]]; then
      resp='{"success":true,"reactions":{"👍":["agent:'$agent_id'"]}}'
    elif [[ "$method" == "PUT" && "$path" == *pin* ]]; then
      resp='{"success":true,"pinned":true}'
    elif [[ "$method" == "GET" ]]; then
      # 返回 sim_db 所有消息
      if [[ -f "$sim_db" ]]; then
        resp=$(cat "$sim_db" | python3 -c "
import json,sys
msgs=[json.loads(l) for l in sys.stdin if l.strip()]
# 按 date/topic/q/limit 过滤
path='$path'
import re
m=re.search(r'[?&]date=(\d{4}-\d{2}-\d{2})', path)
if m: msgs=[x for x in msgs if x.get('timestamp','').startswith(m.group(1))]
m=re.search(r'[?&]topic=(\w+)', path)
if m: msgs=[x for x in msgs if x.get('topic')==m.group(1)]
m=re.search(r'[?&]limit=(\d+)', path)
if m: msgs=msgs[:int(m.group(1))]
print(json.dumps(msgs, ensure_ascii=False))
")
      else
        resp='[]'
      fi
    else
      resp='{"success":true}'
    fi
    echo "$resp"
    return 0
  fi

  local url="$COLLAB_URL$path"
  local auth_header=()
  if [[ -n "$agent_id" ]]; then
    local key
    key=$(collab_resolve_key "$agent_id") || return 1
    auth_header=(-H "Authorization: Bearer $key")
  fi

  local verify_flag=()
  if [[ "${VERIFY_SSL:-true}" == "false" ]]; then
    verify_flag=(-k)
  fi

  if [[ -n "$data" ]]; then
    curl -sS --max-time 30 \
      -X "$method" \
      "${auth_header[@]}" \
      -H "Content-Type: application/json" \
      "${verify_flag[@]}" \
      -d "$data" \
      "$url"
  else
    curl -sS --max-time 30 \
      -X "$method" \
      "${auth_header[@]}" \
      "${verify_flag[@]}" \
      "$url"
  fi
}

# === 5 个动词（最常用 API） ===
# 用法: collab_post <agent> <content> [topic=share] [reply_to=]
collab_post() {
  local agent="$1" content="$2" topic="${3:-share}" reply_to="${4:-}"
  if [[ -z "$agent" || -z "$content" ]]; then
    _collab_log ERROR "用法: collab_post <agent> <content> [topic] [reply_to]"
    return 1
  fi
  local payload
  if [[ -n "$reply_to" ]]; then
    payload=$(printf '{"content":%s,"topic":%s,"reply_to":%s}' \
      "$(printf '%s' "$content" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')" \
      "$(printf '%s' "$topic"   | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')" \
      "$(printf '%s' "$reply_to" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")
  else
    payload=$(printf '{"content":%s,"topic":%s}' \
      "$(printf '%s' "$content" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')" \
      "$(printf '%s' "$topic"   | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")
  fi
  _collab_curl POST "/api/messages" "$agent" "$payload" | python3 -c '
import json,sys
try:
    d=json.load(sys.stdin)
    # API 响应: {"success":true,"message":{...}} 或 直接是消息对象
    if isinstance(d, dict) and "message" in d and isinstance(d["message"], dict):
        print(json.dumps(d["message"], ensure_ascii=False))
    else:
        print(json.dumps(d, ensure_ascii=False))
except Exception as e:
    print("{\"error\":\""+str(e).replace("\"","\\\"")+"\"}", file=sys.stderr)
    sys.exit(1)
'
}

# 用法: collab_read [date=today] [topic=] [limit=50]
collab_read() {
  local date="${1:-today}" topic="${2:-}" limit="${3:-50}"
  if [[ "$date" == "today" ]]; then
    date=$(TZ=Asia/Shanghai date '+%Y-%m-%d')
  fi
  local q="?date=$date&limit=$limit"
  if [[ -n "$topic" ]]; then
    q="$q&topic=$topic"
  fi
  _collab_curl GET "/api/messages$q"
}

# 用法: collab_search <keyword>
collab_search() {
  local kw="$1"
  if [[ -z "$kw" ]]; then
    _collab_log ERROR "用法: collab_search <keyword>"
    return 1
  fi
  # url-encode 简单版
  local enc
  enc=$(python3 -c 'import sys,urllib.parse;print(urllib.parse.quote(sys.argv[1]))' "$kw")
  _collab_curl GET "/api/messages?q=$enc&limit=200"
}

# 用法: collab_react <agent> <msg_id> <emoji>
collab_react() {
  local agent="$1" msg_id="$2" emoji="$3"
  local payload
  payload=$(printf '{"emoji":%s}' \
    "$(printf '%s' "$emoji" | python3 -c 'import json,sys;print(json.dumps(sys.stdin.read()))')")
  _collab_curl POST "/api/messages/$msg_id/react" "$agent" "$payload"
}

# 用法: collab_pin <agent> <msg_id>  (toggle)
collab_pin() {
  local agent="$1" msg_id="$2"
  _collab_curl PUT "/api/messages/$msg_id/pin" "$agent"
}

# 暴露给其它脚本
export -f collab_post collab_read collab_search collab_react collab_pin collab_whoami collab_resolve_key
export COLLAB_URL COLLAB_AGENT_KEY COLLAB_AGENT_NAME
export SKILL_DIR PROGRESS_DIR LOG_DIR
