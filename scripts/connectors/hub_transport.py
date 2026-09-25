#!/usr/bin/env python3
"""hub_transport.py — Hermes 侧 Hub 连接的传输模式解析与健康握手（connector-hub#107）

契约（connector-hub PR #113，提交 b0fdeaa）：
  · 模式必须显式声明，禁止根据网络探测结果静默切换：
      HUB_TRANSPORT_MODE=local|wireguard（显式）
      未设置时兼容推导：HUB_REMOTE_ENABLED=1 → wireguard，否则 local
      显式值与推导来源冲突 → exit 8（fail-closed，不猜）
  · GET /health 返回 transport_mode / device_id / device_name / version；
    响应 transport_mode 与本地配置模式不一致 → exit 5（mismatch，不静默切）
  · local 模式：base_url 用 127.0.0.1 或显式局域网地址；Hub 未启动时按
    HUB_LOCAL_START_CMD 自动拉起，HUB_LOCAL_HANDSHAKE_SECONDS（默认 25s）内
    完成 /health 握手；失败按 exit code 分类：
      2 = address_unreachable（Hub 无响应/超时）
      3 = device_id_mismatch
      4 = token_invalid（本地校验：local 模式必须配置非空 HUB_REMOTE_TOKEN）
      5 = transport_mode_mismatch
      6 = hub_start_failed（自动拉起命令执行失败）
      8 = 配置冲突（模式显式值互相矛盾）
  · wireguard 模式：保持既有 pre-flight（hub_device.py verify）与 fail-closed；
    本模块不发起 WireGuard 连接动作，不恢复 Relay 轮询。

日志/输出纪律：不打印 Token 值，只打印变量名；设备身份只打 device_id/device_name。

用法（供 hub_client.sh 消费，key=value 行输出）：
  hub_transport.py resolve-config     # 输出 mode=... base_url=... token_env=...
  hub_transport.py handshake --endpoint URL --mode local|wireguard \
      [--expect DEVICE_ID] [--start-cmd CMD] [--handshake-timeout SEC]
"""
import json
import os
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

EXIT_ADDRESS_UNREACHABLE = 2
EXIT_DEVICE_MISMATCH = 3
EXIT_TOKEN_INVALID = 4
EXIT_MODE_MISMATCH = 5
EXIT_HUB_START_FAILED = 6
EXIT_CONFIG_CONFLICT = 8

MODE_ENV = "HUB_TRANSPORT_MODE"
LEGACY_ENV = "HUB_REMOTE_ENABLED"


def cfg_fail(msg, code=EXIT_CONFIG_CONFLICT):
    print(f"hub_transport: {msg}", file=sys.stderr)
    sys.exit(code)


def resolve_mode(env=None):
    """显式 HUB_TRANSPORT_MODE 优先；未设置时按 HUB_REMOTE_ENABLED 推导。
    返回 (mode, explicit)；显式值非法 → exit 8。"""
    env = os.environ if env is None else env
    explicit = (env.get(MODE_ENV) or "").strip().lower()
    if explicit:
        if explicit not in ("local", "wireguard"):
            cfg_fail(f"{MODE_ENV}={explicit} 非法（只允许 local|wireguard）")
        return explicit, True
    legacy = (env.get(LEGACY_ENV) or "").strip()
    return ("wireguard" if legacy == "1" else "local"), False


def resolve_base_url(mode, env=None):
    """local → 127.0.0.1 或显式局域网地址；wireguard → 既有 WireGuard 地址。
    解析不出地址 → exit 8（配置缺失，不猜）。"""
    env = os.environ if env is None else env
    url = (env.get("HUB_BASE_URL") or "").strip()
    if url:
        return url.rstrip("/")
    if mode == "local":
        port = (env.get("HUB_LOCAL_PORT") or "15174").strip()
        return f"http://127.0.0.1:{port}"
    cfg_fail("wireguard 模式必须显式配置 HUB_BASE_URL（WireGuard 地址，如 http://10.99.0.4:15174）")


def resolve_token_env(mode, env=None):
    """token 变量名（不取值、不打值）。local 缺省 HUB_LOCAL_TOKEN（Hub 管理员 Token），
    wireguard 缺省 HUB_REMOTE_TOKEN（远程 Token）；HUB_TOKEN_ENV 显式覆盖两者。"""
    env = os.environ if env is None else env
    name = (env.get("HUB_TOKEN_ENV") or "").strip()
    if name:
        return name
    return "HUB_LOCAL_TOKEN" if mode == "local" else "HUB_REMOTE_TOKEN"


def cmd_resolve_config(args):
    env = os.environ
    mode, explicit = resolve_mode(env)
    base = resolve_base_url(mode, env)
    token_env = resolve_token_env(mode, env)
    if explicit and mode == "local" and (env.get(LEGACY_ENV) or "").strip() == "1":
        cfg_fail(f"冲突：{MODE_ENV}=local 与 {LEGACY_ENV}=1 矛盾，拒绝启动式解析（fail-closed）")
    print(f"mode={mode}")
    print(f"mode_explicit={'1' if explicit else '0'}")
    print(f"base_url={base}")
    print(f"token_env={token_env}")


def _tcp_probe(host, port, timeout):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _health_once(endpoint, timeout):
    """一次 GET /health。返回 (payload, None) 或 (None, err_kind)；
    err_kind ∈ unreachable|http_401|http_other。"""
    url = endpoint.rstrip("/") + "/health"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return None, "http_401"
        return None, "http_other"
    except Exception:
        return None, "unreachable"


def classify_health(body, mode, expect):
    """比对 /health 与本地配置。返回 None 或 (exit_code, 原因)。"""
    actual_mode = body.get("transport_mode")
    if actual_mode is not None and actual_mode != mode:
        return EXIT_MODE_MISMATCH, (
            f"Hub /health transport_mode={actual_mode} 与本地配置 mode={mode} 不一致，"
            "拒绝连接（不静默切换）")
    actual = body.get("device_id")
    if expect:
        if not isinstance(actual, str) or not actual:
            return EXIT_ADDRESS_UNREACHABLE, "health 响应缺少 device_id，无法确认设备身份（fail-closed）"
        if actual != expect:
            return EXIT_DEVICE_MISMATCH, (
                f"device_id 不匹配：期望 {expect}，实际 {actual} "
                f"(device_name={body.get('device_name') or '?'})")
    return None


def handshake(endpoint, mode, expect, start_cmd, timeout_s):
    """local/wireguard 统一握手。成功打印 key=value 并 exit 0；失败按类退出。
    local：未启动时用 start_cmd 拉起一次，限时轮询 /health；
    wireguard：不做启动动作，直接探测（不可达即 fail-closed）。"""
    deadline = time.time() + max(2, timeout_s)
    started = False
    last_body = None
    while True:
        body, err = _health_once(endpoint, timeout=5)
        if body is not None:
            verdict = classify_health(body, mode, expect)
            if verdict is None:
                print(f"mode={mode}")
                print(f"endpoint={endpoint}")
                print(f"device_id={body.get('device_id', '')}")
                print(f"device_name={body.get('device_name', '')}")
                print(f"transport_mode={body.get('transport_mode', '')}")
                print(f"version={(body.get('version') or (body.get('build') or {}).get('version', ''))}")
                print(f"hub_started_by_hermes={'1' if started else '0'}")
                return
            cfg_fail(verdict[1], verdict[0])
        if err == "http_401":
            cfg_fail("/health 返回 401：Token 缺失或无效，拒绝继续", EXIT_TOKEN_INVALID)
        if mode == "wireguard":
            cfg_fail(f"wireguard 模式：Hub 不可达（{endpoint}），fail-closed 拒绝副作用操作",
                     EXIT_ADDRESS_UNREACHABLE)
        # local：未启动 → 拉起一次；已拉起 → 轮询到限时
        if not started and start_cmd:
            print(f"hub_transport: Hub 无响应，按 HUB_LOCAL_START_CMD 拉起…", file=sys.stderr)
            try:
                subprocess.Popen(shlex.split(start_cmd),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                 start_new_session=True)
            except Exception as exc:
                cfg_fail(f"Hub 启动命令执行失败：{exc}", EXIT_HUB_START_FAILED)
            started = True
            # 给进程一点监听时间再继续轮询
            time.sleep(1.0)
            continue
        if time.time() >= deadline:
            if not started and start_cmd is None:
                cfg_fail(f"local 模式：Hub 不可达（{endpoint}），且未配置 HUB_LOCAL_START_CMD，"
                         "无法自动拉起", EXIT_ADDRESS_UNREACHABLE)
            if started:
                cfg_fail(f"local 模式：拉起命令已执行但 {timeout_s}s 内 /health 未就绪"
                         f"（最后错误：{err}）", EXIT_HUB_START_FAILED)
            cfg_fail(f"local 模式：Hub 不可达（{endpoint}，{err}）", EXIT_ADDRESS_UNREACHABLE)
        time.sleep(0.5)


def main():
    import argparse
    p = argparse.ArgumentParser(description="Hub 传输模式解析与健康握手（#107）")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("resolve-config").set_defaults(func=cmd_resolve_config)
    h = sub.add_parser("handshake")
    h.add_argument("--endpoint", required=True)
    h.add_argument("--mode", required=True, choices=["local", "wireguard"])
    h.add_argument("--expect", default="")
    h.add_argument("--start-cmd", default="")
    h.add_argument("--handshake-timeout", type=int, default=25)
    h.set_defaults(func=None)
    args = p.parse_args()
    if args.cmd == "handshake":
        handshake(args.endpoint, args.mode, args.expect.strip(),
                  args.start_cmd.strip(), args.handshake_timeout)
    else:
        args.func(args)


if __name__ == "__main__":
    main()
