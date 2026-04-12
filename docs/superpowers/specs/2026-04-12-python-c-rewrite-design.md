# tailscale-routes 重写设计：Python + C 架构

> 日期：2026-04-12
> 状态：已批准

## 背景

现有 shell 脚本在 macOS 上绕过 Tailscale exit node，让指定 IP 段走本地网关直连。
shell 实现存在以下痛点：

- xargs 缓冲区限制、`set -uo pipefail` 与 `(( ))` 的交互陷阱
- 7500 条路由串行 `route add` 耗时约 4 分钟，不可接受
- 无法热更新 bypass-routes.txt（改完需重启守护进程）
- 不可测试

## 目标

- 7500 条路由批量操作 < 100ms
- 修改 bypass-routes.txt 后自动生效，不需要重启守护进程
- 代码可测试、可维护
- 安装零额外依赖（只需 Xcode CLT，提供 cc 和 python3）

## 明确不做

- IPv6 旁路路由
- 按域名分流
- 第三方 Python 库依赖

## 架构

```
tailscale-routes.py（Python 单进程，主循环轮询）
  ├─ 检测 exit node（subprocess: route -n get 8.8.8.8）
  ├─ 获取物理网关（解析 netstat -rnf inet，过滤 utun）
  ├─ 监控 bypass-routes.txt 变化（比较 mtime）
  └─ 调 route-helper（subprocess: sudo route-helper add/del）

route-helper（C 二进制，stdin 接收路由列表）
  ├─ 解析 stdin 的 CIDR
  ├─ 打开 PF_ROUTE socket
  └─ 批量构造 rt_msghdr 写入内核
```

Python 负责一切控制逻辑。C 只做性能热路径（批量路由操作）。
两者通过 stdin + stdout + exit code 通信。

## 项目结构

```
tailscale-routes/
├── tailscale-routes.py                        # Python 主程序（单文件）
├── route-helper.c                             # C 路由助手源码
├── bypass-routes.txt                          # 路由配置（示例）
├── tailscale-routes.conf                      # 共享路径配置
├── com.local.tailscale-routes.plist.template   # launchd 模板
├── install.sh                                 # 安装脚本
├── uninstall.sh                               # 卸载脚本
├── tests/
│   ├── test_tailscale_routes.py               # Python 单元测试
│   └── test_route_helper.sh                   # C 助手集成测试
├── CLAUDE.md
└── README.md
```

旧文件 `tailscale-routes.sh` 在重写完成后删除。

## C 路由助手：route-helper

### 接口

```bash
# 添加路由
echo "1.2.3.0/24\n5.6.7.0/16" | sudo route-helper add 172.20.10.1

# 删除路由
echo "1.2.3.0/24\n5.6.7.0/16" | sudo route-helper del
```

- argv[1]：操作，`add` 或 `del`
- argv[2]：网关 IP（仅 `add` 时需要）
- stdin：每行一个 CIDR，由 Python 预先过滤注释和空行

### 输出

- stdout：JSON 一行
  - add：`{"total":7500,"added":7498,"changed":2,"failed":0}`
  - del：`{"total":7500,"deleted":7500,"failed":0}`
- exit code：
  - 0：全部成功
  - 1：部分失败（查看 JSON 中的 failed 字段）
  - 2：致命错误（如无法打开 routing socket）

### 内部流程

```
main()
  ├─ 解析 argv，校验参数
  ├─ socket(PF_ROUTE, SOCK_RAW, AF_INET)
  ├─ 从 stdin 逐行读 CIDR
  │   ├─ inet_pton() 解析 IP
  │   ├─ 前缀长度 → 子网掩码
  │   ├─ 构造 rt_msghdr + sockaddr_in（dst + gateway + netmask）
  │   └─ write() 发送到 routing socket
  │       ├─ RTM_ADD 成功 → added++
  │       ├─ RTM_ADD 返回 EEXIST → 重试 RTM_CHANGE → changed++
  │       └─ 其他失败 → failed++
  ├─ printf JSON 到 stdout
  └─ exit(failed > 0 ? 1 : 0)
```

### 幂等性

- add：遇 EEXIST 自动降级为 RTM_CHANGE
- del：遇 ESRCH（路由不存在）直接跳过，不计入 failed

### 性能预期

| 环节 | 7500 条路由 | 耗时 |
|------|-----------|------|
| stdin 读取 120KB | pipe 传输 | <1ms |
| 解析 CIDR | inet_pton + 位运算 | <5ms |
| 写 routing socket | 每次 write ~10us | ~75ms |
| **合计** | | **<100ms** |

## Python 主程序：tailscale-routes.py

### 命令行接口

```bash
tailscale-routes.py watch    # 守护进程模式（默认）
tailscale-routes.py add      # 手动添加路由
tailscale-routes.py remove   # 手动删除路由
tailscale-routes.py status   # 查看当前状态
```

与现有 shell 脚本命令完全一致。

### 函数分组

```
tailscale-routes.py
├─ Config          # 读 tailscale-routes.conf，提供路径常量
├─ Gateway         # get_gateway()：解析 netstat 输出，过滤 utun 接口
├─ ExitNode        # is_active()：route -n get 8.8.8.8 | grep utun
├─ Routes          # load_routes()：读 bypass-routes.txt，过滤注释/空行/\r，
│                  #   ipaddress.ip_network() 校验，返回 set[str]
├─ RouteHelper     # add(gateway, cidrs) / delete(cidrs)：
│                  #   subprocess 调 sudo route-helper，解析 JSON 返回值
├─ HotReload       # 比较 mtime，变了算 set 差集，增量调 RouteHelper
├─ Logger          # logging 模块，写 LOG_FILE，带轮转
├─ Watch           # 主循环，状态机逻辑
└─ main()          # argparse 入口
```

### 热更新逻辑

每轮轮询（5 秒）时检查 bypass-routes.txt 的 mtime：

```python
if file_mtime_changed(routes_file):
    new_routes = load_routes(routes_file)   # set[str]
    old_routes = current_active_routes       # set[str]
    to_add = new_routes - old_routes
    to_del = old_routes - new_routes
    if to_del:
        route_helper.delete(to_del)
    if to_add:
        route_helper.add(gateway, to_add)
    current_active_routes = new_routes
    log("路由文件变更：+{len(to_add)} -{len(to_del)}")
```

### 状态文件

从现有的单行网关 IP 扩展为 JSON：

```json
{
  "gateway": "172.20.10.1",
  "routes": ["1.2.3.0/24", "5.6.7.0/16"],
  "mtime": 1234567890.0
}
```

守护进程重启时读取状态文件，能精确知道内核中有哪些路由，
做准确 diff 而非盲目全量删除再全量添加。

### 关键设计决策保留

以下现有设计决策不变：

- 检测 exit node：`route -n get 8.8.8.8 | grep utun`
- 获取物理网关：`netstat -rnf inet` 过滤 utun（不用 scutil）
- 空网关防御：get_gateway 返回空时不触发操作
- 连接后等待 2 秒稳定再添加路由
- 日志 emoji 前缀风格
- 日志只记摘要不逐条记录

## 安装与权限

### install.sh 变更

```
1. 检查环境
   - macOS 系统检查（现有）
   - Tailscale 安装检查（现有）
   + cc 编译器检查（新增，提示 xcode-select --install）
   + python3 检查（新增，CLT 自带）

2. 编译 C 助手（新增）
   cc -O2 -Wall -o route-helper route-helper.c
   cp route-helper /usr/local/bin/route-helper

3. 安装 Python 主脚本（替代 shell 脚本）
   cp tailscale-routes.py /usr/local/bin/tailscale-routes
   chmod +x

4. 安装配置文件（不变）

5. 配置 sudoers（修改）
   - 旧：$USER ALL=(ALL) NOPASSWD: /sbin/route
   + 新：$USER ALL=(ALL) NOPASSWD: /usr/local/bin/route-helper

6. 安装 launchd（修改）
   plist ProgramArguments 改为：
   python3 /usr/local/bin/tailscale-routes watch
```

### uninstall.sh 变更

- 新增删除 `/usr/local/bin/route-helper`
- `/usr/local/bin/tailscale-routes.sh` → `/usr/local/bin/tailscale-routes`
- 其余逻辑不变（先停守护进程再清路由）

### tailscale-routes.conf 变更

新增：

```bash
ROUTE_HELPER="/usr/local/bin/route-helper"
INSTALL_BIN="/usr/local/bin/tailscale-routes"  # 后缀去掉 .sh
```

### 权限模型

- sudoers 授权 `$USER` 对 `/usr/local/bin/route-helper` 免密（不再授权 /sbin/route）
- 不使用 setuid（行业标准做法是 sudoers）
- 用户级 LaunchAgent（非系统 Daemon）

## 测试

### Python 单元测试（tests/test_tailscale_routes.py）

```
用 unittest 标准库，python3 -m unittest 运行

├─ test_load_routes()        # 注释过滤、空行过滤、\r 清理、CIDR 校验、去重
├─ test_load_routes_invalid  # 非法 CIDR 被跳过并记录警告
├─ test_parse_gateway()      # 从 netstat 输出中提取物理网关，过滤 utun
├─ test_exit_node_detect()   # 从 route -n get 输出判断 utun
├─ test_hot_reload_diff()    # set 差集：新增、删除、不变
├─ test_config_loading()     # conf 文件解析
└─ test_route_helper_json()  # 解析 route-helper 的 JSON 输出
```

mock subprocess 调用，测试解析和状态逻辑，不需要 root 权限。

### C 助手集成测试（tests/test_route_helper.sh）

```bash
# 需要 sudo 权限，手动运行
# 使用 TEST-NET-2 (198.51.100.0/24) 保留地址段

# 功能测试
1. add 单条路由 → 验证 JSON + 验证 netstat
2. 幂等 add（已存在 → change）→ 验证 JSON
3. del 路由 → 验证已删除
4. del 不存在的路由 → 不报错

# 性能测试
5. 生成 7500 条路由，计时 add → 验证 <100ms
6. 计时 del → 验证 <100ms
```

## 实施顺序

1. **先交付 C 助手**（route-helper.c + 编译 + 测试脚本）
2. **用户验证性能**（7500 条路由 < 100ms）
3. 性能达标后，继续 Python 重写
4. 更新 install.sh / uninstall.sh / conf / plist
5. 更新 CLAUDE.md / README.md
6. 删除 tailscale-routes.sh
