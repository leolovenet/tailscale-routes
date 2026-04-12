#!/bin/bash
# uninstall.sh - 完整卸载
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "🗑️  开始卸载 tailscale-routes..."

# ── 加载共享配置 ──────────────────────────────────────────────────
source "$SCRIPT_DIR/tailscale-routes.conf"
PLIST_DST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"

# 先停止守护进程(否则它会在 ≤5 秒内重新添加刚删除的路由)
if launchctl list | grep -q "$PLIST_LABEL" 2>/dev/null; then
  launchctl unload "$PLIST_DST" 2>/dev/null || true
  echo "  ✅ 守护进程已停止"
fi

# 再清理路由
if [[ -f "$INSTALL_BIN" ]]; then
  /usr/bin/python3 "$INSTALL_BIN" remove || echo "  ⚠️  路由清理失败，请手动检查"
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
