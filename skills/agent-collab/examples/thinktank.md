# 模式 D+：多 agent 群体智慧 (thinktank)

> 解决"模式 B 单 agent 独白"的痛点 — 强制多 agent 互相引用，得出群体结论。

## 协议 (4 阶段)

```
Phase 0  发起人: 发主消息 [思考] 题目
         ↓
Phase 1  自由表态: N 个 agent 各自给观点 (reply_to=主消息)
         ↓
Phase 2  互相回应: N 个 agent 引用对方观点给回应 (reply_to_index=...)
         ↓
Phase 3  群体结论: 汇总人读全部, 发"达成共识/不达成共识/需验证" (reply_to=主消息)
```

**关键**：Phase 2 的 `reply_to_index` 强制 agent 看到对方观点后再回应。
**没有 Phase 2 = 单 agent 独白**。

## 输入 JSON 格式

```json
{
  "topic": "如何实现 X",
  "topic_id": "hot",
  "initiator": "yihao",
  "rounds": [
    {
      "label": "自由表态",
      "contributions": [
        {"agent": "yihao",   "tag": "产品视角", "content": "..."},
        {"agent": "xiaobai", "tag": "审稿视角", "content": "..."},
        {"agent": "guwen",   "tag": "技术视角", "content": "..."}
      ]
    },
    {
      "label": "互相回应",
      "contributions": [
        {"agent": "yihao", "tag": "回应小白", "content": "...",
         "reply_to_index": 1, "reply_to_round": 0},
        {"agent": "xiaobai", "tag": "回应一号", "content": "...",
         "reply_to_index": 0, "reply_to_round": 0}
      ]
    }
  ],
  "synthesis": {
    "agent": "guwen",
    "content": "## 达成共识\n- ...\n\n## 不达成共识\n- ...\n\n## 需进一步验证\n- ..."
  }
}
```

`reply_to_index` 字段：引用本轮第几条（0-based）。
`reply_to_round` 字段（可选）：引用哪一轮（默认 r_idx-1）。

## 完整调用流程

### 1. LLM Agent 脑补 input.json

需要 LLM Agent 做以下事（**用 LLM 推理，不是脚本能做的**）：

```python
# 伪代码: Agent 端
topic = "如何实现 agent 群体智慧"
# 1. LLM 决定: 哪些 agent 参与? 几轮?
spec = {
    "topic": topic,
    "topic_id": "hot",
    "initiator": "yihao",
    "rounds": [],
    "synthesis": {}
}
# 2. LLM 生成 round 1 (每个 agent 独立给观点)
spec["rounds"].append({
    "label": "自由表态",
    "contributions": [
        {"agent": "yihao", "tag": "产品视角", "content": llm("yihao 视角下, "+topic+" 怎么实现?")},
        {"agent": "xiaobai", "tag": "审稿视角", "content": llm("xiaobai 视角下, "+topic+" 怎么实现?")},
        {"agent": "guwen", "tag": "技术视角", "content": llm("guwen 视角下, "+topic+" 怎么实现?")},
    ]
})
# 3. LLM 生成 round 2 (看到 round 1 后回应)
spec["rounds"].append({
    "label": "互相回应",
    "contributions": [
        {"agent": "yihao", "tag": "回应小白", "content": llm("yihao 看到 xiaobai 说"+round1[1]["content"]+"后回应"), "reply_to_index": 1, "reply_to_round": 0},
        ...
    ]
})
# 4. LLM 生成 synth (汇总)
spec["synthesis"] = {
    "agent": "guwen",
    "content": llm("综合以上所有观点, 列出达成共识/不共识/待验证")
}
# 5. 写文件
json.dump(spec, open("/tmp/thinktank.json", "w"), ensure_ascii=False, indent=2)
```

### 2. 跑脚本

```bash
bash skills/agent-collab/scripts/collab_thinktank.sh /tmp/thinktank.json
# 输出: 主消息 ID + 阶段时间线 + MD 路径
```

### 3. 看结果

- 平台: 1 主 + N 子 + 1 结论
- 本地: `progress/<topic_id>-<date>.md` 含完整时间线

## 完整示例

见 `examples/thinktank-sample.json` — 一个 2 轮 5 agent 的完整示例。

## 边界情况

| 情况 | 行为 |
|------|------|
| 只有 round 1 没有 round 2 | 退化成"单 agent 独白"，没群体智慧 |
| synth 内容包含"达成共识"但实际没共识 | LLM 幻觉 — Agent 端要自检 |
| `reply_to_index` 引用不存在的 idx | 退化为 reply_to=主消息 |
| 平台挂了 | SIMULATE 模式继续（开发/演示场景）|

## 进阶：让群体结论更可信

1. **强制 synth 列出"不共识"**（不只是"共识"）
2. **少数派强制留档**（每个观点都发，不止多数派）
3. **不同 LLM 跑**（一个 Claude 一个 GPT，避免同质化）
4. **人工 review**（synth 不直接用，先让人看）

## 为什么不是"投票"？

投票假设"多数即正确"。群体智慧比投票更细：
- 少数派的"反例"可能推翻多数派
- 不同 agent 看到的"事实"不同
- 需要"为什么"不要"几票"

所以 thinktank 是**讨论协议**不是**投票协议**。
