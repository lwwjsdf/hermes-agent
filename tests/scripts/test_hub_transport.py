"""hub_transport.py / hub_client.sh — connector-hub#107 客户端自动化测试（不依赖真实 Hub）

覆盖：模式解析（显式/推导/冲突）、local 启动失败分类（含自动拉起 + 延迟监听 mock）、
WireGuard fail-closed 回归、模式切换 fail-closed、hub_client.sh 错误分类映射、
token 纪律（日志不落 Token 值）、hub_device.py 单活语义（TTL 过滤 + 钉定冲突）。

被测脚本位于 scripts/connectors/（本文件按 tests/scripts/ 镜像源码树放置）。
契约来源：connector-hub PR #113（merge ede6d48）——Hub 端显式
CONNECTOR_HUB_TRANSPORT_MODE=local|wireguard，/health 返回 transport_mode。
"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONNECTORS = os.path.join(REPO, "scripts", "connectors")
TRANSPORT = os.path.join(CONNECTORS, "hub_transport.py")
CLIENT = os.path.join(CONNECTORS, "hub_client.sh")
DEVICE = os.path.join(CONNECTORS, "hub_device.py")
LAZY_MOCK = os.path.join(CONNECTORS, "lazy_mock_hub.py")

PASS, FAIL = 0, []


def check(name, cond, detail=""):
    global PASS
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def run_transport(args, env_extra=None, timeout=60):
    env = dict(os.environ)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, TRANSPORT] + args,
                          capture_output=True, text=True, env=env, timeout=timeout)


def run_device(args, env_extra=None, timeout=30):
    env = dict(os.environ)
    env.pop("HUB_DEVICES_FILE", None)
    env.update(env_extra or {})
    return subprocess.run([sys.executable, DEVICE] + args,
                          capture_output=True, text=True, env=env, timeout=timeout)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ---------- mock Hub（transport_mode 可控） ----------
class MockHub:
    def __init__(self, transport_mode=None, device_id="mock-dev-1", version="0.21.0"):
        self.transport_mode = transport_mode
        self.device_id = device_id
        self.version = version
        self.port = free_port()
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/health":
                    self.send_error(404)
                    return
                body = {"status": "ok", "device_id": outer.device_id,
                        "device_name": "MockHub", "version": outer.version}
                if outer.transport_mode is not None:
                    body["transport_mode"] = outer.transport_mode
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format=None, *args):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", self.port), H)
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def stop(self):
        self.srv.shutdown()


def parse_kv(text):
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k] = v
    return out


# ============ 1. 模式解析 ============
def test_mode_resolution():
    print("== 模式解析 ==")
    r = run_transport(["resolve-config"], {"HUB_TRANSPORT_MODE": "", "HUB_REMOTE_ENABLED": ""})
    kv = parse_kv(r.stdout)
    check("未设置→推导 local", r.returncode == 0 and kv.get("mode") == "local", r.stderr)
    check("推导标注 mode_explicit=0", kv.get("mode_explicit") == "0")

    r = run_transport(["resolve-config"], {"HUB_TRANSPORT_MODE": "wireguard",
                                           "HUB_BASE_URL": "http://10.99.0.4:15174"})
    kv = parse_kv(r.stdout)
    check("显式 wireguard", r.returncode == 0 and kv.get("mode") == "wireguard"
          and kv.get("base_url") == "http://10.99.0.4:15174")

    r = run_transport(["resolve-config"], {"HUB_TRANSPORT_MODE": "wireguard",
                                           "HUB_BASE_URL": ""})
    check("wireguard 无 HUB_BASE_URL → 拒绝(exit 8)", r.returncode == 8, r.stderr)

    r = run_transport(["resolve-config"], {"HUB_TRANSPORT_MODE": "bogus"})
    check("非法模式值 → exit 8", r.returncode == 8, r.stderr)

    r = run_transport(["resolve-config"], {"HUB_TRANSPORT_MODE": "local", "HUB_REMOTE_ENABLED": "1"})
    check("显式 local 与 REMOTE_ENABLED=1 冲突 → exit 8", r.returncode == 8, r.stderr)


# ============ 2. local 握手与启动失败分类 ============
def test_local_handshake():
    print("== local 握手 / 启动失败 ==")
    hub = MockHub(transport_mode="local", device_id="mock-dev-1")
    try:
        r = run_transport(["handshake", "--endpoint", hub.url, "--mode", "local",
                           "--expect", "mock-dev-1"])
        kv = parse_kv(r.stdout)
        check("local 握手成功", r.returncode == 0
              and kv.get("device_id") == "mock-dev-1"
              and kv.get("transport_mode") == "local", r.stdout + r.stderr)
        check("未拉起标注 hub_started_by_hermes=0", kv.get("hub_started_by_hermes") == "0")

        r = run_transport(["handshake", "--endpoint", hub.url, "--mode", "local",
                           "--expect", "other-dev"])
        check("device_id 不匹配 → exit 3", r.returncode == 3 and "不匹配" in r.stderr, r.stderr)

        hub.transport_mode = "wireguard"  # 篡改 Hub 上报模式
        r = run_transport(["handshake", "--endpoint", hub.url, "--mode", "local",
                           "--expect", "mock-dev-1"])
        check("Hub 上报 transport_mode 与配置不一致 → exit 5", r.returncode == 5, r.stderr)
    finally:
        hub.stop()

    # 老版本 Hub（无 transport_mode 字段）→ 兼容
    hub = MockHub(transport_mode=None)
    try:
        r = run_transport(["handshake", "--endpoint", hub.url, "--mode", "local",
                           "--expect", "mock-dev-1"])
        check("旧版 Hub 缺 transport_mode → 兼容放行", r.returncode == 0, r.stderr)
    finally:
        hub.stop()

    # 地址不可达（无 start_cmd）
    port = free_port()
    r = run_transport(["handshake", "--endpoint", f"http://127.0.0.1:{port}",
                       "--mode", "local", "--handshake-timeout", "3"])
    check("local 无响应无 start_cmd → address_unreachable(exit 2)", r.returncode == 2, r.stderr)

    # 启动命令执行失败 → hub_start_failed(6)
    r = run_transport(["handshake", "--endpoint", f"http://127.0.0.1:{free_port()}",
                       "--mode", "local", "--start-cmd", "/nonexistent/hub-start",
                       "--handshake-timeout", "8"])
    check("启动命令不存在 → hub_start_failed(exit 6)", r.returncode == 6, r.stderr)

    # 自动拉起后握手成功：懒启动 mock（先 sleep 再 bind，延迟监听，防"立即就绪"假路径）
    port = free_port()
    start_cmd = f"/bin/bash -c 'sleep 1.5; nohup {sys.executable} {LAZY_MOCK} {port} local local-dev-9 >/dev/null 2>&1 &'"
    r = run_transport(["handshake", "--endpoint", f"http://127.0.0.1:{port}",
                       "--mode", "local", "--start-cmd", start_cmd,
                       "--handshake-timeout", "20"])
    kv = parse_kv(r.stdout)
    check("local 自动拉起后 /health 握手成功", r.returncode == 0
          and kv.get("hub_started_by_hermes") == "1", r.stdout + r.stderr)

    # 拉起命令执行了但限时内未就绪 → hub_start_failed(6)：mock 30s 后才监听，握手限时 3s
    port2 = free_port()
    start_cmd2 = f"/bin/bash -c 'sleep 30; nohup {sys.executable} {LAZY_MOCK} {port2} local late-dev >/dev/null 2>&1 &'"
    r = run_transport(["handshake", "--endpoint", f"http://127.0.0.1:{port2}",
                       "--mode", "local", "--start-cmd", start_cmd2,
                       "--handshake-timeout", "3"])
    check("拉起后限时内未就绪 → hub_start_failed(exit 6)", r.returncode == 6, r.stderr)


# ============ 3. WireGuard 回归 ============
def test_wireguard():
    print("== wireguard fail-closed ==")
    port = free_port()  # 本机死端口，模拟隧道不可达
    r = run_transport(["handshake", "--endpoint", f"http://127.0.0.1:{port}",
                       "--mode", "wireguard", "--handshake-timeout", "4"])
    check("wg 不可达 → exit 2，且不尝试拉起", r.returncode == 2 and "fail-closed" in r.stderr,
          r.stderr)
    check("wg 失败不发启动命令（无 hub_started 输出）", "hub_started_by_hermes" not in r.stdout)

    hub = MockHub(transport_mode="wireguard", device_id="wg-dev-1")
    try:
        r = run_transport(["handshake", "--endpoint", hub.url, "--mode", "wireguard",
                           "--expect", "wg-dev-1"])
        kv = parse_kv(r.stdout)
        check("wg 握手成功", r.returncode == 0 and kv.get("mode") == "wireguard", r.stderr)
        r = run_transport(["handshake", "--endpoint", hub.url, "--mode", "wireguard",
                           "--expect", "wrong"])
        check("wg 设备不匹配 → exit 3", r.returncode == 3, r.stderr)
        hub.transport_mode = "local"
        r = run_transport(["handshake", "--endpoint", hub.url, "--mode", "wireguard",
                           "--expect", "wg-dev-1"])
        check("wg 配置 vs Hub 上报 local → exit 5", r.returncode == 5, r.stderr)
    finally:
        hub.stop()


# ============ 4. 模式切换 fail-closed ============
def test_mode_switch():
    print("== 模式切换 fail-closed ==")
    # 切换后旧 endpoint 不再被选中的机制 = resolve-config 只吃显式配置，
    # 且 hub_client.sh 每次调用重新解析（无常驻连接）。此处验证：
    # 1) local 配置下 wireguard-only 端点被拒（模式事实不一致）
    # 2) 切换配置后 resolve 立即跟随新配置，无缓存旧 endpoint
    hub_local = MockHub(transport_mode="local", device_id="dev-local")
    hub_wg = MockHub(transport_mode="wireguard", device_id="dev-wg")
    try:
        r = run_transport(["handshake", "--endpoint", hub_wg.url, "--mode", "local",
                           "--expect", "dev-local"])
        check("local 配置连 wg Hub → exit 5（旧 endpoint 不放行）", r.returncode == 5, r.stderr)
        r = run_transport(["handshake", "--endpoint", hub_local.url, "--mode", "wireguard",
                           "--expect", "dev-wg"])
        check("wg 配置连 local Hub → exit 5", r.returncode == 5, r.stderr)
        # 切换后新配置握手成功
        env = {"HUB_TRANSPORT_MODE": "local",
               "HUB_BASE_URL": hub_local.url}
        r = run_transport(["resolve-config"], env)
        kv = parse_kv(r.stdout)
        check("切换后 resolve 跟随新配置", r.returncode == 0
              and kv.get("base_url") == hub_local.url, r.stdout)
    finally:
        hub_local.stop()
        hub_wg.stop()


# ============ 5. hub_client.sh 集成面 ============
def test_client_integration():
    print("== hub_client.sh 集成 ==")
    # 干净环境跑 client：local 缺省 token 变量 HUB_LOCAL_TOKEN 缺失 → 显性失败 exit 4
    env = {"HOME": tempfile.mkdtemp(prefix="hubtest_home_"),
           "PATH": os.environ["PATH"], "HUB_TRANSPORT_MODE": "local"}
    r = subprocess.run(["bash", CLIENT, "health"], capture_output=True, text=True,
                       env=env, timeout=30)
    check("无 token 时 client 显性失败 exit 4", r.returncode == 4
          and "HUB_LOCAL_TOKEN" in (r.stdout + r.stderr), r.stdout + r.stderr)

    env2 = dict(env)
    os.makedirs(os.path.join(env2["HOME"], ".hermes"), exist_ok=True)
    with open(os.path.join(env2["HOME"], ".hermes", ".env"), "w") as f:
        f.write("HUB_LOCAL_TOKEN=" + "x" * 40 + "\n")
    # 指向一个确定无监听的死端口，验证 local 直连失败分类
    port = free_port()
    env2["HUB_BASE_URL"] = f"http://127.0.0.1:{port}"
    r = subprocess.run(["bash", CLIENT, "health"], capture_output=True, text=True,
                       env=env2, timeout=60)
    # local 直连死端口 → handshake address_unreachable exit 2
    check("local 模式 Hub 无响应 → exit 2（address_unreachable）", r.returncode == 2,
          f"rc={r.returncode} out={r.stdout[:200]} err={r.stderr[:200]}")


# ============ 6. hub_device.py 单活语义 ============
def test_device_registry():
    print("== hub_device.py 单活语义 ==")
    home = tempfile.mkdtemp(prefix="hubtest_dev_")
    reg = os.path.join(home, "hub_devices.json")
    base_env = {"HUB_DEVICES_FILE": reg}

    r = run_device(["add", "dev-a", "--endpoint", "http://127.0.0.1:1", "--name", "A"],
                   base_env)
    check("add 写入设备", r.returncode == 0, r.stderr)

    r = run_device(["resolve"], base_env)
    kv = parse_kv(r.stdout)
    check("resolve 返回活跃设备", r.returncode == 0 and kv.get("device_id") == "dev-a", r.stderr)

    # 标 failed 后被 TTL 过滤拒选
    run_device(["touch", "dev-a", "--fail"], base_env)
    r = run_device(["resolve"], base_env)
    check("health=failed → resolve 拒绝(exit 9)", r.returncode == 9, r.stderr)
    # health 探测路径允许 include-stale 恢复
    r = run_device(["resolve", "--include-stale"], base_env)
    check("include-stale 兜底可选中被踢设备", r.returncode == 0, r.stderr)
    run_device(["touch", "dev-a", "--ok"], base_env)

    # 钉定冲突
    r = run_device(["pin"], {**base_env, "HUB_EXPECT_DEVICE_ID": "dev-other"})
    check("环境钉定与注册表冲突 → exit 8", r.returncode == 8, r.stderr)
    r = run_device(["pin"], {**base_env, "HUB_EXPECT_DEVICE_ID": "dev-a"})
    kv = parse_kv(r.stdout)
    check("钉定一致 → expected_device_id", r.returncode == 0
          and kv.get("expected_device_id") == "dev-a", r.stdout + r.stderr)

    # verify 设备不匹配
    hub = MockHub(transport_mode="local", device_id="someone-else")
    try:
        r = run_device(["verify", "--endpoint", hub.url, "--expect", "dev-a"], base_env)
        check("verify 设备不匹配 → exit 3", r.returncode == 3, r.stderr)
    finally:
        hub.stop()


# ============ 7. Token 纪律 ============
def test_token_discipline():
    print("== Token 日志纪律 ==")
    for path in (TRANSPORT, CLIENT):
        with open(path) as f:
            src = f.read()
        # 不允许把 token 值打进输出：检查没有 echo/print 直接输出 token 变量值的语句
        bad = [ln for ln in src.splitlines()
               if ("print(" in ln or "echo" in ln)
               and "TOKEN_VAL" in ln.replace('grep -E', '').replace('cut -d= -f2-', '')
               and "缺" not in ln]
        check(f"{os.path.basename(path)} 不回显 Token 值", not bad, str(bad))


if __name__ == "__main__":
    test_mode_resolution()
    test_local_handshake()
    test_wireguard()
    test_mode_switch()
    test_client_integration()
    test_device_registry()
    test_token_discipline()
    print(f"\n{PASS} passed, {len(FAIL)} failed")
    if FAIL:
        print("失败项: " + "; ".join(FAIL))
        sys.exit(1)
