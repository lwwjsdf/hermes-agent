#!/usr/bin/env python3
"""hub_device.py — Hermes 侧 Hub 设备注册表（P1：身份/设备识别；connector-hub#49）

职责：
  · 维护 HERMES_HOME/hub_devices.json：device_id ↔ 微信用户 ↔ endpoint/token
  · 解析"当前用户该用哪台设备"（派发时查一次）
  · 记录 last_seen，多设备时按最近活跃挑选

用法:
  hub_device.py list
  hub_device.py add <device_id> --endpoint http://10.99.0.4:15174 --name "Mac mini" \
                   [--token-env HUB_REMOTE_TOKEN] [--wechat <im.bot user id>] [--tenant 1]
  hub_device.py bind <device_id> --wechat <im.bot user id>      # 绑定微信身份
  hub_device.py resolve [--wechat <id>] [--device <device_id>]  # 输出 endpoint/token_env（脚本用）
  hub_device.py touch <device_id> [--ok|--fail]                 # 记录活跃/健康
  hub_device.py remove <device_id>
  hub_device.py pin [--wechat <id>] [--device <device_id>]      # 输出钉定的期望 device_id（issue#96）
  hub_device.py verify --endpoint <url> --expect <device_id>    # 调 /health 比对设备身份（issue#96）

钉定语义（issue#96）：注册表解析出的 device_id 即期望值；CONNECTOR_HUB_DEVICE_ID 用于
无注册表/强制覆盖场景；两者冲突时报错（exit 8）。

单活语义（2026-09-25 拍板）：
  · resolve/pin 的设备选择带 TTL 活性过滤：health=failed 或 last_seen 超过 10 分钟
    的设备不选用；无可用设备时报错退出（exit 9），调用方不得回落任何硬编码地址。
  · verify 退出码：exit 0 匹配；exit 3 设备不匹配（报实际 device_id/device_name）；
    exit 2 Hub 不可达或响应无法解析（fail-closed）。连接超时 5s、总超时 10s。
  · 钉定环境变量：HUB_EXPECT_DEVICE_ID 优先，CONNECTOR_HUB_DEVICE_ID 兼容兜底；
    两者同时设置且不一致 → 冲突（exit 8）。
"""
import argparse, json, os, socket, sys, time, urllib.parse, urllib.request

try:
    from hermes_constants import get_hermes_home  # repo 内运行时走 profile-aware home
    _REG_DEFAULT = str(get_hermes_home() / "hub_devices.json")
except Exception:  # pragma: no cover - 独立脚本场景（无 repo 依赖）
    _REG_DEFAULT = os.path.expanduser(
        os.environ.get("HERMES_HOME", "~/.hermes") + "/hub_devices.json")
REG = os.environ.get("HUB_DEVICES_FILE", _REG_DEFAULT)
# resolve/pin 的活性 TTL：last_seen 超过该秒数视为不活跃
LIVENESS_TTL_SECONDS = 600


def load():
    if not os.path.exists(REG):
        return {"version": 1, "devices": []}
    try:
        with open(REG) as f:
            data = json.load(f)
        data.setdefault("devices", [])
        return data
    except Exception as exc:
        print(f"hub_device: 设备表损坏({exc})：{REG}", file=sys.stderr)
        sys.exit(2)


def save(data):
    os.makedirs(os.path.dirname(REG), exist_ok=True)
    tmp = REG + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, REG)


def find(data, device_id):
    for d in data["devices"]:
        if d.get("device_id") == device_id:
            return d
    return None


def cmd_list(args):
    data = load()
    if not data["devices"]:
        print("(设备表为空)")
        return
    for d in data["devices"]:
        users = ",".join(d.get("wechat_users") or []) or "-"
        print(f"{d['device_id']}\t{d.get('name','?')}\t{d.get('endpoint','?')}\t"
              f"token={d.get('token_env','-')}\ttenant={d.get('tenant','-')}\t"
              f"微信={users}\tlast_seen={d.get('last_seen') or '-'}\thealth={d.get('health','unknown')}\tbuild={(d.get('build') or '-')[:7]}")


def cmd_add(args):
    data = load()
    d = find(data, args.device_id)
    if d is None:
        d = {"device_id": args.device_id, "created_at": time.strftime("%F %T")}
        data["devices"].append(d)
    d["name"] = args.name or d.get("name") or args.device_id
    d["endpoint"] = args.endpoint
    d["token_env"] = args.token_env
    d["tenant"] = args.tenant
    if args.wechat:
        users = d.setdefault("wechat_users", [])
        if args.wechat not in users:
            users.append(args.wechat)
        d["bound_at"] = time.strftime("%F %T")
    save(data)
    print(f"已写入设备 {d['device_id']} → {d['endpoint']} (token_env={d['token_env']}, tenant={d['tenant']})")


def cmd_bind(args):
    data = load()
    d = find(data, args.device_id)
    if d is None:
        print(f"hub_device: 未知设备 {args.device_id}", file=sys.stderr)
        sys.exit(3)
    users = d.setdefault("wechat_users", [])
    if args.wechat not in users:
        users.append(args.wechat)
    d["bound_at"] = time.strftime("%F %T")
    save(data)
    print(f"已绑定：微信 {args.wechat} ↔ 设备 {args.device_id}")


def cmd_resolve(args):
    d = resolve_device(load(), args.device, args.wechat, include_stale=args.include_stale)
    if d is None:
        print("hub_device: 无可用设备（设备表为空，或全部 health=failed / last_seen 超过 "
              f"{LIVENESS_TTL_SECONDS // 60} 分钟），已拒绝。请先确认 Hub 在线并执行 health 探测。",
              file=sys.stderr)
        sys.exit(9)
    # 脚本消费：key=value 行
    print(f"device_id={d['device_id']}")
    print(f"name={d.get('name','')}")
    print(f"endpoint={d['endpoint']}")
    print(f"token_env={d.get('token_env','')}")
    print(f"tenant={d.get('tenant','')}")


def cmd_touch(args):
    data = load()
    d = find(data, args.device_id)
    if d is None:
        print(f"hub_device: 未知设备 {args.device_id}", file=sys.stderr)
        sys.exit(3)
    d["last_seen"] = time.strftime("%F %T")
    if getattr(args, "build", ""):
        d["build"] = args.build
    if args.ok:
        d["health"] = "ok"
    if args.fail:
        d["health"] = "failed"
    d["seen_count"] = int(d.get("seen_count", 0)) + 1
    save(data)
    print(f"已记录 {args.device_id} last_seen={d['last_seen']} health={d.get('health')}")


def cmd_remove(args):
    data = load()
    before = len(data["devices"])
    data["devices"] = [d for d in data["devices"] if d.get("device_id") != args.device_id]
    if len(data["devices"]) == before:
        print(f"hub_device: 未找到 {args.device_id}", file=sys.stderr)
        sys.exit(3)
    save(data)
    print(f"已移除 {args.device_id}")


def device_liveness(d, now=None):
    """返回 (eligible, reason)。health=failed 或 last_seen 超 TTL → 不活跃；
    从未探测过（无 last_seen）视为候选，活性由 verify 的实时 /health 兜底。"""
    if d.get("health") == "failed":
        return False, "health=failed"
    seen = d.get("last_seen")
    if seen:
        try:
            stamp = time.mktime(time.strptime(seen, "%Y-%m-%d %H:%M:%S"))
        except ValueError:
            return True, ""
        if (now if now is not None else time.time()) - stamp > LIVENESS_TTL_SECONDS:
            return False, f"last_seen 超过 {LIVENESS_TTL_SECONDS // 60} 分钟（{seen}）"
    return True, ""


def resolve_device(data, device="", wechat="", include_stale=False):
    """复用 resolve/pin 的选择逻辑；返回设备 dict 或 None（无可用设备）。
    显式选择失败（未知设备/未绑定微信）直接报错退出；活性过滤见 device_liveness。
    include_stale=True 跳过 TTL 过滤，仅用于 health 探测恢复活性（小鸡生蛋场景）。"""
    devs = data["devices"]
    if not devs:
        return None
    if device:
        d = find(data, device)
        if d is None:
            print(f"hub_device: 未知设备 {device}", file=sys.stderr)
            sys.exit(3)
        ok, reason = device_liveness(d)
        if not ok and not include_stale:
            print(f"hub_device: 设备 {device} 不活跃（{reason}），已拒绝选用。", file=sys.stderr)
            sys.exit(9)
        return d
    if wechat:
        cands = [x for x in devs if wechat in (x.get("wechat_users") or [])]
        if not cands:
            print(f"hub_device: 微信 {wechat} 未绑定任何设备", file=sys.stderr)
            sys.exit(4)
    else:
        cands = devs
    alive = cands if include_stale else [x for x in cands if device_liveness(x)[0]]
    if not alive:
        return None
    return sorted(alive, key=lambda x: x.get("last_seen") or "", reverse=True)[0]


def cmd_pin(args):
    """输出本次调用钉定的期望 device_id。

    来源优先级：注册表解析出的设备 device_id 为基准期望值；
    环境变量 HUB_EXPECT_DEVICE_ID（优先）/ CONNECTOR_HUB_DEVICE_ID（兼容）用于
    无注册表时的钉定或显式覆盖；与注册表冲突或两个环境变量互相冲突 → exit 8。
    """
    env_new = os.environ.get("HUB_EXPECT_DEVICE_ID", "").strip()
    env_legacy = os.environ.get("CONNECTOR_HUB_DEVICE_ID", "").strip()
    if env_new and env_legacy and env_new != env_legacy:
        print(f"hub_device: 钉定冲突：HUB_EXPECT_DEVICE_ID={env_new} 与 "
              f"CONNECTOR_HUB_DEVICE_ID={env_legacy} 不一致，已拒绝。请只保留一个。", file=sys.stderr)
        sys.exit(8)
    env_pin = env_new or env_legacy
    d = resolve_device(load(), args.device, args.wechat)
    reg_pin = (d or {}).get("device_id", "")
    if env_pin and reg_pin and env_pin != reg_pin:
        print(f"hub_device: 钉定冲突：注册表解析设备 device_id={reg_pin}，"
              f"环境变量钉定={env_pin}，二者不一致，已拒绝。请修正其一。", file=sys.stderr)
        sys.exit(8)
    expected = reg_pin or env_pin
    print(f"expected_device_id={expected}")
    if d is not None:
        print(f"endpoint={d['endpoint']}")
        print(f"name={d.get('name','')}")


def cmd_verify(args):
    """GET {endpoint}/health 并比对 device_id。

    exit 0 匹配；exit 3 设备不匹配（给出期望与实际 device_id/device_name）；
    exit 2 Hub 不可达或返回无法解析（fail-closed）。连接超时 5s、总超时 10s。
    """
    expect = args.expect.strip()
    if not expect:
        print("hub_device: verify 需要 --expect <device_id>", file=sys.stderr)
        sys.exit(2)
    url = args.endpoint.rstrip("/") + "/health"
    try:
        parsed = urllib.parse.urlsplit(url)
        probe = socket.create_connection(
            (parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80)), timeout=5)
        probe.close()
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        actual = body.get("device_id")
        if not isinstance(actual, str) or not actual:
            raise ValueError("health 响应缺少 device_id")
    except Exception as exc:
        print(f"hub_device: Hub 不可达（{args.endpoint}，{exc}）：无法确认设备身份，"
              f"fail-closed 拒绝副作用操作（期望 device_id={expect}）。", file=sys.stderr)
        sys.exit(2)
    if actual != expect:
        name = body.get("device_name") or "?"
        print(f"hub_device: 设备不匹配：期望 device_id={expect}，"
              f"实际 device_id={actual} (device_name={name})，已拒绝执行。", file=sys.stderr)
        sys.exit(3)
    print(f"verified_device_id={actual}")


def main():
    p = argparse.ArgumentParser(description="Hermes 侧 Hub 设备注册表")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list").set_defaults(func=cmd_list)

    a = sub.add_parser("add")
    a.add_argument("device_id")
    a.add_argument("--endpoint", required=True)
    a.add_argument("--name", default="")
    a.add_argument("--token-env", default="HUB_REMOTE_TOKEN")
    a.add_argument("--tenant", default="1")
    a.add_argument("--wechat", default="")
    a.set_defaults(func=cmd_add)

    b = sub.add_parser("bind")
    b.add_argument("device_id")
    b.add_argument("--wechat", required=True)
    b.set_defaults(func=cmd_bind)

    r = sub.add_parser("resolve")
    r.add_argument("--wechat", default="")
    r.add_argument("--device", default="")
    r.add_argument("--include-stale", action="store_true",
                   help="跳过 TTL 活性过滤（仅 health 探测恢复用）")
    r.set_defaults(func=cmd_resolve)

    t = sub.add_parser("touch")
    t.add_argument("device_id")
    t.add_argument("--build", default="")
    t.add_argument("--ok", action="store_true")
    t.add_argument("--fail", action="store_true")
    t.set_defaults(func=cmd_touch)

    m = sub.add_parser("remove")
    m.add_argument("device_id")
    m.set_defaults(func=cmd_remove)

    pn = sub.add_parser("pin")
    pn.add_argument("--wechat", default="")
    pn.add_argument("--device", default="")
    pn.set_defaults(func=cmd_pin)

    v = sub.add_parser("verify")
    v.add_argument("--endpoint", required=True)
    v.add_argument("--expect", required=True)
    v.set_defaults(func=cmd_verify)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
