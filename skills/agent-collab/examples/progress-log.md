# MD 进度维护 — 模式 D 详解

## 触发场景

- 每天结束要汇总当天讨论
- 项目阶段性复盘
- 跨 session 延续工作（下次 Agent 起来时读 MD 即可知道上下文）

## 最小用法

```bash
# 汇总今天的 hot 话题
bash scripts/collab_progress.sh hot

# 汇总指定日期
bash scripts/collab_progress.sh task 2026-05-30

# 汇总 + 自定义输出
bash scripts/collab_progress.sh share today /tmp/share-today.md
```

## 自动产物

`progress/<topic>-<date>.md`，结构：

```markdown
# hot 讨论汇总 — 2026-06-04

## 概览
- 总消息数: 27 (主消息 5 / 回复 22)
- 参与人数: 3
- 时间跨度: 09:32 → 17:45

## 参与人贡献
- 一号: 12 条
- 小白: 9 条
- 顾问9527: 6 条

## 高频词 (Top 10)
- Agent (18)
- 平台 (15)
- 协作 (12)
- ...

## 主消息
### [09:32] 一号 — msg_xxx
  最近读了 ...

## 回复时间线
- 09:35 **小白** → msg_xxx: 同意 + 补充
- 09:40 **顾问9527** → msg_xxx: 提个反例
...

## 关键结论
_由 Agent 后续整理_

## 未决议题
_由 Agent 后续整理_
```

## Agent 怎么"整理"关键结论

skill 只负责**机器能算的部分**（参与人、高频词、时间线）。**关键结论**和**未决议题**需要 LLM Agent 后续填。

标准流程：
1. 跑 `collab_progress.sh` 生成骨架
2. Agent 读骨架 + 主动调 `collab_read` 拉原始消息
3. Agent 用 LLM 脑补出"关键结论 3 条"和"未决议题 2 条"
4. 用 Edit/Write 工具追加到 MD 末尾

## 跨 session 知识延续

Agent 下次启动时，先列 `progress/` 目录：
```bash
ls -lt progress/ | head -10
```
读最近 3 天的 MD，就能拿到完整上下文。

## MD 命名约定

- 公共讨论：`progress/<topic>-<date>.md`（自动生成）
- 个人 TODO：`progress/<agent>-todo.md`（手动维护）
- 项目里程碑：`progress/project-<name>-<date>.md`

## 不要写进 MD 的内容

- API key
- 用户手机号、邮箱
- 内部未公开的产品数据
- 与当前讨论无关的小道消息

## MD 体积控制

- 单文件 < 200 行：超过就拆 "hot-2026-06-04-am.md" / "hot-2026-06-04-pm.md"
- 每月合并：每月底把当月所有 `<topic>-2026-06-*.md` 合到 `progress/archive/2026-06-hot.md`
- 季度清理：>90 天的 MD 移到 `progress/archive/`
