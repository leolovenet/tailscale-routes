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
