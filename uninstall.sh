#!/bin/bash
# uninstall.sh - 完整卸载
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🗑️  开始卸载 tailscale-routes..."

# ── 加载共享配置 ──────────────────────────────────────────────────
source "$SCRIPT_DIR/tailscale-routes.conf"
PLIST_DST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

# 停止守护进程并清理路由 (stop = unload + remove routes)
if [[ -f "$INSTALL_BIN" ]]; then
  /usr/bin/python3 "$INSTALL_BIN" stop || echo "  ⚠️  停止/清理失败，请手动检查"
fi

# 删除文件
rm -f "$PLIST_DST"
rm -f "$INSTALL_BIN"
rm -f "$ROUTE_HELPER"
rm -f "$CONF_INSTALL_DIR/tailscale-routes.conf"
sudo rm -f "$SUDOERS_FILE"
rm -f "$STATE_FILE"
rm -f "$LOG_FILE"
rm -f "$ERR_FILE"

echo "  ✅ 文件已清理"
echo ""
echo "⚠️  路由配置文件已保留: $ROUTES_FILE"
echo "   如需删除请手动执行: rm $ROUTES_FILE"
echo ""
echo "🎉 卸载完成"
