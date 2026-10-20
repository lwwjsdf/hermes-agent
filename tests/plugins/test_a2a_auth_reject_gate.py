"""Q-0126 闸门③ (t_702894bd): auth-reject observability — 401/403 must land in
~/.hermes/audit/a2a_auth_rejects.jsonl (with port + token fingerprint, never the
token itself) and in the /metrics counters. The 10-18 forced-bearer rollout left
this path dark for 16h+ (ledger Q-0126)."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import urllib.error
import urllib.request

import pytest

from plugins.platforms import a2a
from plugins.platforms.a2a import protocol, security
from tests.plugins.test_a2a_plugin import (
    _free_port,
    _make_live_adapter,
    _post_json,
    _send_body,
)


def _read_audit(home) -> list[dict]:
    path = home / "audit" / "a2a_auth_rejects.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _post_raw(url: str, body: dict, headers: dict | None = None):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


class TestAuthRejectAudit:
    def test_401_bad_token_audited_with_port_and_fingerprint(self, monkeypatch, tmp_path):
        """A wrong bearer must 401 AND produce an audit row carrying the bind port
        and a fingerprint of the presented token — never the credential itself."""
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("A2A_BEARER_TOKEN", "real-secret")
        adapter, base = _make_live_adapter(monkeypatch)

        async def run():
            assert await adapter.connect() is True
            status, body = await asyncio.to_thread(
                _post_raw, base + "/", _send_body("x"), {"Authorization": "Bearer wrong-token"})
            assert status == 401
            assert body["error"]["code"] == protocol.ERR_UNAUTHORIZED
            await adapter.disconnect()

        asyncio.run(run())

        rows = _read_audit(tmp_path)
        assert len(rows) == 1
        row = rows[0]
        assert str(row["status"]) == "401"
        assert row["port"] == adapter.port
        assert row["token"] == "fp:" + hashlib.sha256(b"wrong-token").hexdigest()[:12]
        assert "wrong-token" not in json.dumps(rows)
        assert "real-secret" not in json.dumps(rows)
        assert protocol.metrics.unauthorized_total >= 1

    def test_401_missing_header_audited_as_none(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("A2A_PEER_TOKENS", "alice:tok1")
        adapter, base = _make_live_adapter(monkeypatch)

        async def run():
            assert await adapter.connect() is True
            status, _ = await asyncio.to_thread(_post_raw, base + "/", _send_body("x"))
            assert status == 401
            await adapter.disconnect()

        asyncio.run(run())

        rows = _read_audit(tmp_path)
        assert str(rows[0]["status"]) == "401"
        assert rows[0]["token"] == "none"
        assert rows[0]["client_ip"] == "127.0.0.1"

    def test_success_path_writes_no_auth_reject_row(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setenv("A2A_BEARER_TOKEN", "real-secret")
        adapter, base = _make_live_adapter(monkeypatch)

        async def run():
            assert await adapter.connect() is True
            headers = {"Authorization": "Bearer real-secret"}

            async def authorized():
                _post_json(base + "/", _send_body("hi"), headers)  # noqa: F841

            await asyncio.to_thread(_post_json, base + "/", _send_body("hi"), headers)
            await adapter.disconnect()

        asyncio.run(run())
        assert _read_audit(tmp_path) == []

    def test_metrics_snapshot_exposes_auth_counters(self, monkeypatch):
        monkeypatch.setenv("A2A_BEARER_TOKEN", "")
        snapshot = protocol.metrics.snapshot()
        assert "unauthorized_total" in snapshot
        assert "untrusted_peer_total" in snapshot


class TestTokenFingerprint:
    def test_shapes(self):
        assert security._token_fingerprint(None) == "none"
        assert security._token_fingerprint("") == "none"
        assert security._token_fingerprint("Basic dXNlcjpwYXNz") == "malformed"
        assert security._token_fingerprint("Bearer") == "malformed"
        assert security._token_fingerprint("Bearer   ") == "malformed"
        fp = security._token_fingerprint("Bearer sekrit")
        assert fp == "fp:" + hashlib.sha256(b"sekrit").hexdigest()[:12]
        assert "sekrit" not in fp

    def test_fingerprint_stable_and_distinguishing(self):
        a = security._token_fingerprint("Bearer token-a")
        b = security._token_fingerprint("Bearer token-b")
        assert a != b
        assert a == security._token_fingerprint("Bearer token-a")


class TestAuthRejectAuditFile:
    def test_never_raises_on_unwritable_home(self, monkeypatch):
        # audit must degrade silently (debug log), never break the request path
        monkeypatch.setenv("HERMES_HOME", "/proc/definitely/not/writable")
        security.audit_auth_reject("401", 9901, "/", "127.0.0.1", "Bearer x")
