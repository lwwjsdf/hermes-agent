#!/bin/bash
# hub_client.sh — connector-hub API 客户端（多设备；设备解析见 connector-hub#49）
# 用法: hub_client.sh [--device <id>|--wechat <im.bot用户id>] health|logged|capabilities|create <title> <md>|sse <article_id> <acc_id...>|records|draft_toutiao <title> <md>|devices
# 传输模式（connector-hub#107）：显式 HUB_TRANSPORT_MODE=local|wireguard；未设置时按
# HUB_REMOTE_ENABLED 推导（1→wireguard，否则 local）。模式解析/健康握手/启动失败分类
# 统一由 hub_transport.py 完成，禁止根据网络探测结果静默切换模式。
#   · local：HUB_BASE_URL 或缺省 http://127.0.0.1:$HUB_LOCAL_PORT(15174)；Hub 未启动时
#     按 HUB_LOCAL_START_CMD 拉起并限时握手（HUB_LOCAL_HANDSHAKE_SECONDS，默认 25s）。
#     local 显式配置时直接使用该地址，旁路设备注册表——local 模式不产生 WireGuard 动作、
#     不访问公网网关、不轮询 Relay。
#   · wireguard：保持既有设备表解析 + 钉定 + pre-flight fail-closed，不恢复 Relay 轮询。
# 错误分类（#107 统一退出码）：2=hub 不可达/地址不可达，3=device_id_mismatch，
# 4=token_invalid，5=transport_mode_mismatch，6=hub_start_failed，7=无钉定，
# 8=配置冲突；副作用命令 preflight 由 hub_transport handshake 承担。
# 设备解析优先级: --device/--wechat > 环境变量 HUB_DEVICE > 设备表里 last_seen 最新的活跃设备。
# 单活语义（2026-09-25）：resolve 失败即失败（exit 3），不再回落任何硬编码地址；
# 副作用命令执行前强制 preflight（GET /health），失败按上述分类退出。
# 严格模式说明：启用 -u + pipefail；不启用 -e——case 分支与 touch_seen 等函数依赖显式
# 退出码判断，-e 会在命令替换/条件分支中误伤正常路径，失败由各调用点显式检查。
set -uo pipefail
SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --device) DEV_ARGS+=(--device "$2"); shift 2 ;;
    --wechat) DEV_ARGS+=(--wechat "$2"); shift 2 ;;
    *) break ;;
  esac
done
[ -n "${HUB_DEVICE:-}" ] && DEV_ARGS+=(--device "$HUB_DEVICE")
# 传输模式先行解析（#107）：token 变量名随模式可配（local 可用 HUB_LOCAL_TOKEN 等），
# 只取 ~/.hermes/.env 中的变量名与值，值不落日志。
MODE_OUT="$(python3 "$SCRIPTS_DIR/hub_transport.py" resolve-config 2>&1)"; MODE_RC=$?
[ $MODE_RC -ne 0 ] && { echo "$MODE_OUT" >&2; exit $MODE_RC; }
MODE="$(printf '%s\n' "$MODE_OUT" | sed -n 's/^mode=//p')"
CFG_BASE="$(printf '%s\n' "$MODE_OUT" | sed -n 's/^base_url=//p')"
TOKEN_ENV_NAME="$(printf '%s\n' "$MODE_OUT" | sed -n 's/^token_env=//p')"
TOKEN_ENV_NAME="${TOKEN_ENV_NAME:-HUB_REMOTE_TOKEN}"
TOKEN_VAL="$(grep -E "^${TOKEN_ENV_NAME}=" ~/.hermes/.env | head -1 | cut -d= -f2-)"
[ -z "$TOKEN_VAL" ] && { echo "缺 ${TOKEN_ENV_NAME}（~/.hermes/.env）"; exit 4; }
# local 显式直连：旁路设备注册表（local 不走 WireGuard/Relay）
LOCAL_EXPLICIT=0
[ "$MODE" = "local" ] && LOCAL_EXPLICIT=1
if [ "$LOCAL_EXPLICIT" = "1" ]; then
  BASE="$CFG_BASE"; DEV_ID=""
else
DEV_ID=""; RESOLVED_BASE=""
RESOLVE_ERR=""
if ! RESOLVE="$(python3 "$SCRIPTS_DIR/hub_device.py" resolve "${DEV_ARGS[@]+"${DEV_ARGS[@]}"}" 2>&1)"; then
  RESOLVE_ERR="$RESOLVE"; RESOLVE=""
  # health 探测是活性恢复入口：设备被 TTL 踢出后仍须能选中它做探测（小鸡生蛋），允许 include-stale 兜底
  [ "${1:-}" = "health" ] && RESOLVE="$(python3 "$SCRIPTS_DIR/hub_device.py" resolve --include-stale "${DEV_ARGS[@]+"${DEV_ARGS[@]}"}" 2>/dev/null || true)"
fi
if [ -n "$RESOLVE" ]; then
  DEV_ID="$(printf '%s\n' "$RESOLVE" | sed -n 's/^device_id=//p')"
  RESOLVED_BASE="$(printf '%s\n' "$RESOLVE" | sed -n 's/^endpoint=//p')"
fi
# HUB_BASE 为显式覆盖（排障用）；无解析结果且无显式覆盖 → 报错退出，禁止静默回落
BASE="${HUB_BASE:-$RESOLVED_BASE}"
if [ -z "$BASE" ]; then
  echo "hub_client: 无法确定 Hub 端点：${RESOLVE_ERR:-设备解析失败}。请注册活跃设备（hub_device.py add + health 探测），或显式设置 HUB_BASE。" >&2
  exit 3
fi
fi  # end 非 local 显式直连
touch_seen() { [ -n "$DEV_ID" ] && python3 "$SCRIPTS_DIR/hub_device.py" touch "$DEV_ID" "$@" >/dev/null 2>&1 || true; }
export NO_PROXY=10.99.0.0/24 no_proxy=10.99.0.0/24
# curl 失败显性化：--fail-with-body 让 HTTP 4xx/5xx 变非零退出码并保留响应体，
# -S 在静默模式下仍输出错误原因；所有调用点显式检查退出码，杜绝"空输出当成功"。
auth() { local PIN_HDR=(); [ -n "$EXPECTED_ID" ] && PIN_HDR=(-H "X-Connector-Hub-Device: $EXPECTED_ID")
  curl -sS --fail-with-body -m 60 --noproxy '*' -H "Authorization: Bearer ${TOKEN_VAL}" ${PIN_HDR[@]+"${PIN_HDR[@]}"} "$@"; }
# 设备钉定期望值：local 显式直连用环境钉定（HUB_EXPECT_DEVICE_ID 优先，CONNECTOR_HUB_DEVICE_ID
# 兼容）；wireguard 走既有 pin（注册表解析 + 环境覆盖 + 冲突 exit 8）。
EXPECTED_ID=""
if [ "$LOCAL_EXPLICIT" = "1" ]; then
  EXPECTED_ID="${HUB_EXPECT_DEVICE_ID:-${CONNECTOR_HUB_DEVICE_ID:-}}"
else
  PIN_OUT="$(python3 "$SCRIPTS_DIR/hub_device.py" pin "${DEV_ARGS[@]+"${DEV_ARGS[@]}"}" 2>&1)"; PIN_RC=$?
  [ $PIN_RC -ne 0 ] && { echo "$PIN_OUT" >&2; exit $PIN_RC; }
  EXPECTED_ID="$(printf '%s\n' "$PIN_OUT" | sed -n 's/^expected_device_id=//p')"
fi
# 模式统一 preflight（#107）：hub_transport.py handshake——local 自动拉起/探测，
# wireguard 保持 fail-closed；错误码透传（2/3/4/5/6）。副作用命令一律先过此闸门。
preflight() {
  local HS_START HS_TMO
  HS_START="${HUB_LOCAL_START_CMD:-}"
  HS_TMO="${HUB_LOCAL_HANDSHAKE_SECONDS:-25}"
  # handshake 的 key=value 事实行走 stderr，命令的业务 JSON 独占 stdout
  python3 "$SCRIPTS_DIR/hub_transport.py" handshake --endpoint "$BASE" --mode "$MODE" \
    --expect "$EXPECTED_ID" --start-cmd "$HS_START" --handshake-timeout "$HS_TMO" >&2
}
require_pin() { # $1=命令名（副作用）
  if [ -z "$EXPECTED_ID" ]; then
    echo "hub_client: 未配置设备钉定，拒绝执行副作用命令 $1。请先 hub_device.py add 注册设备，或设置 HUB_EXPECT_DEVICE_ID。" >&2
    exit 7
  fi
  preflight || exit $?
}
verify_if_pinned() { if [ -n "$EXPECTED_ID" ]; then preflight || exit $?; fi; }
case "$1" in
  devices) python3 "$SCRIPTS_DIR/hub_device.py" list;;
  health) OUT="$(curl -sS --fail-with-body --connect-timeout 5 -m 10 --noproxy '*' "$BASE/health")" || { touch_seen --fail; echo "hub_client: Hub 不可达（${BASE}）" >&2; exit 2; }
    BSHA="$(printf '%s' "$OUT" | python3 -c "import sys,json
try: print((json.load(sys.stdin).get('build') or {}).get('sha','') or '')
except Exception: print('')" 2>/dev/null)";
    if printf '%s' "$OUT" | grep -q '"status":"ok"'; then touch_seen --ok --build "$BSHA"; else touch_seen --fail; fi; printf '%s' "$OUT";;
  logged) verify_if_pinned; auth "$BASE/account/logged" || exit $?;;
  capabilities) verify_if_pinned; auth "$BASE/api/v1/platform-capabilities" || exit $?;;
  create) require_pin create; shift; \
    PAYLOAD=$(python3 -c 'import json,sys;print(json.dumps({"title":sys.argv[1],"content":sys.argv[2],"markdown":sys.argv[2]}))' "$1" "$2"); \
    auth -X POST "$BASE/article/create" -H "Content-Type: application/json" -d "$PAYLOAD" || exit $?;;
  create_video) require_pin create_video; shift; TITLE="$1"; MD="$2"; VPATH="$3"; \
    PAYLOAD=$(python3 -c 'import json,sys;print(json.dumps({"title":sys.argv[1],"content":sys.argv[2],"markdown":sys.argv[2],"video_path":sys.argv[3],"publish_type":"video"}))' "$TITLE" "$MD" "$VPATH"); \
    auth -X POST "$BASE/article/create" -H "Content-Type: application/json" -d "$PAYLOAD" || exit $?;;
  sse) require_pin sse; shift
    TIMEOUT=900
    [ "${1:-}" = "--timeout" ] && { TIMEOUT="$2"; shift 2; }
    AID=$1; shift; ACCS=$(python3 -c 'import json,sys;print(json.dumps([{"id":int(x)} for x in sys.argv[1:]]))' "$@")
    auth -N -m "${TIMEOUT}" -X POST "$BASE/sse/article/$AID" -H "Content-Type: application/json" -d "{\"postAccounts\":$ACCS,\"syncDraft\":true}" || exit $?;;
  records) verify_if_pinned; auth "$BASE/api/v1/records" || exit $?;;
  draft_toutiao) require_pin draft_toutiao; shift; TITLE="$1"; MD="$2"; \
    PAYLOAD=$(python3 -c 'import json,sys;print(json.dumps({"title":sys.argv[1],"content":sys.argv[2],"markdown":sys.argv[2]}))' "$TITLE" "$MD"); \
    R=$(auth -X POST "$BASE/article/create" -H "Content-Type: application/json" -d "$PAYLOAD") || { echo "hub_client: 建文请求失败（${BASE}）" >&2; exit 1; }; \
    AID=$(echo "$R" | python3 -c "import json,sys;print(json.load(sys.stdin)['data']['id'])" 2>/dev/null) || { echo "建文失败: $R" >&2; exit 1; }; \
    echo "article_id=$AID"; \
    auth -N -m 180 -X POST "$BASE/sse/article/$AID" -H "Content-Type: application/json" -d '{"postAccounts":[{"id":3}],"syncDraft":true}' || exit $?;;
  *) echo "用法: hub_client.sh [--device <id>|--wechat <id>] health|logged|capabilities|create <t> <md>|create_video <t> <md> <video_url>|sse [--timeout <sec>] <aid> <acc...>|records|draft_toutiao <t> <md>|devices";;
esac
