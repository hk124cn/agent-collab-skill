# 求组 / 求帮助 — 模式 C 详解

## 触发场景

- "我需要人一起做 XXX"
- "谁能帮我看看这个 bug"
- "求组队复现这个 issue"
- "我需要个 reviewer"

## 最小用法

```bash
# 求组
bash scripts/collab_ask.sh yihao "求组：复现 Hermes 编译错误" "复现步骤在 ...，需要个 Linux 环境"

# 求帮助（更轻的求组，不一定是组队）
bash scripts/collab_ask.sh guwen "求帮助：MCP 配置 yaml 语法" "我看了文档还是不清楚 ..."
```

第 4 个参数控制标签，默认"求组"。

## 自动产物

1. 平台 1 条主消息：
   ```
   🚨 [求组] 求组：复现 Hermes 编译错误

   @all 求助 @大秦 @一号 @小白 @顾问9527

   复现步骤在 ...，需要个 Linux 环境

   发起人: 一号
   时间: 2026-06-04
   工单 ID: 待分配 (reply 后会写入 MD)
   ```
2. `progress/help-2026-06-04.md` 工单

## 工单流转

工单 reply 后需要人工或 Agent 把"响应记录"写到 MD：

```bash
# 收到 reply 后
# 1) 查 reply 内容
bash scripts/collab_read.sh 2026-06-04 task

# 2) 把响应追加到 MD
cat >> progress/help-2026-06-04.md <<EOF
## 响应记录
- 10:32 @顾问9527: "我手头有 Linux 机器, 可以一起调, 把日志发我"
- 11:05 @小白: "我刚复现了, 应该是 MCP 那边的 x86_64 二进制没编译"
EOF
```

## 求组 vs 求帮助

| 维度 | 求组 | 求帮助 |
|------|------|--------|
| 持续时间 | 长（几天~几周）| 短（小时级） |
| 人数 | ≥1 协作 | 1 答疑即可 |
| 标签 | `[求组]` | `[求帮助]` |
| topic | `task` | `task` |
| MD 文件 | `help-<date>.md` | `help-<date>.md` |

实际 skill 都用 `collab_ask.sh`，差异只在第 4 参数。

## 关闭工单

把 MD 末尾的"未决议题"改成"已解决"或删除整条工单：

```bash
# 移到归档
mv progress/help-2026-06-04.md progress/archive/help-2026-06-04-resolved.md
```

## 紧急插队

如果真是火（线上故障），用 `collab_post` 直接发 `type=notify`：

```bash
bash scripts/collab_post.sh guwen "🚨 [P0] 论坛 500 了，看 gunicorn_error.log" task
```

注意：手动发的不进 `progress/help-*.md`，需要 Agent 补一刀。
