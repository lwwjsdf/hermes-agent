"""Behavior contracts for family-ops#127 inbound A2A retry idempotency."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

from gateway.config import PlatformConfig
from plugins.platforms.a2a import protocol
from plugins.platforms.a2a.adapter import A2AAdapter


def _params(message_id: str | None, context_id: str = "ctx-1", text: str = "decide once") -> dict:
    message = {"role": "user", "parts": [{"text": text}], "contextId": context_id}
    if message_id is not None:
        message["messageId"] = message_id
    return {"message": message}


def _forwarding_adapter(monkeypatch, extra: dict | None = None):
    cfg = {"agents": {"dev": {"profile": "dev", "tenant": "dev"}}, **(extra or {})}
    adapter = A2AAdapter(PlatformConfig(enabled=True, extra=cfg))
    calls = []

    def forward(_agent, _peer, _context, framed):
        calls.append(framed)
        return "one decision", protocol.STATE_COMPLETED

    monkeypatch.setattr(adapter, "_forward_to_profile", forward)
    return adapter, adapter._agents["dev"], calls


def test_idempotency_three_states_keys_restart_window_race_and_audit(tmp_path, monkeypatch):
    """M1/M2/M3 kill points: no lookup, inverted window, or stripped prefix breaks this contract."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter, agent, calls = _forwarding_adapter(monkeypatch)

    # Terminal duplicate: original identity/context/reply, one dispatch, and explicit audit evidence.
    first, _ = adapter._prepare_task(_params("m-1"), "peer-a", agent)
    duplicate, _ = adapter._prepare_task(_params("m-1"), "peer-a", agent)
    assert duplicate["id"] == first["id"] and duplicate["contextId"] == "ctx-1"
    assert protocol.extract_text(duplicate["status"]["message"]).startswith("[duplicate-receipt]")
    assert len(calls) == 1 and adapter.tasks.list(with_total=True)[2] == 1
    audits = [json.loads(line) for line in (tmp_path / "a2a_audit.jsonl").read_text().splitlines()]
    assert audits[-1]["direction"] == "inbound_dedup" and first["id"] in audits[-1]["summary"]

    # Changed and absent messageId both converge by canonical content; another context is not a hit.
    changed, _ = adapter._prepare_task(_params("m-2"), "peer-a", agent)
    absent, _ = adapter._prepare_task(_params(None), "peer-a", agent)
    other_context, _ = adapter._prepare_task(_params("m-1", "ctx-2"), "peer-a", agent)
    assert changed["id"] == absent["id"] == first["id"]
    assert other_context["id"] != first["id"] and len(calls) == 2

    no_context = _params("context-created", text="new context retry")
    no_context["message"].pop("contextId")
    generated, _ = adapter._prepare_task(no_context, "peer-a", agent)
    generated_retry, _ = adapter._prepare_task(no_context, "peer-a", agent)
    assert generated_retry["id"] == generated["id"] and generated_retry["contextId"] == generated["contextId"]

    # A fresh Python process (empty TaskStore) replays the persisted terminal receipt. A miss would
    # attempt the real forwarded-profile subprocess and make this probe fail instead of printing JSON.
    probe = """
import json
from gateway.config import PlatformConfig
from plugins.platforms.a2a.adapter import A2AAdapter
adapter = A2AAdapter(PlatformConfig(enabled=True, extra={"agents":{"dev":{"profile":"dev","tenant":"dev"}}}))
task, pending = adapter._prepare_task({"message":{"role":"user","parts":[{"text":"decide once"}],"contextId":"ctx-1","messageId":"m-1"}}, "peer-a", adapter._agents["dev"])
print(json.dumps({"task": task, "pending": pending, "count": adapter.tasks.list(with_total=True)[2]}))
"""
    restarted = subprocess.run(
        [sys.executable, "-c", probe], cwd=os.getcwd(), env=os.environ.copy(),
        text=True, capture_output=True, timeout=10, check=True,
    )
    payload = json.loads(restarted.stdout)
    replay = payload["task"]
    assert replay["id"] == first["id"] and replay["status"]["state"] == protocol.STATE_COMPLETED
    assert protocol.extract_text(replay["status"]["message"]).startswith("[duplicate-receipt]")
    assert payload["pending"] is None and payload["count"] == 0

    # Working duplicates return the accepted task and never dispatch a second session.
    local = A2AAdapter(PlatformConfig(enabled=True, extra={}))
    local._loop, local._message_handler = SimpleNamespace(), object()
    monkeypatch.setattr("plugins.platforms.a2a.adapter.asyncio.run_coroutine_threadsafe", lambda *_: None)
    terminal, pending = local._prepare_task(_params("working", "ctx-work", "work"), "peer-b")
    working_dup, duplicate_pending = local._prepare_task(_params("working", "ctx-work", "work"), "peer-b")
    assert terminal is None and pending is not None and duplicate_pending is None
    assert working_dup["id"] == pending["task_id"] and working_dup["status"]["state"] == protocol.STATE_WORKING
    assert protocol.extract_text(working_dup["status"]["message"]).startswith("[duplicate-receipt]")

    # Configurable short window: inside hits, outside creates and executes normally.
    now = [1000.0]
    monkeypatch.setattr("plugins.platforms.a2a.dedup.time.time", lambda: now[0])
    short, short_agent, short_calls = _forwarding_adapter(monkeypatch, {"dedup_window_seconds": 2})
    before, _ = short._prepare_task(_params("short", "ctx-short", "short"), "peer-c", short_agent)
    now[0] += 1
    inside, _ = short._prepare_task(_params("short", "ctx-short", "short"), "peer-c", short_agent)
    now[0] += 2
    outside, _ = short._prepare_task(_params("short", "ctx-short", "short"), "peer-c", short_agent)
    assert inside["id"] == before["id"] and outside["id"] != before["id"] and len(short_calls) == 2

    # Concurrent retries are serialized around claim+task creation; only one forward executes.
    race, race_agent, race_calls = _forwarding_adapter(monkeypatch)
    original_forward = race._forward_to_profile

    def slow_forward(*args):
        time.sleep(0.05)
        return original_forward(*args)

    monkeypatch.setattr(race, "_forward_to_profile", slow_forward)
    barrier = threading.Barrier(3)
    results = []

    def submit():
        barrier.wait()
        results.append(race._prepare_task(_params("race", "ctx-race", "race"), "peer-d", race_agent)[0])

    threads = [threading.Thread(target=submit) for _ in range(2)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert len({task["id"] for task in results}) == 1 and len(race_calls) == 1


def test_send_and_stream_share_prepare_task_dedup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapter, agent, calls = _forwarding_adapter(monkeypatch)
    sent = adapter._rpc_message_send(1, _params("dual", "ctx-dual"), "peer", agent=agent)

    chunks = []
    handler = SimpleNamespace(
        wfile=SimpleNamespace(write=lambda body: chunks.append(body.decode()), flush=lambda: None),
        send_response=lambda _code: None,
        send_header=lambda *_args: None,
        end_headers=lambda: None,
        close_connection=False,
    )
    adapter._rpc_message_stream(handler, 2, _params("dual", "ctx-dual"), "peer", agent=agent)
    stream = "".join(chunks)
    assert len(calls) == 1
    assert sent["result"]["id"] in stream and "[duplicate-receipt]" in stream
