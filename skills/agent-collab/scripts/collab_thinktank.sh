#!/usr/bin/env bash
# collab_thinktank.sh — 模式 C: 群体智慧 (多 agent 协商)
#
# 协议 (4 阶段):
#   Phase 0: 发起人发主消息 [思考] 题目
#   Phase 1: 自由表态 (round 1) — 每个 agent 独立给观点 (reply_to=主消息)
#   Phase 2: 互相回应 (round 2) — 每个 agent 引用别人观点给回应 (reply_to=对方消息)
#   Phase 3: 群体总结 (synth)  — 汇总人读全部, 发群体结论 (reply_to=主消息)
#
# 用法:
#   collab_thinktank.sh <input.json>
#
# input.json 格式:
# {
#   "topic": "如何实现 X 功能",
#   "topic_id": "hot",
#   "initiator": "yihao",
#   "main_content": "[思考] ...",      // 可选, 不填自动生成
#   "rounds": [
#     {
#       "label": "自由表态",
#       "contributions": [
#         {"agent": "yihao",   "tag": "产品视角", "content": "..."},
#         {"agent": "xiaobai", "tag": "审稿视角", "content": "..."},
#         {"agent": "guwen",   "tag": "技术视角", "content": "..."}
#       ]
#     },
#     {
#       "label": "互相回应",
#       "contributions": [
#         {"agent": "yihao", "tag": "回应小白", "content": "...",
#          "reply_to_index": 1}             // 引用的 round 内第几条
#       ]
#     }
#   ],
#   "synthesis": {
#     "agent": "guwen",
#     "content": "群体结论: ..."
#   }
# }
#
# 完整调用流程 (Agent 端):
#   1. LLM 脑补所有 phase 内容 → 写 input.json
#   2. 跑本脚本 → 自动 post 全部消息 + 写 MD
#   3. 用户/其他 agent 看到后可以在平台继续 reply (脚本不参与)
#
# 关键设计:
#   - 内容由 LLM 决定, bash 脚本只负责"按协议 post"
#   - reply_to 支持"引用具体某条" (round 2 的关键)
#   - synth 自动 reply_to=主消息, 出现在主消息下
#   - 一次落 MD: 含全部消息 ID + 时间线 + 群体结论
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/lib.sh"

INPUT="${1:?用法: collab_thinktank.sh <input.json>}"
if [[ ! -f "$INPUT" ]]; then
  _collab_log ERROR "找不到输入: $INPUT"
  exit 1
fi

# 全部 python 处理, 因为 JSON 解析和状态机用 bash 写容易错
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_DIR="$(cd "$SELF_DIR/.." && pwd)"
LIB="$SELF_DIR/lib.sh"

python3 - "$INPUT" "$LIB" "$SKILL_DIR" "$COLLAB_SIMULATE" "$COLLAB_SIMULATE_DB" <<'PYEOF'
import json, sys, os, subprocess
input_path = sys.argv[1]
lib_path = sys.argv[2]
skill_dir = sys.argv[3]
sim_flag = sys.argv[4] if len(sys.argv) > 4 else "0"
sim_db = sys.argv[5] if len(sys.argv) > 5 else ""

with open(input_path) as f:
    spec = json.load(f)

topic = spec.get("topic", "未命名话题")
topic_id = spec.get("topic_id", "hot")
initiator = spec.get("initiator", "yihao")
main_content = spec.get("main_content", "")
rounds = spec.get("rounds", [])
synthesis = spec.get("synthesis", {})

# 解析 skill 路径 (parent of scripts/)
# skill_dir 和 lib_path 已从 sys.argv 传入

def call_lib_function(func, *args):
    """调 lib.sh 里的函数, args 都是 raw string (脚本内部会自己 JSON encode)"""
    cmd = f'source "{lib_path}" && {func} ' + ' '.join(
        f'{json.dumps(a)}' for a in args  # 用 json.dumps 处理 shell 转义, 但内容仍是 raw
    )
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                          env={**os.environ, "COLLAB_SIMULATE": sim_flag, "COLLAB_SIMULATE_DB": sim_db},
                          check=True).stdout

def call_bash(args_list, input_data=None):
    """直接调 bash 命令"""
    return subprocess.run(args_list, capture_output=True, text=True, input=input_data,
                          env={**os.environ, "COLLAB_SIMULATE": sim_flag, "COLLAB_SIMULATE_DB": sim_db},
                          check=True).stdout

def post_message(agent, content, topic_id, reply_to):
    """直接 post, 绕开 collab_post 内部的二次 JSON encoding"""
    payload = {"content": content, "topic": topic_id}
    if reply_to:
        payload["reply_to"] = reply_to
    # 把 JSON 写到一个临时文件, 让 bash 读取 (避免 shell 转义地狱)
    import tempfile
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
    json.dump(payload, tmp, ensure_ascii=False)
    tmp.close()
    payload_path = tmp.name
    cmd = f'source "{lib_path}" && _collab_curl POST "/api/messages" "{agent}" "$(cat {payload_path})"'
    env = {**os.environ, "COLLAB_SIMULATE": sim_flag, "COLLAB_SIMULATE_DB": sim_db}
    try:
        proc = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                              env=env, check=True)
        result = proc.stdout
    except subprocess.CalledProcessError as e:
        print(f"[ERROR post] agent={agent} rc={e.returncode} stderr={e.stderr[:500]!r}", file=sys.stderr)
        raise
    finally:
        try: os.unlink(payload_path)
        except: pass
    resp = json.loads(result)
    if isinstance(resp, dict) and "message" in resp:
        return resp["message"]
    return resp

# === Phase 0: 主消息 ===
date = subprocess.run(["bash", "-c", "TZ=Asia/Shanghai date '+%Y-%m-%d'"],
                      capture_output=True, text=True).stdout.strip()
time_now = subprocess.run(["bash", "-c", "TZ=Asia/Shanghai date '+%H:%M'"],
                          capture_output=True, text=True).stdout.strip()

if not main_content:
    main_content = f"🧠 [思考开始] {topic}\n\n发起人: {initiator}\n时间: {date} {time_now}\n参与 agent: 见子消息\n\n我将综合各 agent 观点, 群体结论放在最后"

# post 主消息
main_resp = post_message(initiator, main_content, topic_id, None)
main_id = main_resp.get("id", "")
print(f"[Phase 0] 主消息: {main_id}")

# === Phase 1/2: rounds ===
msg_index = {}   # (round_idx, contrib_idx) -> msg_id
round_msg_ids = []  # for MD

for r_idx, r in enumerate(rounds):
    label = r.get("label", f"Round {r_idx+1}")
    print(f"[Phase {r_idx+1}] {label}")
    for c_idx, c in enumerate(r.get("contributions", [])):
        agent = c.get("agent", "")
        tag = c.get("tag", "")
        content = c.get("content", "")
        # 拼接内容
        if tag:
            full_content = f"💡 [{tag}] {content}"
        else:
            full_content = content
        # reply_to: 优先 reply_to, 其次 reply_to_index
        reply_to_raw = c.get("reply_to")
        reply_to_index = c.get("reply_to_index")
        if reply_to_raw == "main" or (reply_to_raw is None and r_idx == 0 and reply_to_index is None):
            reply_to = main_id
        elif reply_to_index is not None:
            # 引用前一轮的某条 (默认 reply_to_round = r_idx-1, 但 JSON 可指定 reply_to_round)
            ref_round = c.get("reply_to_round", r_idx - 1)
            ref_idx = reply_to_index
            reply_to = msg_index.get((ref_round, ref_idx), main_id)
        elif isinstance(reply_to_raw, int):
            reply_to = msg_index.get((r_idx, reply_to_raw), main_id)
        elif reply_to_raw is not None:
            # 显式给定的消息 ID
            reply_to = reply_to_raw
        else:
            # round 2+ 没指定 → 默认 reply_to=主消息
            reply_to = main_id
        # post
        result_data = post_message(agent, full_content, topic_id, reply_to)
        new_id = result_data.get("id", "")
        msg_index[(r_idx, c_idx)] = new_id
        print(f"  - [{agent}] {new_id} → {reply_to}")

# === Phase 3: synth ===
if synthesis:
    agent = synthesis.get("agent", "guwen")
    content = synthesis.get("content", "")
    if not content.startswith("🎯"):
        content = f"🎯 [群体结论]\n\n{content}"
    result_data = post_message(agent, content, topic_id, main_id)
    synth_id = result_data.get("id", "")
    print(f"[Phase 3] 群体结论: {synth_id} (by {agent})")
else:
    synth_id = ""
    print(f"[Phase 3] 跳过 (无 synthesis)")

# === 写 MD ===
progress_dir = os.path.join(skill_dir, "progress")
os.makedirs(progress_dir, exist_ok=True)
md_path = os.path.join(progress_dir, f"{topic_id}-{date}.md")

# 收集所有 msg_id 顺序
all_timeline = []
for r_idx, r in enumerate(rounds):
    label = r.get("label", f"Round {r_idx+1}")
    for c_idx, c in enumerate(r.get("contributions", [])):
        all_timeline.append((r_idx, label, c, msg_index.get((r_idx, c_idx), "")))
if synth_id:
    all_timeline.append(("synth", "群体结论", synthesis, synth_id))

with open(md_path, "w") as f:
    f.write(f"# 🧠 {topic} — {date}\n\n")
    f.write(f"## 元信息\n")
    f.write(f"- 发起人: {initiator}\n")
    f.write(f"- 话题 ID: {topic_id}\n")
    f.write(f"- 主消息: {main_id}\n")
    f.write(f"- 群体结论: {synth_id}\n")
    f.write(f"- 阶段数: {len(rounds) + (1 if synth_id else 0)}\n")
    f.write(f"\n## 阶段时间线\n")
    f.write(f"- {time_now} 阶段 0: 发起主消息\n")
    for r_idx, r in enumerate(rounds):
        f.write(f"- {time_now} 阶段 {r_idx+1}: {r.get('label', '')} ({len(r.get('contributions',[]))} 条)\n")
    if synth_id:
        f.write(f"- {time_now} 阶段 {len(rounds)+1}: 群体结论\n")
    f.write(f"\n## 详细记录\n")
    f.write(f"### 阶段 0: 主消息\n> {main_content}\n\n")
    for r_idx, label, c, cid in all_timeline:
        if r_idx == "synth":
            f.write(f"### 群体结论 ({c.get('agent','')})\n> {c.get('content','')}\n\n")
            f.write(f"- 消息 ID: {cid}\n\n")
        else:
            tag = c.get("tag", "")
            f.write(f"### 阶段 {r_idx+1}: {label} — [{c.get('agent','')}] {tag}\n")
            f.write(f"> {c.get('content','')}\n\n")
            f.write(f"- 消息 ID: {cid}\n")
            f.write(f"- reply_to: {c.get('reply_to', main_id if r_idx==0 else main_id)}\n\n")
    f.write(f"\n## 关键结论\n")
    if synth_id:
        f.write(f"由 **{synthesis.get('agent','')}** 在群体结论中给出, 见上文\n")
    else:
        f.write(f"_待 Agent 后续填_\n")
    f.write(f"\n## 未决议题\n_由 Agent 后续填_\n")

print(f"\n[MD] {md_path}")
print(f"\nDONE main={main_id} synth={synth_id} rounds={len(rounds)}")
PYEOF
