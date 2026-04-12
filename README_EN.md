# tailscale-routes

English | [中文](README.md)

Split-tunneling route manager for macOS when using Tailscale exit nodes.

## The Problem

When Tailscale exit node is enabled, it captures all traffic via two `/1` routes (`0.0.0.0/1` and `128.0.0.0/1`). macOS has no native split tunnel support ([tailscale/tailscale#13677](https://github.com/tailscale/tailscale/issues/13677)), and Linux-style workarounds (`ip rule` + routing tables) don't exist on macOS.

This tool exploits the kernel's **longest prefix match** behavior: by injecting more specific routes (e.g. `/8`, `/16`, `/24`) for designated IP ranges, those networks bypass the VPN and route directly through the local physical gateway. It handles **thousands of routes at once**, fully automated.

## Features

- **High-performance bulk routing**: A C helper program writes directly to the macOS kernel routing table via PF_ROUTE sockets — 7,500 routes added/removed in under 150ms
- **Fully automatic state management**: Daemon polls every 5 seconds, automatically adding routes when exit node connects, cleaning up on disconnect, and rebuilding when switching Wi-Fi networks
- **Hot-reload**: Edits to `bypass-routes.txt` take effect within 5 seconds — no daemon restart needed, only incremental changes are applied (adds new, removes deleted)
- **Crash recovery**: launchd `KeepAlive` ensures automatic restart; a JSON state file enables precise diff-based recovery
- **Zero external dependencies**: Python uses only the standard library (compatible with macOS built-in Python 3.9+), C uses only system headers — no third-party packages required

## Architecture

```
tailscale-routes.py (Python controller)
  ├─ Detect exit node status
  ├─ Discover physical gateway (filtering out Tailscale utun interfaces)
  ├─ Monitor bypass-routes.txt for changes (mtime + set diff)
  └─ Invoke route-helper (communicates via stdin/stdout/exit code)

route-helper (C routing helper)
  ├─ Read CIDR list from stdin
  ├─ Open PF_ROUTE socket
  └─ Bulk-construct rt_msghdr messages and write to kernel routing table
```

## Prerequisites

- macOS (tested on Sequoia 15.x)
- Tailscale (App Store or standalone)
- Xcode Command Line Tools (provides C compiler and Python 3)
  ```bash
  xcode-select --install
  ```

## Installation

```bash
git clone https://github.com/leolovenet/tailscale-routes.git
cd tailscale-routes
./install.sh
```

The installer automatically: compiles the C route helper, installs the Python controller, configures passwordless sudo (for `route-helper` only), and registers a user-level launchd agent. Takes effect immediately — no reboot needed.

<details>
<summary>Files created/modified by install.sh</summary>

| Path | Action | Description |
|------|--------|-------------|
| `/usr/local/bin/route-helper` | Create | Compiled C routing helper binary |
| `/usr/local/bin/tailscale-routes` | Create | Python controller script |
| `/usr/local/etc/tailscale-routes.conf` | Create | Shared path configuration |
| `/usr/local/etc/bypass-routes.txt` | Create (if absent) | Route config file; skipped if already exists |
| `/etc/sudoers.d/tailscale-routes` | Create (requires sudo) | Passwordless sudo rule for current user on route-helper |
| `~/Library/LaunchAgents/com.local.tailscale-routes.plist` | Create | launchd user agent generated from template |

</details>

## Configuring Routes

Edit `/usr/local/etc/bypass-routes.txt`, one CIDR per line, `#` for comments:

```
# Private networks (LAN direct access)
10.0.0.0/8
192.168.0.0/16

# Public DNS servers
114.114.114.0/24
223.5.5.0/24
1.1.1.0/24
```

Changes take effect within 5 seconds (hot-reload) — no restart or manual action required.

> **Note**: Routes in `bypass-routes.txt` must not cover the probe IP (default `8.8.8.8`, configurable via `PROBE_IP` in `tailscale-routes.conf`). This IP is used to detect whether the exit node is active. If a bypass route covers it, detection breaks. The program automatically excludes conflicting routes and logs an error, but it's best to avoid the conflict altogether.

## Usage

| Command | Description |
|---------|-------------|
| `tailscale-routes status` | Show exit node status, gateway, active route count, recent logs |
| `tailscale-routes add` | Manually add routes (for debugging) |
| `tailscale-routes remove` | Manually remove routes |
| `tail -f /tmp/tailscale-routes.log` | Watch daemon logs in real time |

## Uninstall

```bash
./uninstall.sh
```

Stops the daemon, cleans up routes, and removes all installed files. The `bypass-routes.txt` configuration is preserved — delete it manually if needed.

## File Reference

| File | Purpose |
|------|---------|
| `tailscale-routes.py` | Python controller: state machine, gateway detection, hot-reload, logging |
| `route-helper.c` | C routing helper: PF_ROUTE socket bulk route operations |
| `bypass-routes.txt` | IP ranges to bypass VPN (example config) |
| `tailscale-routes.conf` | Shared path configuration for all scripts |
| `com.local.tailscale-routes.plist.template` | launchd agent template |
| `install.sh` | One-command installer (compile + deploy + register service) |
| `uninstall.sh` | One-command uninstaller |
| `tests/` | C integration tests + Python unit tests |

## Security

- sudoers grants passwordless execution of `route-helper` to the **installing user only** — no setuid, no group-wide access
- The C helper strictly validates all CIDR input (`strtol` + `inet_pton`), preventing malformed input from manipulating the default route
- Routes that would cover the probe IP are automatically detected and excluded, preventing a detection-failure loop
- Runs as a user-level LaunchAgent (not a system Daemon), minimizing privilege scope

## Notes

- Logs auto-rotate at 2,000 lines
- State file `/tmp/tailscale-routes.state` is JSON, recording the active gateway and route list for precise diff on daemon restart
- Official macOS split tunnel support in Tailscale is still pending ([tailscale/tailscale#13677](https://github.com/tailscale/tailscale/issues/13677)) — this tool is the workaround until then