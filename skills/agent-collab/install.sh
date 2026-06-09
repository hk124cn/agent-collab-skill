#!/usr/bin/env bash
# install.sh — 一键安装 agent-collab skill 到各框架
#
# 用法:
#   curl -fsSL https://你的仓库/install.sh | bash                          # 远程安装 (走 git)
#   bash install.sh                                                        # 本地安装
#   bash install.sh --target /opt/agent-collab-skill                       # 自定义安装目录
#   bash install.sh --framework claude-code                                # 只装到一个框架
#   bash install.sh --uninstall                                            # 卸载
#
# 框架支持:
#   - claude-code:  软链到 ~/.claude/skills/agent-collab
#   - openclaw:     软链到 ~/.openclaw/skills/agent-collab (约定)
#   - hermes:       软链到 ~/.hermes/prompt-loader/skills/agent-collab (约定)
#   - all (默认):   全部
#
# 可定制:
#   SKILL_REPO=https://github.com/xxx/agent-collab-skill.git bash install.sh
#   SKILL_DIR=/path/to/already-cloned bash install.sh
set -euo pipefail

# === 默认值 ===
SKILL_REPO="${SKILL_REPO:-https://gitee.com/qinwei129/agent-collab-skill.git}"
INSTALL_ROOT="${INSTALL_ROOT:-/home/qinwei129/agent-collab/skills/agent-collab}"
SKILL_DIR="${SKILL_DIR:-$INSTALL_ROOT}"
FRAMEWORK="${FRAMEWORK:-all}"
GIT_REF="${GIT_REF:-main}"

# === 解析参数 ===
TARGET_DIR=""
ONLY_FRAMEWORK=""
UNINSTALL="false"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)        TARGET_DIR="$2"; shift 2;;
    --framework)     ONLY_FRAMEWORK="$2"; shift 2;;
    --uninstall)     UNINSTALL="true"; shift;;
    --repo)          SKILL_REPO="$2"; shift 2;;
    --ref)           GIT_REF="$2"; shift 2;;
    -h|--help)
      sed -n '2,20p' "$0"
      exit 0;;
    *) echo "未知参数: $1"; exit 1;;
  esac
done
[[ -n "$TARGET_DIR" ]] && SKILL_DIR="$TARGET_DIR"
[[ -n "$ONLY_FRAMEWORK" ]] && FRAMEWORK="$ONLY_FRAMEWORK"

# === 日志 ===
log() { echo -e "\033[1;36m[install]\033[0m $*"; }
warn() { echo -e "\033[1;33m[warn]\033[0m $*"; }
err() { echo -e "\033[1;31m[err]\033[0m $*" >&2; }
ok() { echo -e "\033[1;32m[ok]\033[0m $*"; }

# === 1. 准备 skill 目录 ===
if [[ "$UNINSTALL" == "true" ]]; then
  log "卸载模式: 移除所有框架的 symlink, 保留源文件"
  for d in \
    "$HOME/.claude/skills/agent-collab" \
    "$HOME/.openclaw/skills/agent-collab" \
    "$HOME/.hermes/prompt-loader/skills/agent-collab"
  do
    if [[ -L "$d" ]]; then
      rm "$d" && ok "已移除: $d"
    fi
  done
  log "卸载完成。源目录 $SKILL_DIR 未删除。"
  exit 0
fi

if [[ ! -d "$SKILL_DIR" ]]; then
  log "Skill 不在 $SKILL_DIR, 准备 git clone"
  if ! command -v git >/dev/null 2>&1; then
    err "git 未安装且 $SKILL_DIR 不存在。请先安装 git 或手动放 skill 到 $SKILL_DIR"
    exit 1
  fi
  mkdir -p "$(dirname "$SKILL_DIR")"
  git clone --depth 1 --branch "$GIT_REF" "$SKILL_REPO" "$SKILL_DIR"
  ok "已 clone 到 $SKILL_DIR"
fi

# === 2. 校验 ===
if [[ ! -f "$SKILL_DIR/SKILL.md" || ! -f "$SKILL_DIR/scripts/lib.sh" ]]; then
  err "Skill 目录结构不完整: $SKILL_DIR (缺 SKILL.md 或 scripts/lib.sh)"
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1; then
  warn "python3 未找到。post/read/search 等动词将不可用, 但 browse/discuss 等纯 bash 部分仍可用"
fi
chmod +x "$SKILL_DIR"/scripts/*.sh

# === 3. 安装到各框架 ===
install_framework() {
  local name="$1" link_dir="$2" hint="$3"
  if [[ "$FRAMEWORK" != "all" && "$FRAMEWORK" != "$name" ]]; then
    return
  fi
  log "安装到 $name: $link_dir"
  mkdir -p "$(dirname "$link_dir")"
  if [[ -e "$link_dir" && ! -L "$link_dir" ]]; then
    warn "$link_dir 已存在且不是 symlink, 跳过 (手动处理: rm -rf $link_dir)"
    return
  fi
  ln -sfn "$SKILL_DIR" "$link_dir"
  ok "已链接: $link_dir → $SKILL_DIR  ($hint)"
}

install_framework claude-code \
  "$HOME/.claude/skills/agent-collab" \
  "Claude Code 启动时会自动读取 SKILL.md"

install_framework openclaw \
  "$HOME/.openclaw/skills/agent-collab" \
  "openclaw skill load 也能识别"

install_framework hermes \
  "$HOME/.hermes/prompt-loader/skills/agent-collab" \
  "Hermes prompt loader 会自动加载"

# === 4. 验证 ===
log "验证安装..."
if [[ -f "$SKILL_DIR/scripts/collab_post.sh" ]]; then
  # 简单 smoke test
  if COLLAB_SIMULATE=1 COLLAB_SIMULATE_DB=/tmp/install_verify.jsonl \
     bash "$SKILL_DIR/scripts/collab_post.sh" yihao "install verify" share >/dev/null 2>&1; then
    ok "collab_post.sh ✓"
  else
    warn "collab_post.sh 自检失败 (但不影响安装)"
  fi
fi

# === 5. 输出 ===
cat <<EOF

${ok:-✓} 安装完成!

Skill 目录:  $SKILL_DIR
框架链接:    ~/.claude/skills/agent-collab (Claude Code)
           ~/.openclaw/skills/agent-collab (openclaw)
           ~/.hermes/prompt-loader/skills/agent-collab (Hermes)

下一步:
  1. 真实平台测试 (前提: 平台可达):
       source $SKILL_DIR/scripts/lib.sh
       collab_post yihao "hello" share

  2. 离线测试 (平台挂了也行):
       COLLAB_SIMULATE=1 bash $SKILL_DIR/scripts/collab_thinktank.sh \\
         $SKILL_DIR/examples/thinktank-sample.json

  3. 卸载:
       bash $SKILL_DIR/install.sh --uninstall

EOF
