import asyncio
import os
import platform
import re
import shutil
import socket
import struct
import time

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register

ICMP_ECHO_REQUEST = 8
ICMP_ECHO_REPLY = 0


# ============================================================
# 纯 Python ICMP Ping 工具
# ============================================================

def _checksum(data: bytes) -> int:
    """计算 ICMP 校验和"""
    if len(data) % 2:
        data += b"\x00"
    s = sum(struct.unpack(f"!{len(data) // 2}H", data))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF


def _build_icmp_packet(identifier: int, sequence: int, payload: bytes) -> bytes:
    """构造 ICMP Echo Request 包"""
    header = struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, 0, identifier, sequence)
    cksum = _checksum(header + payload)
    header = struct.pack("!BBHHH", ICMP_ECHO_REQUEST, 0, cksum, identifier, sequence)
    return header + payload


async def _pure_python_icmp_ping(
    host: str, count: int = 4, timeout: float = 2.0, interval: float = 0.2
):
    """
    纯 Python ICMP Ping（基于 raw socket）。
    返回 dict 或 None：
      - None            : raw socket 不可用（权限不足或平台不支持）
      - dict            : 探测已执行，包含 sent / received / rtts / error
    """
    loop = asyncio.get_event_loop()

    # 1. DNS 解析（只解析 IPv4）
    try:
        infos = await loop.getaddrinfo(host, None, family=socket.AF_INET)
        if not infos:
            return {"target_ip": "", "sent": 0, "received": 0, "rtts": [],
                    "error": f"无法解析主机 {host}"}
        target_ip = infos[0][4][0]
    except socket.gaierror as e:
        return {"target_ip": "", "sent": 0, "received": 0, "rtts": [],
                "error": f"DNS 解析失败: {e}"}

    # 2. 创建 raw socket（需要 root 或 CAP_NET_RAW）
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_ICMP)
        sock.setblocking(False)
    except (PermissionError, OSError) as e:
        logger.debug(f"[ping] raw socket 不可用，降级: {e}")
        return None

    identifier = os.getpid() & 0xFFFF
    payload = b"AstrBotPing!" * 2
    rtts: list[float] = []

    try:
        for seq in range(1, count + 1):
            packet = _build_icmp_packet(identifier, seq, payload)
            t0 = time.monotonic()
            try:
                await loop.sock_sendto(sock, packet, (target_ip, 0))
            except OSError as e:
                logger.debug(f"[ping] ICMP sendto 失败: {e}")
                await asyncio.sleep(interval)
                continue

            deadline = t0 + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    data = await asyncio.wait_for(
                        loop.sock_recv(sock, 2048), timeout=remaining
                    )
                except (asyncio.TimeoutError, OSError):
                    break
                # raw socket 收到的报文包含 IP 头，需要跳过
                if len(data) < 28:
                    continue
                ip_header_len = (data[0] & 0x0F) * 4
                if len(data) < ip_header_len + 8:
                    continue
                icmp = data[ip_header_len:]
                icmp_type, icmp_code, _cksum, recv_id, recv_seq = struct.unpack(
                    "!BBHHH", icmp[:8]
                )
                if (
                    icmp_type == ICMP_ECHO_REPLY
                    and recv_id == identifier
                    and recv_seq == seq
                ):
                    rtts.append((time.monotonic() - t0) * 1000)
                    break
            await asyncio.sleep(interval)
    except Exception as e:
        logger.debug(f"[ping] Python ICMP 异常: {e}")
        return None
    finally:
        try:
            sock.close()
        except Exception:
            pass

    return {
        "target_ip": target_ip,
        "sent": count,
        "received": len(rtts),
        "rtts": rtts,
        "error": None,
    }


async def _tcp_probe(host: str, port: int, timeout: float = 3.0):
    """TCP 连接探测，成功返回 RTT 毫秒，失败返回 None"""
    t0 = time.monotonic()
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        return (time.monotonic() - t0) * 1000
    except Exception:
        return None


# ============================================================
# 插件主体
# ============================================================

@register(
    "astrbot_plugin_ping",
    "YourName",
    "网络测速插件：系统 ICMP / 纯 Python ICMP / TCP 降级，内置 Docker 安装指令",
    "1.1.0",
)
class PingPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config = config or {}
        self.ping_count = int(self.config.get("ping_count", 4))
        self.ping_timeout = float(self.config.get("ping_timeout", 2))
        self.allow_setup = bool(self.config.get("allow_setup", True))
        self._has_system_ping = shutil.which("ping") is not None

    # ---------------- 主指令 ----------------

    @filter.command("ping")
    async def ping_cmd(self, event: AstrMessageEvent, target: str = ""):
        """对指定地址执行 Ping 测速。用法：/ping <域名或IP>，或 /ping setup 自动安装 ping 命令"""
        target = (target or "").strip()

        # 无参数：输出用法与环境状态
        if not target:
            env_desc = (
                "系统自带 ping 命令"
                if self._has_system_ping
                else "系统无 ping 命令，将使用纯 Python ICMP 或 TCP 降级"
            )
            yield event.plain_result(
                "用法：\n"
                "  /ping <域名或IP>  —— 对目标执行 Ping 测速\n"
                "  /ping setup       —— 在 Docker 等环境自动安装 iputils-ping\n"
                f"\n当前环境：{env_desc}"
            )
            return

        # /ping setup：安装子命令
        if target.lower() == "setup":
            if not self.allow_setup:
                yield event.plain_result(
                    "管理员已禁用 /ping setup。请在插件配置中开启 allow_setup。"
                )
                return
            result = await self._handle_setup()
            yield event.plain_result(result)
            return

        # 安全校验
        if not re.fullmatch(r"[a-zA-Z0-9.\-:]+", target):
            yield event.plain_result(
                "地址格式不合法，仅支持字母、数字、点、连字符和冒号。"
            )
            return

        # 1. 系统 ping
        if self._has_system_ping:
            result = await self._system_ping(target)
            if result:
                yield event.plain_result(result)
                return

        # 2. 纯 Python ICMP
        py_result = await self._python_ping(target)
        if py_result:
            yield event.plain_result(py_result)
            return

        # 3. TCP 降级
        yield event.plain_result(await self._tcp_ping(target))

    # ---------------- 系统 Ping ----------------

    async def _system_ping(self, target: str) -> str | None:
        param = "-n" if platform.system().lower() == "windows" else "-c"
        cmd = ["ping", param, str(self.ping_count), target]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=20)
            if proc.returncode == 0:
                output = stdout.decode("utf-8", errors="ignore").strip()
                return f"🟢 ICMP Ping 成功 — `{target}`\n\n```\n{output[:2000]}\n```"
            else:
                err = (
                    stderr.decode("utf-8", errors="ignore")
                    + stdout.decode("utf-8", errors="ignore")
                ).strip()
                return f"🔴 ICMP Ping 失败 — `{target}`\n\n```\n{err[:2000]}\n```"
        except asyncio.TimeoutError:
            return f"⏱️ ICMP Ping 超时 — `{target}`"
        except FileNotFoundError:
            self._has_system_ping = False
            return None
        except Exception as e:
            logger.error(f"[ping] 系统 ping 异常: {e}")
            return None

    # ---------------- 纯 Python ICMP ----------------

    async def _python_ping(self, target: str) -> str | None:
        result = await _pure_python_icmp_ping(
            target, count=self.ping_count, timeout=self.ping_timeout
        )
        if result is None:
            return None  # raw socket 不可用，降级 TCP
        if result.get("error"):
            return f"🔴 Python ICMP 失败 — `{target}`\n{result['error']}"

        sent = result["sent"]
        received = result["received"]
        if received == 0:
            return None  # 全部丢失，降级 TCP 再试

        rtts = result["rtts"]
        loss_rate = (sent - received) / sent * 100
        return (
            f"🟢 Python ICMP Ping — `{target}` ({result['target_ip']})\n"
            f"发送 {sent} 个包，成功 {received} 个，丢包率 {loss_rate:.0f}%\n"
            f"RTT 最小 {min(rtts):.2f} ms / "
            f"平均 {sum(rtts) / len(rtts):.2f} ms / "
            f"最大 {max(rtts):.2f} ms\n\n"
            f"> 使用插件内置纯 Python ICMP 实现（需要 root 权限或 CAP_NET_RAW）。"
        )

    # ---------------- TCP 降级 ----------------

    async def _tcp_ping(self, target: str) -> str:
        host = target.split(":")[0]
        ports = [443, 80, 22, 53, 3389, 8443]
        results = await asyncio.gather(
            *[_tcp_probe(host, port, timeout=3.0) for port in ports]
        )
        for port, rtt in zip(ports, results):
            if rtt is not None:
                return (
                    f"🟡 TCP 降级检测 — `{target}`\n"
                    f"端口 **{port}** 连接成功，RTT ≈ {rtt:.2f} ms。\n\n"
                    f"> 当前环境无法使用系统 ping 或纯 Python ICMP"
                    f"（可能需要 root 权限或 CAP_NET_RAW）。\n"
                    f"> 如需完整 ICMP Ping，请尝试 `/ping setup` 自动安装 iputils-ping。"
                )
        return (
            f"🔴 所有检测方式均失败 — `{target}`\n"
            f"ICMP 不可用，常用端口 {ports} 均无法连接。\n"
            f"目标可能离线、屏蔽了 ICMP 与 TCP 探测，或网络不通。"
        )

    # ---------------- /ping setup 安装逻辑 ----------------

    async def _handle_setup(self) -> str:
        """检测环境并尝试自动安装 iputils-ping"""
        system = platform.system().lower()

        if system == "windows":
            return "当前为 Windows 环境，系统自带 ping，无需安装。"

        if shutil.which("ping"):
            self._has_system_ping = True
            return "系统已存在 ping 命令，无需安装。"

        # 检测包管理器
        if shutil.which("apt-get"):
            pre_cmd, install_cmd = (
                ["apt-get", "update"],
                ["apt-get", "install", "-y", "iputils-ping"],
            )
        elif shutil.which("apk"):
            pre_cmd, install_cmd = None, ["apk", "add", "--no-cache", "iputils"]
        elif shutil.which("yum"):
            pre_cmd, install_cmd = None, ["yum", "install", "-y", "iputils"]
        elif shutil.which("dnf"):
            pre_cmd, install_cmd = None, ["dnf", "install", "-y", "iputils"]
        else:
            return (
                "未检测到受支持的包管理器（apt-get / apk / yum / dnf），"
                "请手动安装 iputils-ping。"
            )

        # 权限检查
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            return (
                "当前进程非 root，无法自动安装。请在宿主机执行：\n"
                "```\n"
                "docker exec -u root -it <容器名> "
                "sh -c 'apt-get update && apt-get install -y iputils-ping'\n"
                "```"
            )

        logs: list[str] = []
        try:
            for cmd in [pre_cmd, install_cmd]:
                if not cmd:
                    continue
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                out, err = await asyncio.wait_for(proc.communicate(), timeout=180)
                snippet = (
                    out.decode("utf-8", errors="ignore")[-400:]
                    + err.decode("utf-8", errors="ignore")[-400:]
                )
                logs.append(f"$ {' '.join(cmd)}\n{snippet}")
        except asyncio.TimeoutError:
            return "安装超时，请手动在容器内执行安装命令。"
        except Exception as e:
            return f"安装失败：{e}\n请手动在容器内执行安装命令。"

        if shutil.which("ping"):
            self._has_system_ping = True
            return "✅ iputils-ping 安装成功！现在可以使用 ICMP Ping 了。"
        return (
            "❌ 安装命令已执行，但未检测到 ping 命令。请查看日志并手动确认。\n"
            + "\n".join(logs)
        )

    async def terminate(self):
        """插件卸载/停用时调用"""
        pass