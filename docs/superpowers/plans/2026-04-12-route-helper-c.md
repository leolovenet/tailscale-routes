# route-helper C 助手实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现一个 C 程序，通过 PF_ROUTE socket 批量添加/删除 IPv4 路由，7500 条路由 <100ms。

**Architecture:** 单个 C 源文件，从 stdin 读取 CIDR 列表，通过 macOS routing socket 直写内核路由表。输出 JSON 统计结果。

**Tech Stack:** C (macOS system headers: net/route.h, netinet/in.h), cc (Xcode CLT)

---

### Task 1: 编写 route-helper.c

**Files:**
- Create: `route-helper.c`

- [ ] **Step 1: 创建 route-helper.c**

```c
/*
 * route-helper.c
 * 通过 PF_ROUTE socket 批量添加/删除 IPv4 路由
 *
 * 用法:
 *   echo "1.2.3.0/24" | sudo route-helper add 172.20.10.1
 *   echo "1.2.3.0/24" | sudo route-helper del
 *
 * stdin: 每行一个 CIDR（由调用方预先过滤注释和空行）
 * stdout: JSON 统计 {"total":N,"added":N,"changed":N,"failed":N}
 * exit: 0=全部成功, 1=部分失败, 2=致命错误
 */

#include <sys/socket.h>
#include <sys/types.h>
#include <net/route.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>

#define BUF_SIZE 512

/* ── CIDR 解析 ──────────────────────────────────────────────── */

static int
parse_cidr(const char *cidr, struct in_addr *addr, struct in_addr *mask)
{
    char buf[64];
    strncpy(buf, cidr, sizeof(buf) - 1);
    buf[sizeof(buf) - 1] = '\0';

    char *slash = strchr(buf, '/');
    if (!slash)
        return -1;
    *slash = '\0';

    int prefix = atoi(slash + 1);
    if (prefix < 0 || prefix > 32)
        return -1;

    if (inet_pton(AF_INET, buf, addr) != 1)
        return -1;

    mask->s_addr = (prefix == 0) ? 0 : htonl(~((1u << (32 - prefix)) - 1));

    /* 清除主机位，确保网络地址规范 */
    addr->s_addr &= mask->s_addr;

    return 0;
}

/* ── sockaddr_in 填充 ───────────────────────────────────────── */

static void
fill_sockaddr_in(struct sockaddr_in *sa, struct in_addr addr)
{
    memset(sa, 0, sizeof(*sa));
    sa->sin_len    = sizeof(*sa);
    sa->sin_family = AF_INET;
    sa->sin_addr   = addr;
}

/* ── 构造路由消息 ───────────────────────────────────────────── */
/*
 * 路由消息布局：rt_msghdr + sockaddr_in(dst) [+ sockaddr_in(gw)] + sockaddr_in(mask)
 * sockaddr 的排列顺序由 RTA_ 位掩码决定：DST(0x1) < GATEWAY(0x2) < NETMASK(0x4)
 * del 操作不需要 gateway，所以 mask 紧跟 dst
 */

static int
build_msg(char *buf, int type, int seq,
          struct in_addr dst, struct in_addr *gw, struct in_addr mask)
{
    memset(buf, 0, BUF_SIZE);

    struct rt_msghdr *hdr = (struct rt_msghdr *)buf;
    hdr->rtm_version = RTM_VERSION;
    hdr->rtm_type    = type;
    hdr->rtm_flags   = RTF_UP | RTF_GATEWAY | RTF_STATIC;
    hdr->rtm_seq     = seq;
    hdr->rtm_pid     = getpid();

    char *cp = buf + sizeof(struct rt_msghdr);

    /* RTA_DST */
    fill_sockaddr_in((struct sockaddr_in *)cp, dst);
    cp += sizeof(struct sockaddr_in);

    /* RTA_GATEWAY（仅 add/change） */
    if (gw) {
        fill_sockaddr_in((struct sockaddr_in *)cp, *gw);
        cp += sizeof(struct sockaddr_in);
        hdr->rtm_addrs = RTA_DST | RTA_GATEWAY | RTA_NETMASK;
    } else {
        hdr->rtm_addrs = RTA_DST | RTA_NETMASK;
    }

    /* RTA_NETMASK */
    fill_sockaddr_in((struct sockaddr_in *)cp, mask);
    cp += sizeof(struct sockaddr_in);

    hdr->rtm_msglen = (int)(cp - buf);
    return hdr->rtm_msglen;
}

/* ── main ───────────────────────────────────────────────────── */

int
main(int argc, char *argv[])
{
    /* 解析参数 */
    if (argc < 2) {
        fprintf(stderr,
            "Usage: route-helper add <gateway>\n"
            "       route-helper del\n"
            "stdin: one CIDR per line\n");
        return 2;
    }

    int is_add = (strcmp(argv[1], "add") == 0);
    int is_del = (strcmp(argv[1], "del") == 0);

    if (!is_add && !is_del) {
        fprintf(stderr, "Unknown action: %s (use 'add' or 'del')\n", argv[1]);
        return 2;
    }

    struct in_addr gw_addr;
    if (is_add) {
        if (argc < 3) {
            fprintf(stderr, "add requires a gateway argument\n");
            return 2;
        }
        if (inet_pton(AF_INET, argv[2], &gw_addr) != 1) {
            fprintf(stderr, "Invalid gateway: %s\n", argv[2]);
            return 2;
        }
    }

    /* 打开路由套接字 */
    int s = socket(PF_ROUTE, SOCK_RAW, AF_INET);
    if (s < 0) {
        perror("socket(PF_ROUTE)");
        return 2;
    }

    /* 禁止回环——不读响应，避免接收缓冲区溢出 */
    int off = 0;
    setsockopt(s, SOL_SOCKET, SO_USELOOPBACK, &off, sizeof(off));

    char buf[BUF_SIZE];
    char line[256];
    int total = 0, added = 0, changed = 0, deleted = 0, failed = 0;
    int seq = 0;

    /* 逐行处理 stdin */
    while (fgets(line, sizeof(line), stdin)) {
        line[strcspn(line, "\r\n")] = '\0';
        if (line[0] == '\0')
            continue;

        struct in_addr net_addr, net_mask;
        if (parse_cidr(line, &net_addr, &net_mask) != 0) {
            fprintf(stderr, "Invalid CIDR: %s\n", line);
            failed++;
            total++;
            continue;
        }

        total++;

        if (is_add) {
            int len = build_msg(buf, RTM_ADD, ++seq,
                                net_addr, &gw_addr, net_mask);
            if (write(s, buf, len) < 0) {
                if (errno == EEXIST) {
                    /* 路由已存在，降级为 change */
                    len = build_msg(buf, RTM_CHANGE, ++seq,
                                    net_addr, &gw_addr, net_mask);
                    if (write(s, buf, len) < 0) {
                        failed++;
                    } else {
                        changed++;
                    }
                } else {
                    failed++;
                }
            } else {
                added++;
            }
        } else {
            /* del */
            int len = build_msg(buf, RTM_DELETE, ++seq,
                                net_addr, NULL, net_mask);
            if (write(s, buf, len) < 0) {
                if (errno != ESRCH) {
                    /* ESRCH = 路由不存在，静默跳过 */
                    failed++;
                }
            } else {
                deleted++;
            }
        }
    }

    close(s);

    /* 输出 JSON 统计 */
    if (is_add)
        printf("{\"total\":%d,\"added\":%d,\"changed\":%d,\"failed\":%d}\n",
               total, added, changed, failed);
    else
        printf("{\"total\":%d,\"deleted\":%d,\"failed\":%d}\n",
               total, deleted, failed);

    return (failed > 0) ? 1 : 0;
}
```

---

### Task 2: 编译并冒烟测试

**Files:**
- Read: `route-helper.c`

- [ ] **Step 1: 编译**

```bash
cc -O2 -Wall -Wextra -o route-helper route-helper.c
```

Expected: 零 warning，生成 `route-helper` 二进制。

- [ ] **Step 2: 测试参数校验（不需要 sudo）**

```bash
# 无参数
./route-helper
# Expected: Usage 提示, exit code 2

# 未知操作
./route-helper foo
# Expected: "Unknown action: foo", exit code 2

# add 缺少网关
echo "1.0.0.0/8" | ./route-helper add
# Expected: "add requires a gateway argument", exit code 2

# 无效网关
echo "1.0.0.0/8" | ./route-helper add not-an-ip
# Expected: "Invalid gateway: not-an-ip", exit code 2
```

- [ ] **Step 3: 冒烟测试 add + del（需要 sudo）**

```bash
# 添加一条测试路由（TEST-NET-2, RFC 5737）
echo "198.51.100.0/24" | sudo ./route-helper add 172.20.10.1
# Expected: {"total":1,"added":1,"changed":0,"failed":0}

# 验证路由存在
netstat -rnf inet | grep 198.51.100
# Expected: 198.51.100 ... 172.20.10.1

# 删除
echo "198.51.100.0/24" | sudo ./route-helper del
# Expected: {"total":1,"deleted":1,"failed":0}

# 验证路由已删除
netstat -rnf inet | grep 198.51.100
# Expected: 无输出
```

- [ ] **Step 4: 提交**

```bash
git add route-helper.c
git commit -m "feat: add route-helper C program for bulk PF_ROUTE operations"
```

---

### Task 3: 编写集成测试脚本

**Files:**
- Create: `tests/test_route_helper.sh`

- [ ] **Step 1: 创建 tests 目录和测试脚本**

```bash
#!/bin/bash
# tests/test_route_helper.sh
# route-helper 集成测试（需要 sudo）
#
# 用法: sudo bash tests/test_route_helper.sh
#
# 使用 TEST-NET-2 (198.51.100.0/24, RFC 5737) 做功能测试
# 使用 10.128.0.0/9 子段做性能测试
set -uo pipefail

HELPER="./route-helper"
GATEWAY="198.51.100.1"   # 测试用网关（不需要真实存在）
PASS=0
FAIL=0
TOTAL=0

# ── 测试框架 ─────────────────────────────────────────────────
red()   { printf "\033[31m%s\033[0m\n" "$*"; }
green() { printf "\033[32m%s\033[0m\n" "$*"; }
bold()  { printf "\033[1m%s\033[0m\n" "$*"; }

assert_eq() {
    local label="$1" expected="$2" actual="$3"
    (( TOTAL++ ))
    if [[ "$expected" == "$actual" ]]; then
        green "  ✅ $label"
        (( PASS++ ))
    else
        red   "  ❌ $label"
        red   "     expected: $expected"
        red   "     actual:   $actual"
        (( FAIL++ ))
    fi
}

assert_contains() {
    local label="$1" needle="$2" haystack="$3"
    (( TOTAL++ ))
    if echo "$haystack" | grep -q "$needle"; then
        green "  ✅ $label"
        (( PASS++ ))
    else
        red   "  ❌ $label"
        red   "     expected to contain: $needle"
        red   "     actual: $haystack"
        (( FAIL++ ))
    fi
}

assert_not_contains() {
    local label="$1" needle="$2" haystack="$3"
    (( TOTAL++ ))
    if ! echo "$haystack" | grep -q "$needle"; then
        green "  ✅ $label"
        (( PASS++ ))
    else
        red   "  ❌ $label"
        red   "     expected NOT to contain: $needle"
        (( FAIL++ ))
    fi
}

cleanup() {
    # 清理功能测试路由
    echo "198.51.100.0/24" | "$HELPER" del &>/dev/null || true
    echo "203.0.113.0/24"  | "$HELPER" del &>/dev/null || true
    # 清理性能测试路由
    if [[ -f /tmp/route-helper-perf-routes.txt ]]; then
        "$HELPER" del < /tmp/route-helper-perf-routes.txt &>/dev/null || true
        rm -f /tmp/route-helper-perf-routes.txt
    fi
}
trap cleanup EXIT

# ── 前置检查 ─────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    red "请用 sudo 运行: sudo bash $0"
    exit 1
fi

if [[ ! -x "$HELPER" ]]; then
    red "未找到 $HELPER，请先编译: cc -O2 -Wall -o route-helper route-helper.c"
    exit 1
fi

# ── 功能测试 ─────────────────────────────────────────────────
bold "功能测试"

# 清理可能残留的测试路由
cleanup

# Test 1: add 单条路由
bold "  Test 1: add 单条路由"
result=$(echo "198.51.100.0/24" | "$HELPER" add "$GATEWAY")
assert_contains "JSON 包含 added:1" '"added":1' "$result"
routes=$(netstat -rnf inet | grep "198.51.100")
assert_contains "路由表中存在" "198.51.100" "$routes"

# Test 2: 幂等 add（已存在 → change）
bold "  Test 2: 幂等 add（已存在 → change）"
result=$(echo "198.51.100.0/24" | "$HELPER" add "$GATEWAY")
assert_contains "JSON 包含 changed:1" '"changed":1' "$result"

# Test 3: 多条路由 add
bold "  Test 3: 多条路由 add"
result=$(printf "198.51.100.0/24\n203.0.113.0/24\n" | "$HELPER" add "$GATEWAY")
assert_contains "total:2" '"total":2' "$result"

# Test 4: del 路由
bold "  Test 4: del 路由"
result=$(printf "198.51.100.0/24\n203.0.113.0/24\n" | "$HELPER" del)
assert_contains "deleted:2" '"deleted":2' "$result"
routes=$(netstat -rnf inet | grep "198.51.100")
assert_not_contains "路由表中不再存在" "198.51.100" "$routes"

# Test 5: del 不存在的路由（静默跳过）
bold "  Test 5: del 不存在的路由"
result=$(echo "198.51.100.0/24" | "$HELPER" del)
exit_code=$?
assert_eq "exit code 为 0" "0" "$exit_code"
assert_contains "deleted:0" '"deleted":0' "$result"
assert_contains "failed:0" '"failed":0' "$result"

# Test 6: 无效 CIDR
bold "  Test 6: 无效 CIDR"
result=$(echo "not-a-cidr" | "$HELPER" add "$GATEWAY" 2>/dev/null)
assert_contains "failed:1" '"failed":1' "$result"

# Test 7: 空 stdin
bold "  Test 7: 空 stdin"
result=$(echo "" | "$HELPER" add "$GATEWAY")
assert_contains "total:0" '"total":0' "$result"

# ── 性能测试 ─────────────────────────────────────────────────
bold ""
bold "性能测试（7500 条路由）"

# 生成 7500 条 /24 路由：10.128.0.0/24 到 10.157.84.0/24
python3 -c "
for i in range(7500):
    b2 = 128 + i // 256
    b3 = i % 256
    print(f'10.{b2}.{b3}.0/24')
" > /tmp/route-helper-perf-routes.txt

count=$(wc -l < /tmp/route-helper-perf-routes.txt | tr -d ' ')
bold "  生成了 $count 条测试路由"

# add 性能
bold "  Add 性能:"
time_add=$( { time "$HELPER" add "$GATEWAY" < /tmp/route-helper-perf-routes.txt > /tmp/route-helper-perf-add.json; } 2>&1 | grep real | awk '{print $2}' )
add_result=$(cat /tmp/route-helper-perf-add.json)
bold "  结果: $add_result"
bold "  耗时: $time_add"

# 幂等 add 性能（全部 change）
bold "  Change 性能（全部已存在）:"
time_change=$( { time "$HELPER" add "$GATEWAY" < /tmp/route-helper-perf-routes.txt > /tmp/route-helper-perf-change.json; } 2>&1 | grep real | awk '{print $2}' )
change_result=$(cat /tmp/route-helper-perf-change.json)
bold "  结果: $change_result"
bold "  耗时: $time_change"

# del 性能
bold "  Del 性能:"
time_del=$( { time "$HELPER" del < /tmp/route-helper-perf-routes.txt > /tmp/route-helper-perf-del.json; } 2>&1 | grep real | awk '{print $2}' )
del_result=$(cat /tmp/route-helper-perf-del.json)
bold "  结果: $del_result"
bold "  耗时: $time_del"

rm -f /tmp/route-helper-perf-add.json /tmp/route-helper-perf-change.json /tmp/route-helper-perf-del.json

# ── 结果汇总 ─────────────────────────────────────────────────
bold ""
bold "结果: $PASS/$TOTAL passed, $FAIL failed"
if (( FAIL > 0 )); then
    red "FAILED"
    exit 1
else
    green "ALL PASSED"
    exit 0
fi
```

- [ ] **Step 2: 设置执行权限**

```bash
chmod +x tests/test_route_helper.sh
```

- [ ] **Step 3: 提交**

```bash
git add tests/test_route_helper.sh
git commit -m "test: add route-helper integration and performance tests"
```

---

### Task 4: 运行测试

- [ ] **Step 1: 运行集成测试**

```bash
sudo bash tests/test_route_helper.sh
```

Expected output:
```
功能测试
  Test 1: add 单条路由
  ✅ JSON 包含 added:1
  ✅ 路由表中存在
  Test 2: 幂等 add（已存在 → change）
  ✅ JSON 包含 changed:1
  ...

性能测试（7500 条路由）
  生成了 7500 条测试路由
  Add 性能:
  结果: {"total":7500,"added":7500,"changed":0,"failed":0}
  耗时: 0m0.XXXs
  ...

结果: N/N passed, 0 failed
ALL PASSED
```

性能验收标准：add 和 del 各 <100ms（对应 `time` 输出 real < 0.100s）。

- [ ] **Step 2: 如果测试失败，修复并重新运行**

根据失败信息调试。常见问题：
- `socket(PF_ROUTE): Operation not permitted` → 需要 sudo
- `failed` 不为 0 → 检查 stderr 中的具体 CIDR 和 errno
- 性能超标 → 检查 SO_USELOOPBACK 是否生效

- [ ] **Step 3: 测试通过后提交最终状态**

```bash
git add -A
git commit -m "chore: route-helper ready for performance validation"
```

---

### Task 5: 用户性能验证

此任务由用户手动执行，验证真实 bypass-routes.txt 的性能。

- [ ] **Step 1: 用真实路由文件测试 add 性能**

```bash
# 用实际的 bypass-routes.txt（过滤注释和空行后）
grep -v '^\s*#' /usr/local/etc/bypass-routes.txt | grep -v '^\s*$' | \
  time sudo ./route-helper add $(netstat -rnf inet | awk '$1=="default" && $2~/^[0-9]+\./ && $4!~/^utun/ {print $2; exit}')
```

- [ ] **Step 2: 测试 del 性能**

```bash
grep -v '^\s*#' /usr/local/etc/bypass-routes.txt | grep -v '^\s*$' | \
  time sudo ./route-helper del
```

- [ ] **Step 3: 确认性能达标**

验收标准：7500 条路由的 add 和 del 各 < 100ms。

如果达标 → 继续 Python 重写计划。
如果不达标 → 分析瓶颈，优化后重新测试。
