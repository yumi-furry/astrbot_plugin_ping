# astrbot_plugin_ping

为 AstrBot 提供网络 Ping 测速能力的插件。支持系统 ICMP、纯 Python ICMP 与 TCP 降级三级探测，内置 Docker 环境一键安装 `iputils-ping` 的指令。

## ✨ 特性

- **三级探测兜底**：系统 `ping` → 纯 Python ICMP（raw socket）→ TCP 端口探测，尽最大可能给出可达性结论。
- **Docker 友好**：即使容器内没有 `ping` 命令，只要以 root 运行或具备 `CAP_NET_RAW`，也能通过内置的纯 Python ICMP 完成探测。
- **一键安装**：`/ping setup` 自动检测 `apt-get / apk / yum / dnf` 并安装 `iputils-ping`；非 root 时返回可直接复制到宿主机执行的 `docker exec` 命令。
- **可配置**：Ping 次数、单次超时、是否允许自动安装，均可在 WebUI 中调整。
- **安全**：目标地址白名单校验、子进程无 shell 调用、输出截断，避免命令注入与超长消息。
- **无第三方依赖**：仅使用 Python 标准库（`asyncio` / `socket` / `struct` 等），无需 `requirements.txt`。

## 📦 安装

### 方式一：插件市场

在 AstrBot WebUI 的「插件」页搜索 `astrbot_plugin_ping` 并安装。

### 方式二：手动安装

将插件目录放入 `data/plugins/`：

```bash
cd AstrBot/data/plugins
git clone https://github.com/YourName/astrbot_plugin_ping