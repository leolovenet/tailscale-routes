# CLAUDE.md

## 项目目标

在 macOS 上启用 Tailscale exit node 时，让 `bypass-routes.txt` 里列出的 IP 段绕过 VPN，继续走物理网关直连。

核心原理：Tailscale 会下发 `0.0.0.0/1` 和 `128.0.0.0/1` 两条 /1 路由覆盖全网。利用路由表最长前缀匹配，手动添加更精确的 /8、/16、/24 就能把这些网段抢回本地出口。

## 架构

Python 单文件主程序 + C 路由助手，两层分工：

- **`tailscale-routes.py`**：控制逻辑（检测 exit node、获取网关、状态管理、热更新 diff、日志），使用 `/usr/bin/python3`（macOS 系统自带，Xcode CLT 提供）
- **`route-helper.c`**：性能热路径，通过 PF_ROUTE socket 批量读写内核路由表，7500 条路由 <150ms
- 通信方式：stdin（CIDR 列表）+ stdout（JSON 统计）+ exit code

## 关键陷阱（都是踩过的坑，改代码前必读）

1. **`scutil State:/Network/Global/IPv4` 在 exit node 激活时返回的是 VPN 网关（`100.x.x.x`），不是物理网关。** 这是因为 Tailscale 激活后 utun 成为 PrimaryService，Global/IPv4 的 Router 字段跟着变。必须用 `netstat -rnf inet` 过滤掉 utun 接口来读物理 default 路由。`get_gateway()` 的实现和注释完整记录了这个坑。

2. **`scutil --mon` 是交互模式**，无法脚本化，不能用来监听网络变化。只能轮询（当前 5 秒）。

3. **`tailscale status --json` 的 `ExitNodeStatus` 字段不稳定**，不要依赖它判断 exit node 是否激活。改用 `route -n get <PROBE_IP>` 检查输出是否包含 `utun`，这是 `is_exit_node_active()` 的做法。探测 IP 可在 `tailscale-routes.conf` 的 `PROBE_IP` 中配置（默认 `8.8.8.8`）。

4. **网络切换时 `/etc/resolv.conf` 不一定更新**，不适合作为 launchd `WatchPaths` 触发器。

5. **macOS 没有 `ipset`、没有 `ip rule`**，pf table 只管防火墙不管路由。Linux 上的 split-tunnel 方案在 macOS 上无法复刻——这也是项目存在的理由。

6. **Tailscale 官方至今不支持 macOS split tunnel**（GitHub issue tailscale/tailscale#13677，pending）。

7. **plist 的 `StandardOutPath` 不要指向脚本自己的日志文件。** Python 脚本用 logging 模块自行写日志文件，plist 不设 `StandardOutPath`，这是故意的。

8. **卸载时必须先停守护进程，再清理路由。** 守护进程有 `KeepAlive=true` 且每 5 秒轮询。

9. **C 助手中 CIDR 前缀解析必须用 `strtol` 而非 `atoi`。** `atoi("")` 和 `atoi("abc")` 返回 0，会生成 0.0.0.0/0（默认路由），root 权限下会误删默认路由。

10. **RTM_CHANGE 不允许网关在目标网段内。** macOS 内核对 RTM_ADD 宽松但对 RTM_CHANGE 严格。测试时网关地址不要在测试路由网段内。

11. **探测 IP 不能被 bypass-routes.txt 中的网段覆盖。** 如果 `PROBE_IP`（默认 `8.8.8.8`）被某条旁路路由覆盖（如 `8.0.0.0/8`），exit node 检测会失效，导致路由无限循环添加/删除。`load_routes()` 会自动检测并排除冲突网段，但应在配置时就避免。

12. **shell 脚本中不要用全角括号紧贴 `$变量`。** 如 `$ROUTE_HELPER）` 中的全角 `）`（UTF-8: `ef bc 89`）会被 bash 当作变量名的一部分，在 `set -u` 下触发 unbound variable 错误。统一用半角 `()` 或用 `${变量}` 隔离。

## 设计约定（修改代码时请遵守）

- **幂等**：C 助手 add 遇 EEXIST 降级为 RTM_CHANGE，del 遇 ESRCH 静默跳过。
- **最小权限 sudo**：sudoers 只授权安装者本人（`$USER`）对 `route-helper` 免密，不使用 setuid。
- **bypass-routes.txt 不需要 sudo**：`/usr/local/etc/` 在装了 Homebrew 的 macOS 上是用户可写的。
- **用户级 LaunchAgent**：plist 安装到 `~/Library/LaunchAgents/`（非系统 Daemon），以当前用户身份运行。
- **exit node 双重检测**：`is_exit_node_active()` 先用 `pgrep` 确认 Tailscale 进程在运行，再用 `route -n get` 检查路由是否走 utun。避免其他 VPN 的 utun 接口造成误判。
- **探测 IP 冲突自动排除**：`load_routes()` 会检查每条 CIDR 是否覆盖 `PROBE_IP`，覆盖的自动排除并记 error 日志，防止检测失效。
- **空网关防御**：`get_gateway()` 返回 None 时不触发路由操作，等下一轮轮询。
- **只用系统自带工具**：Python 只用标准库（兼容 3.9+），C 只用系统头文件，不引入第三方依赖。
- **日志只记摘要**：路由添加/删除只记总数和失败数，不逐条记录。
- **日志带 emoji 前缀**：emoji 区分动作（✅ ➕ ➖ 🔀 🔗 🔌 🗑️ ⚠️ ❌ 🔄 🚀），保持一致。
- **状态文件是 JSON**：`{"gateway": "...", "routes": [...], "mtime": ...}`，守护进程重启后能精确 diff。
- **状态更新必须在操作成功后**：`prev_active`、`active_routes`、`save_state` 只在 `add_routes`/`remove_routes` 返回成功后才执行。失败时保持旧状态，下轮重试。这条规则适用于 watch 主循环的所有分支（首次连接、网关变化、热更新）。
- **原子写入状态文件**：用 write-then-rename（`os.replace`）防止进程被 kill 时留下截断的 JSON。
- **守护进程不得因 I/O 错误崩溃**：`save_state` 等磁盘操作必须 try/except，记日志后继续运行。守护进程的生命周期比单次写入重要。
- **C 代码用 `_Static_assert` 校验缓冲区大小**：`rt_msghdr` 等内核结构体的大小可能因平台而异，编译期断言比运行时溢出更安全。
- **状态文件字段用 `.get()` 访问**：不要假设 JSON 字段一定存在（文件可能被手动编辑或损坏），与 `status()` 中已有的 `.get()` 模式保持一致。

## 常用命令

```bash
tailscale-routes status     # 查看当前状态 + 最近日志
tailscale-routes start      # 启动守护进程
tailscale-routes stop       # 停止守护进程并清理路由
tailscale-routes add        # 手动添加路由
tailscale-routes remove     # 手动删除路由
tailscale-routes watch      # 前台运行守护进程
./install.sh                # 编译 C 助手 + 安装到 /usr/local + 注册 launchd
./uninstall.sh              # 完全卸载

# 测试
sudo bash tests/test_route_helper.sh              # C 助手集成测试 + 性能基准
/usr/bin/python3 -m unittest tests/test_tailscale_routes.py -v  # Python 单元测试
```

关键路径统一定义在 `tailscale-routes.conf` 中，所有脚本共享，不要在其他文件里硬编码路径。
plist 使用模板 `com.local.tailscale-routes.plist.template`，安装时由 `install.sh` 替换占位符生成。

## 性能基准（2026-04-12，Apple Silicon，7500 条 /24 路由）

| 操作 | 耗时 | 说明 |
|------|------|------|
| Add | ~146ms | PF_ROUTE socket RTM_ADD |
| Change | ~262ms | RTM_ADD 失败(EEXIST) + RTM_CHANGE 重试，双倍 syscall |
| Del | ~77ms | RTM_DELETE |

瓶颈在内核路由表操作（每次 write() ~10-20us），stdin/解析开销可忽略。

## 还没有做的事

- IPv6 旁路路由
- 按域名分流（需要 DNS 拦截，架构差异大）
