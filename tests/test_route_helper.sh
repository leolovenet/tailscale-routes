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
GATEWAY="192.0.2.1"       # TEST-NET-1 (RFC 5737)，不在任何测试路由网段内
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
/usr/bin/python3 -c "
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
