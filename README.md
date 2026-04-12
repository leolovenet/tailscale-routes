# tailscale-routes

在 macOS Tailscale exit node 模式下，让指定 IP 段绕过 VPN，直接走本地网关。

## 工作原理

Python 守护进程每 5 秒检查一次 Tailscale exit node 状态，通过 C 路由助手批量操作内核路由表：

- **Exit node 连上** → 自动添加旁路路由 (7500 条 < 150ms)
- **Exit node 断开** → 自动删除旁路路由
- **换了 WiFi 网关变了** → 自动重建路由
- **修改 bypass-routes.txt** → 自动增量更新 (热更新，不需要重启)
- **进程崩溃** → launchd 自动重启

## 前置要求

- macOS (已在 Sequoia 15.x 上测试)
- Tailscale (App Store 或 standalone 版均可)
- Xcode Command Line Tools (`xcode-select --install`，提供 C 编译器和 Python 3)

## 安装

```bash
cd tailscale-routes
./install.sh
```

安装脚本会自动：编译 C 路由助手、安装 Python 主脚本、配置 sudoers 免密、注册 launchd 守护进程。安装后即刻生效。

## 配置路由

编辑 `/usr/local/etc/bypass-routes.txt`，每行一个 CIDR，`#` 开头为注释：

```
# 私有网段
10.0.0.0/8
192.168.0.0/16

# 国内 DNS
114.114.114.0/24
```

修改后 5 秒内自动生效 (热更新)，无需重启守护进程。

## 常用命令

| 命令 | 说明 |
|------|------|
| `tailscale-routes status` | 查看当前状态 |
| `tailscale-routes add` | 手动添加路由 |
| `tailscale-routes remove` | 手动删除路由 |
| `tail -f /tmp/tailscale-routes.log` | 实时查看日志 |

## 卸载

```bash
./uninstall.sh
```

## 文件说明

| 文件 | 说明 |
|------|------|
| `tailscale-routes.py` | Python 主程序 |
| `route-helper.c` | C 路由助手 (PF_ROUTE socket 批量操作) |
| `bypass-routes.txt` | 路由配置 (示例) |
| `tailscale-routes.conf` | 共享路径配置 |
| `com.local.tailscale-routes.plist.template` | launchd 模板 |
| `install.sh` | 安装脚本 |
| `uninstall.sh` | 卸载脚本 |
| `tests/` | 集成测试 + 单元测试 |

## 注意事项

- 安装时会为 `route-helper` 配置当前用户的 sudo 免密 (最小权限)
- 日志自动轮转，最多保留 2000 行
- 状态文件 `/tmp/tailscale-routes.state` 为 JSON 格式，记录活跃路由以支持精确 diff
