#!/bin/bash
# install.sh - 一键安装 tailscale-routes
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BOLD="\033[1m"; GREEN="\033[32m"; YELLOW="\033[33m"; RED="\033[31m"; RESET="\033[0m"

info()  { echo -e "${GREEN}[✅]${RESET} $*"; }
warn()  { echo -e "${YELLOW}[⚠️ ]${RESET} $*"; }
error() { echo -e "${RED}[❌]${RESET} $*"; exit 1; }
title() { echo -e "\n${BOLD}$*${RESET}"; }

# ── 加载共享配置 ──────────────────────────────────────────────────
source "$SCRIPT_DIR/tailscale-routes.conf"
PLIST_DST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

# ── 检查环境 ──────────────────────────────────────────────────────
title "1. 检查环境"

[[ "$(uname)" != "Darwin" ]] && error "仅支持 macOS"

if ! command -v tailscale &>/dev/null \
  && [[ ! -d "/Applications/Tailscale.app" ]]; then
  error "未找到 Tailscale，请先安装(App Store 或 standalone 版均可)"
fi

if ! command -v cc &>/dev/null; then
  error "未找到 C 编译器，请先执行: xcode-select --install"
fi

if ! /usr/bin/python3 --version &>/dev/null; then
  error "未找到 /usr/bin/python3，请先安装 Xcode Command Line Tools: xcode-select --install"
fi

info "环境检查通过"

# ── 编译 C 路由助手 ──────────────────────────────────────────────
title "2. 编译 route-helper"

cc -O2 -Wall -Wextra -o "$SCRIPT_DIR/route-helper" "$SCRIPT_DIR/route-helper.c"
sudo cp "$SCRIPT_DIR/route-helper" "$ROUTE_HELPER"
sudo chown root:wheel "$ROUTE_HELPER"
sudo chmod 755 "$ROUTE_HELPER"
info "route-helper 已编译并安装到 $ROUTE_HELPER"

# ── 安装 Python 主脚本 ───────────────────────────────────────────
title "3. 安装主脚本"

sudo cp "$SCRIPT_DIR/tailscale-routes.py" "$INSTALL_BIN"
sudo chmod +x "$INSTALL_BIN"
info "脚本已安装到 $INSTALL_BIN"

# ── 安装共享配置 ──────────────────────────────────────────────────
sudo mkdir -p "$CONF_INSTALL_DIR"
sudo cp "$SCRIPT_DIR/tailscale-routes.conf" "$CONF_INSTALL_DIR/tailscale-routes.conf"
info "配置已安装到 $CONF_INSTALL_DIR/tailscale-routes.conf"

# ── 安装路由配置文件(已存在则跳过，不覆盖用户数据)────────────
title "4. 安装路由配置"

if [[ -f "$ROUTES_FILE" ]]; then
  warn "路由文件已存在，跳过(保留您的配置): $ROUTES_FILE"
elif [[ -f "$SCRIPT_DIR/bypass-routes.txt" ]]; then
  sudo cp "$SCRIPT_DIR/bypass-routes.txt" "$ROUTES_FILE"
  info "路由文件已安装到 $ROUTES_FILE"
  info "请编辑该文件，添加需要直连的 IP 段"
else
  sudo touch "$ROUTES_FILE"
  warn "未找到示例 bypass-routes.txt，已创建空文件: $ROUTES_FILE"
fi

# ── 配置 sudo 免密(仅对 route-helper)──────────────────────────
title "5. 配置 sudo 免密"

if [[ -f "$SUDOERS_FILE" ]]; then
  warn "sudoers 规则已存在，跳过"
else
  echo "$USER ALL=(ALL) NOPASSWD: $ROUTE_HELPER" | \
    sudo tee "$SUDOERS_FILE" > /dev/null
  sudo chmod 440 "$SUDOERS_FILE"
  info "已添加 sudoers 规则 (仅限 $ROUTE_HELPER)"
fi

# ── 安装 launchd 服务 ─────────────────────────────────────────────
title "6. 安装 launchd 守护进程"

# 如果已存在，先停止旧版本
/usr/bin/python3 "$INSTALL_BIN" stop 2>/dev/null || true
# fallback: 确保 launchd 已卸载 (防止 daemon_stop 中 launchctl unload 失败)
launchctl unload "$PLIST_DST" 2>/dev/null || true

# 从模板生成 plist，替换占位符
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__PLIST_LABEL__|$PLIST_LABEL|g" \
    -e "s|__INSTALL_BIN__|$INSTALL_BIN|g" \
    -e "s|__ERR_FILE__|$ERR_FILE|g" \
    "$SCRIPT_DIR/com.local.tailscale-routes.plist.template" > "$PLIST_DST"
chmod 644 "$PLIST_DST"

# 启动守护进程
/usr/bin/python3 "$INSTALL_BIN" start

# ── 完成 ──────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}🎉 安装完成！${RESET}"
echo ""
echo "常用命令："
echo "  查看状态      : tailscale-routes status"
echo "  停止守护进程  : tailscale-routes stop"
echo "  启动守护进程  : tailscale-routes start"
echo "  查看日志      : tail -f $LOG_FILE"
echo ""
echo "路由配置文件    : $ROUTES_FILE"
