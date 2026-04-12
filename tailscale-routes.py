#!/usr/bin/python3
"""
tailscale-routes - macOS Tailscale exit node bypass route manager

在 Tailscale exit node 模式下，让指定 IP 段绕过 VPN 走本地网关直连。
通过 route-helper C 程序批量操作 PF_ROUTE socket。

用法:
    tailscale-routes watch    - 守护进程模式（默认）
    tailscale-routes start    - 启动守护进程
    tailscale-routes stop     - 停止守护进程并清理路由
    tailscale-routes add      - 手动添加路由
    tailscale-routes remove   - 手动删除路由
    tailscale-routes status   - 查看当前状态
"""

import argparse
import ipaddress
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

# ── 配置加载 ─────────────────────────────────────────────────

def load_config(conf_path=None):
    """从 tailscale-routes.conf 加载共享路径配置"""
    if conf_path is None:
        candidates = [
            Path("/usr/local/etc/tailscale-routes.conf"),
            Path(__file__).resolve().parent / "tailscale-routes.conf",
        ]
        for p in candidates:
            if p.exists():
                conf_path = p
                break
        else:
            print("❌ 找不到 tailscale-routes.conf", file=sys.stderr)
            sys.exit(1)

    config = {}
    with open(conf_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, val = line.partition("=")
                val = val.strip().strip('"').strip("'")
                val = val.split("#")[0].rstrip()  # 去除行内注释
                config[key.strip()] = val
    return config


# ── 日志 ─────────────────────────────────────────────────────

def setup_logging(log_file, max_lines=2000):
    """配置日志：写文件 + 轮转"""
    logger = logging.getLogger("tailscale-routes")
    logger.setLevel(logging.INFO)

    handler = logging.FileHandler(log_file)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s",
                                           datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(handler)

    # 简单轮转：超过 max_lines 时截断前半
    def rotate():
        try:
            with open(log_file) as f:
                lines = f.readlines()
            if len(lines) > max_lines:
                with open(log_file, "w") as f:
                    f.writelines(lines[max_lines // 2:])
        except OSError:
            pass

    logger.rotate = rotate
    return logger


# ── 网关检测 ─────────────────────────────────────────────────

def get_gateway():
    """
    获取本地物理网关 IP。
    从 netstat -rnf inet 过滤 utun 接口，取第一条物理 default 路由的网关。
    不用 scutil：exit node 激活后 Global/IPv4 返回的是 VPN 网关。
    """
    try:
        result = subprocess.run(
            ["netstat", "-rnf", "inet"],
            capture_output=True, text=True, timeout=5
        )
        for line in result.stdout.splitlines():
            parts = line.split()
            if (len(parts) >= 4
                    and parts[0] == "default"
                    and not parts[3].startswith("utun")):
                try:
                    ipaddress.ip_address(parts[1])
                    return parts[1]
                except ValueError:
                    continue
    except (subprocess.TimeoutExpired, OSError):
        pass
    return None


# ── Exit node 检测 ───────────────────────────────────────────

def _is_tailscale_running():
    """检查 Tailscale 进程是否在运行（App Store 版或 standalone 版）"""
    try:
        # App Store 版: GUI 进程名为 "Tailscale"
        # Standalone 版: 守护进程名为 "tailscaled"
        result = subprocess.run(
            ["pgrep", "-xiE", "tailscale|tailscaled"],
            capture_output=True, timeout=3
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def is_exit_node_active(probe_ip="8.8.8.8"):
    """
    判断 Tailscale exit node 是否激活。
    先检查 Tailscale 进程是否在运行，再检查探测 IP 的路由是否走 utun。
    双重检查避免其他 VPN 的 utun 接口造成误判。

    probe_ip 不能被 bypass-routes.txt 覆盖，否则检测会失效。
    """
    if not _is_tailscale_running():
        return False
    try:
        result = subprocess.run(
            ["route", "-n", "get", probe_ip],
            capture_output=True, text=True, timeout=5
        )
        return "utun" in result.stdout
    except (subprocess.TimeoutExpired, OSError):
        return False


# ── 路由文件加载 ─────────────────────────────────────────────

def load_routes(routes_file, probe_ip=None):
    """
    读取 bypass-routes.txt，返回 set[str]。
    过滤注释、空行、\\r，用 ipaddress 校验 CIDR 格式。
    如果指定了 probe_ip，会检查是否被路由覆盖并排除冲突网段。
    """
    routes = set()
    logger = logging.getLogger("tailscale-routes")
    try:
        with open(routes_file) as f:
            for line in f:
                line = line.strip().replace("\r", "")
                if not line or line.startswith("#"):
                    continue
                try:
                    net = ipaddress.ip_network(line, strict=False)
                    routes.add(str(net))
                except ValueError:
                    logger.warning(f"⚠️  无效 CIDR，跳过: {line}")
    except OSError as e:
        logger.error(f"❌ 路由文件读取失败: {e}")

    # 检查 probe_ip 是否被路由覆盖
    if probe_ip and routes:
        try:
            probe = ipaddress.ip_address(probe_ip)
            conflicts = [r for r in routes
                         if probe in ipaddress.ip_network(r, strict=False)]
            if conflicts:
                for c in conflicts:
                    logger.error(
                        f"❌ 路由 {c} 覆盖了探测 IP {probe_ip}，"
                        f"已排除 (否则 exit node 检测会失效导致路由循环)"
                    )
                routes -= set(conflicts)
        except ValueError:
            pass

    return routes


def get_file_mtime(path):
    """获取文件 mtime，不存在返回 0"""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


# ── Route Helper 调用 ────────────────────────────────────────

def call_route_helper(helper_path, action, cidrs, gateway=None):
    """
    调用 C route-helper 程序。
    返回 (success: bool, result: dict)
    """
    logger = logging.getLogger("tailscale-routes")
    cmd = ["sudo", helper_path, action]
    if gateway:
        cmd.append(gateway)

    stdin_data = "\n".join(cidrs) + "\n"

    try:
        result = subprocess.run(
            cmd, input=stdin_data,
            capture_output=True, text=True, timeout=30
        )
        if result.stdout.strip():
            stats = json.loads(result.stdout.strip())
        else:
            stats = {}
        return result.returncode <= 1, stats
    except (subprocess.TimeoutExpired, OSError, json.JSONDecodeError) as e:
        logger.error(f"❌ route-helper 调用失败: {e}")
        return False, {}


# ── 状态管理 ─────────────────────────────────────────────────

def load_state(state_file):
    """从状态文件加载 JSON，返回 dict 或 None"""
    try:
        with open(state_file) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def save_state(state_file, gateway, routes, mtime):
    """保存状态到 JSON 文件（原子写入：write-then-rename）"""
    logger = logging.getLogger("tailscale-routes")
    state = {
        "gateway": gateway,
        "routes": sorted(routes),
        "mtime": mtime,
    }
    tmp = state_file + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(state, f)
        os.replace(tmp, state_file)
    except OSError as e:
        logger.error(f"❌ 状态文件写入失败: {e}")


def clear_state(state_file):
    """删除状态文件"""
    try:
        os.remove(state_file)
    except OSError:
        pass


# ── 路由操作 ─────────────────────────────────────────────────

def add_routes(config, gateway, cidrs):
    """添加旁路路由"""
    logger = logging.getLogger("tailscale-routes")
    if not gateway:
        logger.error("❌ 获取不到网关，跳过")
        return False

    logger.info(f"➕ 添加旁路路由，网关={gateway}")
    success, stats = call_route_helper(
        config["ROUTE_HELPER"], "add", cidrs, gateway
    )
    total = stats.get("total", 0)
    failed = stats.get("failed", 0)
    if failed > 0:
        logger.info(f"⚠️  路由添加完成（共 {total} 条，{failed} 条失败）")
    else:
        logger.info(f"✅ 路由添加完成（共 {total} 条）")
    return success


def remove_routes(config, cidrs):
    """删除旁路路由"""
    logger = logging.getLogger("tailscale-routes")
    logger.info("➖ 删除旁路路由")
    success, stats = call_route_helper(
        config["ROUTE_HELPER"], "del", cidrs
    )
    total = stats.get("total", 0)
    failed = stats.get("failed", 0)
    if failed > 0:
        logger.info(f"⚠️  路由删除完成（共 {total} 条，{failed} 条失败）")
    else:
        logger.info(f"✅ 路由删除完成（共 {total} 条）")
    return success


# ── 守护进程主循环 ───────────────────────────────────────────

POLL_INTERVAL = 5      # 秒
STABILIZE_WAIT = 2     # VPN 连接后等待路由表稳定的秒数

def watch(config):
    """守护进程主循环"""
    logger = logging.getLogger("tailscale-routes")
    logger.info("🚀 守护进程启动")

    routes_file = config["ROUTES_FILE"]
    state_file = config["STATE_FILE"]
    probe_ip = config.get("PROBE_IP", "8.8.8.8")

    # 启动时清理残留路由
    state = load_state(state_file)
    if state and state.get("routes"):
        remove_routes(config, state["routes"])
    clear_state(state_file)

    prev_active = False
    active_routes = set()
    last_mtime = 0.0

    while True:
        active = is_exit_node_active(probe_ip)
        gw = get_gateway()

        if active:
            if not prev_active:
                # ── exit node 刚连上 ──
                logger.info(f"🔗 Exit node 已连接，等待 {STABILIZE_WAIT}s 稳定...")
                time.sleep(STABILIZE_WAIT)
                gw = get_gateway()
                routes = load_routes(routes_file, probe_ip)
                if not routes:
                    # 路由文件为空，标记已处理，等热更新触发
                    last_mtime = get_file_mtime(routes_file)
                    prev_active = True
                elif add_routes(config, gw, routes):
                    active_routes = routes
                    last_mtime = get_file_mtime(routes_file)
                    save_state(state_file, gw, active_routes, last_mtime)
                    prev_active = True
                # else: add_routes 失败，保持 prev_active=False，下轮重试

            else:
                # ── exit node 保持连接 ──
                current_gw = gw
                state = load_state(state_file)
                saved_gw = state.get("gateway") if state else None

                # 检查网关变化
                if current_gw and saved_gw and current_gw != saved_gw:
                    logger.info(f"🔀 网关变化 {saved_gw} → {current_gw}，重建路由")
                    if not remove_routes(config, active_routes):
                        logger.error("❌ 旧路由清理失败，下轮重试")
                    else:
                        clear_state(state_file)
                        active_routes = set()
                        time.sleep(1)
                        routes = load_routes(routes_file, probe_ip)
                        last_mtime = get_file_mtime(routes_file)
                        if routes and add_routes(config, current_gw, routes):
                            active_routes = routes
                            save_state(state_file, current_gw, active_routes, last_mtime)
                        elif not routes:
                            pass  # 路由文件为空，等热更新触发
                        else:
                            # add 失败，重置状态让下轮重新进入首次连接分支
                            prev_active = False
                            last_mtime = 0.0

                # 检查路由文件热更新
                elif current_gw:
                    current_mtime = get_file_mtime(routes_file)
                    if current_mtime != last_mtime:
                        new_routes = load_routes(routes_file, probe_ip)
                        to_add = new_routes - active_routes
                        to_del = active_routes - new_routes
                        del_ok = remove_routes(config, to_del) if to_del else True
                        add_ok = add_routes(config, current_gw, to_add) if to_add else True
                        if to_add or to_del:
                            logger.info(
                                f"🔄 路由文件变更：+{len(to_add)} -{len(to_del)}"
                            )
                        if del_ok and add_ok:
                            active_routes = new_routes
                            last_mtime = current_mtime
                            save_state(state_file, current_gw, active_routes, last_mtime)
                        else:
                            logger.error("❌ 热更新部分失败，下轮重试")

        else:
            if prev_active:
                # ── exit node 刚断开 ──
                logger.info("🔌 Exit node 已断开，清理路由")
                if remove_routes(config, active_routes):
                    clear_state(state_file)
                    active_routes = set()
                    last_mtime = 0.0
                    prev_active = False
                else:
                    logger.error("❌ 路由清理失败，下轮重试")
            else:
                prev_active = False

        logger.rotate()
        time.sleep(POLL_INTERVAL)


# ── 状态查看 ─────────────────────────────────────────────────

def status(config):
    """打印当前状态"""
    probe_ip = config.get("PROBE_IP", "8.8.8.8")
    label = config.get("PLIST_LABEL", "com.local.tailscale-routes")
    active = is_exit_node_active(probe_ip)
    gw = get_gateway()

    # 检查守护进程是否在运行
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5
        )
        daemon_running = label in result.stdout
    except (subprocess.TimeoutExpired, OSError):
        daemon_running = False

    print(f"守护进程状态   : {'✅ 运行中' if daemon_running else '⭕ 未运行'}")
    print(f"Exit node 状态 : {'✅ 已激活' if active else '⭕ 未激活'}")
    print(f"当前网关       : {gw or '（获取失败）'}")

    state = load_state(config["STATE_FILE"])
    if state:
        print(f"已记录网关     : {state.get('gateway', '—')}")
        print(f"活跃路由条数   : {len(state.get('routes', []))}")
    else:
        print("已记录网关     : （无，路由未添加）")

    routes = load_routes(config["ROUTES_FILE"], probe_ip)
    print(f"配置路由条数   : {len(routes)}")

    print()
    print("最近 10 条日志：")
    log_file = config["LOG_FILE"]
    try:
        with open(log_file) as f:
            lines = f.readlines()
            for line in lines[-10:]:
                print(line, end="")
    except OSError:
        print("（暂无日志）")


# ── 守护进程启停 ─────────────────────────────────────────────

def _get_plist_path(config):
    """推导 launchd plist 安装路径"""
    label = config.get("PLIST_LABEL", "com.local.tailscale-routes")
    return os.path.expanduser(f"~/Library/LaunchAgents/{label}.plist")


def daemon_stop(config):
    """停止守护进程并清理路由"""
    logger = logging.getLogger("tailscale-routes")
    plist = _get_plist_path(config)
    label = config.get("PLIST_LABEL", "com.local.tailscale-routes")

    # 先停守护进程
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5
        )
        if label in result.stdout:
            r = subprocess.run(
                ["launchctl", "unload", plist],
                capture_output=True, text=True, timeout=5
            )
            if r.returncode == 0:
                print("✅ 守护进程已停止")
                logger.info("🔌 守护进程被 stop 命令停止")
            else:
                print(f"⚠️  停止守护进程可能失败: {r.stderr.strip()}")
        else:
            print("⭕ 守护进程未在运行")
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"⚠️  停止守护进程失败: {e}")

    # 再清理路由
    state = load_state(config["STATE_FILE"])
    if state and state.get("routes"):
        if remove_routes(config, state["routes"]):
            clear_state(config["STATE_FILE"])
            print("✅ 路由已清理")
        else:
            print("⚠️  路由清理失败，状态文件已保留，可手动执行 tailscale-routes remove")


def daemon_start(config):
    """启动守护进程"""
    logger = logging.getLogger("tailscale-routes")
    plist = _get_plist_path(config)
    label = config.get("PLIST_LABEL", "com.local.tailscale-routes")

    if not os.path.exists(plist):
        print(f"❌ plist 不存在: {plist}")
        print("   请先执行 ./install.sh 安装")
        return

    # 检查是否已在运行
    try:
        result = subprocess.run(
            ["launchctl", "list"],
            capture_output=True, text=True, timeout=5
        )
        if label in result.stdout:
            print("⭕ 守护进程已在运行")
            return
    except (subprocess.TimeoutExpired, OSError):
        pass

    try:
        r = subprocess.run(
            ["launchctl", "load", plist],
            capture_output=True, text=True, timeout=5
        )
        if r.returncode == 0:
            print("✅ 守护进程已启动")
            logger.info("🚀 守护进程被 start 命令启动")
        else:
            print(f"❌ 启动失败: {r.stderr.strip()}")
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"❌ 启动失败: {e}")


# ── 入口 ─────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="macOS Tailscale exit node bypass route manager"
    )
    parser.add_argument(
        "action", nargs="?", default="watch",
        choices=["watch", "start", "stop", "add", "remove", "status"],
        help="操作：watch(默认) | start | stop | add | remove | status"
    )
    args = parser.parse_args()

    config = load_config()
    logger = setup_logging(config["LOG_FILE"])
    probe_ip = config.get("PROBE_IP", "8.8.8.8")

    if args.action == "watch":
        watch(config)
    elif args.action == "start":
        daemon_start(config)
    elif args.action == "stop":
        daemon_stop(config)
    elif args.action == "add":
        gw = get_gateway()
        routes = load_routes(config["ROUTES_FILE"], probe_ip)
        if routes:
            add_routes(config, gw, routes)
            save_state(config["STATE_FILE"], gw, routes,
                       get_file_mtime(config["ROUTES_FILE"]))
    elif args.action == "remove":
        state = load_state(config["STATE_FILE"])
        if state and state.get("routes"):
            remove_routes(config, state["routes"])
        else:
            routes = load_routes(config["ROUTES_FILE"], probe_ip)
            if routes:
                remove_routes(config, routes)
        clear_state(config["STATE_FILE"])
    elif args.action == "status":
        status(config)


if __name__ == "__main__":
    main()
