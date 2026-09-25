# Hub 传输模式（local | wireguard）— Hermes 侧实现与运维手册

对应 connector-hub issue #107（Hub 侧契约见 connector-hub PR #113，merge `ede6d48`，head `b0fdeaa`，v0.21.0）。自动 enrollment/配对由 #109 负责，本文不覆盖。

## 目标与背景

Hermes 与 Connector Hub 之间支持两种显式选择的通信模式：

| 模式 | 地址 | WireGuard | 公网 Relay |
| --- | --- | --- | --- |
| `local` | `127.0.0.1`（或显式局域网地址） | 不启动、不依赖 | 不访问、不轮询 |
| `wireguard` | WireGuard 可达地址（如 `http://10.99.0.4:15174`） | 仅使用 WireGuard | 不使用 |

两种模式都保留 Hub Token、`device_id` 与 `/health` 握手校验——local 不等于免鉴权。模式必须显式声明，**禁止根据网络探测结果静默切换**；Hub 上报的 `transport_mode` 与本地配置不一致时拒绝连接（fail-closed）。

## 文件布局

```
scripts/connectors/hub_transport.py   # 模式解析 + /health 握手 + local 自动拉起 + 错误分类
scripts/connectors/hub_client.sh      # API 客户端（副作用命令 preflight 硬闸门）
scripts/connectors/hub_device.py      # 设备注册表（wireguard 模式用；local 显式直连旁路）
scripts/connectors/lazy_mock_hub.py   # 测试专用延迟监听 mock Hub
tests/scripts/test_hub_transport.py   # 自动化测试（34 项，不依赖真实 Hub）
```

## 配置

写入 `~/.hermes/.env`（secrets 层；行为值有缺省，可不设）：

```bash
# —— 模式声明（三选一）——
# A. 显式 local（推荐本机同机部署）
HUB_TRANSPORT_MODE=local
# B. 显式 wireguard（跨机）
#HUB_TRANSPORT_MODE=wireguard
# C. 不设 → 按 HUB_REMOTE_ENABLED 推导：1→wireguard，否则 local（兼容旧安装）

# —— 地址 ——
# local：HUB_BASE_URL 优先；否则 http://127.0.0.1:$HUB_LOCAL_PORT（缺省 15174）
#HUB_BASE_URL=http://127.0.0.1:15174
#HUB_LOCAL_PORT=15174
# wireguard：必须显式配置
#HUB_BASE_URL=http://10.99.0.4:15174

# —— Token（按模式分变量；HUB_TOKEN_ENV 可覆盖变量名）——
# local  → HUB_LOCAL_TOKEN   = Hub 的 CONNECTOR_WEB_AUTH（管理员 Token）
# wg     → HUB_REMOTE_TOKEN  = Hub 的 CONNECTOR_HUB_REMOTE_TOKEN
HUB_LOCAL_TOKEN=<...>
#HUB_REMOTE_TOKEN=<...>

# —— local 自动拉起（可选）——
#HUB_LOCAL_START_CMD=<启动 Hub 的命令>
#HUB_LOCAL_HANDSHAKE_SECONDS=25

# —— 设备钉定（可选但强烈建议；副作用命令需要）——
#HUB_EXPECT_DEVICE_ID=<device_id>   # 兼容 CONNECTOR_HUB_DEVICE_ID
```

注意：`HUB_TRANSPORT_MODE=local` 与 `HUB_REMOTE_ENABLED=1` 同时存在 → exit 8 拒绝（fail-closed）。

## 错误分类（统一退出码）

| exit | 含义 | 典型场景 |
| --- | --- | --- |
| 2 | address_unreachable | Hub 无响应/超时；wg 隧道断 |
| 3 | device_id_mismatch | /health 的 device_id ≠ 钉定期望 |
| 4 | token_invalid | Token 变量缺失或 /health 401 |
| 5 | transport_mode_mismatch | Hub 上报模式 ≠ 本地配置 |
| 6 | hub_start_failed | local 拉起命令失败或限时内未就绪 |
| 7 | 无钉定 | 副作用命令未配置 HUB_EXPECT_DEVICE_ID 等 |
| 8 | 配置冲突 | 模式值非法 / 显式值与推导源矛盾 / 钉定冲突 |

## local 模式行为

1. `resolve-config`：base_url = `HUB_BASE_URL` 或 `http://127.0.0.1:$HUB_LOCAL_PORT`；旁路设备注册表（不查 `hub_devices.json`）。
2. 握手：先探 `/health`；已在线且模式/device_id 校验过 → 直接可用（`hub_started_by_hermes=0`）。
3. 未在线且有 `HUB_LOCAL_START_CMD`：拉起一次（`start_new_session`，输出丢弃），`HUB_LOCAL_HANDSHAKE_SECONDS`（缺省 25s）内轮询 `/health`；超时 → exit 6。
4. 未在线且无启动命令：exit 2。
5. 全程零 `wg` 动作、零 Relay 访问。

## wireguard 模式行为（不回归）

设备表解析（TTL 活性过滤）→ 钉定（`hub_device.py pin`）→ preflight 握手。不可达直接 exit 2，**不做任何启动动作**。与 local 的互斥由握手期 `transport_mode` 事实比对强制：local 配置连 wg Hub（或反向）→ exit 5。

## 日常操作

```bash
# 探活（local，自动拉起生效时兼作启动入口）
bash scripts/connectors/hub_client.sh health

# 账号/能力查询（带 Token 与 preflight）
bash scripts/connectors/hub_client.sh logged
bash scripts/connectors/hub_client.sh capabilities

# 模式切换：改 ~/.hermes/.env 的 HUB_TRANSPORT_MODE/HUB_BASE_URL 后直接调用即可；
# 每次调用重新解析配置并握手校验模式事实，无常驻连接残留。

# 测试
python3 tests/scripts/test_hub_transport.py
```

## 变更记录

- 2026-09-25：初版。local/wireguard 显式模式、local 自动拉起、模式切换 fail-closed、34 项自动化测试（connector-hub#107 Hermes 侧）。
