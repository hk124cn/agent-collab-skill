# 发布指南 — agent-collab skill

> 怎么把这个 skill 发给其他服务器上的 agent 用。

## TL;DR

```bash
# 1. 推到 Gitee/GitHub (假设推到 gitee)
cd /home/qinwei129/agent-collab
git init
git add skills/agent-collab/
git commit -m "agent-collab skill v1.0"
git remote add origin git@gitee.com:qinwei129/agent-collab-skill.git
git push -u origin main

# 2. 任何服务器一行安装
ssh user@target "curl -fsSL https://gitee.com/qinwei129/agent-collab-skill/raw/main/skills/agent-collab/install.sh | bash"
```

## 三种发布方式对比

### 方式 A：Git 仓库（推荐）⭐⭐

**适合**: 5+ 台机器、需要版本管理、多人协作

| 平台 | 免费私有仓库 | 国内速度 |
|------|------|------|
| Gitee | ✅ | ✅ 快 |
| GitHub | ✅ (限) | ⚠️ 慢 |
| 自建 Gitea | ✅ | ✅ |

**步骤**：
1. 在 Gitee 建一个空仓库 (例如 `agent-collab-skill`)
2. 本地 `git init` + `git remote add` + `git push`
3. 目标机 `git clone` 或 `curl ... install.sh | bash`

**更新**：
```bash
# 源机
git commit -am "v1.1: 修复 thinktank reply_to_index"
git push

# 所有目标机 (写个 update.sh 跑 cron)
cd /home/qinwei129/agent-collab/skills/agent-collab && git pull
```

### 方式 B：直接 tar 拷贝（最快）⭐

**适合**: 1-3 台机器、不想 git、临时部署

```bash
# 源机
cd /home/qinwei129/agent-collab
tar czf agent-collab-skill-v1.0.tar.gz skills/agent-collab/

# 目标机
scp agent-collab-skill-v1.0.tar.gz user@target:/tmp/
ssh user@target "
  mkdir -p /home/qinwei129/agent-collab
  tar xzf /tmp/agent-collab-skill-v1.0.tar.gz -C /home/qinwei129/agent-collab/
  bash /home/qinwei129/agent-collab/skills/agent-collab/install.sh
"
```

### 方式 C：NPM 包（最正规）⭐⭐⭐

**适合**: 50+ 机器、被 marketplace 收录、CI/CD

把 `install.sh` 改成 `package.json` 的 `bin` 字段：

```jsonc
// skills/agent-collab/package.json
{
  "name": "@yourname/agent-collab-skill",
  "version": "1.0.0",
  "description": "Multi-agent collaboration platform skill",
  "bin": {
    "collab": "scripts/lib.sh"
  },
  "scripts": {
    "postinstall": "bash install.sh"
  }
}
```

发布：`npm publish --access public`
使用：`npm install -g @yourname/agent-collab-skill`

⚠️ 缺点：npm 生态重，对纯 bash 脚本有点 overkill。

---

## 各框架集成方法

### Claude Code

```bash
# install.sh 自动做这个:
mkdir -p ~/.claude/skills
ln -sf /path/to/skill ~/.claude/skills/agent-collab

# Claude Code 启动时自动读 SKILL.md frontmatter 加载
```

### openclaw

```bash
# 假设 openclaw 的 skill 目录是:
#   ~/.openclaw/skills/   (约定)
ln -sf /path/to/skill ~/.openclaw/skills/agent-collab

# openclaw 命令行加载:
openclaw skill load /path/to/skill
```

### Hermes

```bash
# Hermes 的 prompt loader 目录:
#   ~/.hermes/prompt-loader/skills/  (约定)
ln -sf /path/to/skill ~/.hermes/prompt-loader/skills/agent-collab

# Hermes 启动时自动扫描该目录
```

### 自建 Agent（任何能跑 bash 的）

```bash
# 只要把 scripts/ 加到 PATH 或能 source 即可
export PATH="/path/to/skill/scripts:$PATH"
# 或在 agent config 里加:
#   bash_scripts_dir: /path/to/skill/scripts
```

---

## 版本管理

### Git tag 规范

```bash
git tag -a v1.0.0 -m "首个稳定版"
git push --tags

# 目标机装指定版本
SKILL_REF=v1.0.0 bash install.sh
# 或
curl -fsSL https://.../install.sh | SKILL_REF=v1.0.0 bash
```

### SemVer

- **MAJOR** (v2.0.0): 改了 5 动词的语义、删了某个 mode
- **MINOR** (v1.1.0): 加了 mode F、加了新 example
- **PATCH** (v1.0.1): 修了 bug、改了文档

---

## 发布 checklist

发布前确认:

- [ ] `bash install.sh --help` 显示正确
- [ ] `bash install.sh` 在干净环境装成功
- [ ] 5 动词在 SIMULATE 模式都能跑
- [ ] `examples/thinktank-sample.json` 能跑出 7 条消息
- [ ] `progress/` 和 `logs/` 是 .gitignore 的
- [ ] README.md.dist 是 distribution 版本（不是开发版）
- [ ] 没有 API key 硬编码在文档示例里
- [ ] install.sh 里的默认 Gitee 仓库地址改了

---

## 监控 / 升级

```bash
# 加到所有目标机的 cron
0 3 * * 0 cd /path/to/skill && git pull && bash install.sh

# 每周日凌晨 3 点自动 pull + 重新 link
```

---

## 私有 vs 公开

| 选项 | 适合 |
|------|------|
| 公开 Gitee/GitHub | 开源社区、合作方 |
| 私有 Gitee/GitHub | 内部团队、API key 在仓库里 |
| 自建 Gitea | 完全控制、审计合规 |
| 不发仓库，curl 到自托管 raw | 5 台以下 |

⚠️ **API key 已经硬编码** (`agents/registry.json` 和 `scripts/lib.sh`)。如果发布到公开仓库，**必须先改成环境变量 / 加密**。
