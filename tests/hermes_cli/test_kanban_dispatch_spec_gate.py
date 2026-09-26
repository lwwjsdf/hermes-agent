"""kanban.spec_gate: the spec-driven READY dispatch gate (t_0ce3783c / t_20c2566e).

A NEW spec-driven protocol card (title/body references the protocol, created
after the LEGACY cutoff) that still misses assignee / substantial body /
acceptance criteria must not be dispatched by the machine either — the human
validators could only warn after the fact. The card stays READY with a
``spec_gate_blocked`` audit event; the operator completes the fields and the
next tick dispatches it. LEGACY cards (pre-cutoff) and plain cards are never
touched, and with the gate disabled behaviour is exactly upstream.

The gate lives inside :func:`kanban_db_dispatch.dispatch_once`, which BOTH the
CLI (``hermes kanban dispatch``) and the gateway watcher
(``gateway/kanban_watchers_dispatcher.py``) call — one gate, every dispatch path.
"""
from __future__ import annotations

import inspect
import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd

SPEC_BODY = (
    "spec-driven card per family-ops/docs/multi-profile-spec-driven-workflow.md\n"
    "spec_version: 1.0\n"
)
ACCEPTANCE = "\n验收：\n- A1 门禁拦截有审计事件\n"
FULL_BODY = SPEC_BODY + ACCEPTANCE


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME with an empty kanban DB."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


@pytest.fixture
def gate_on(kanban_home):
    """Opt the home in via kanban.spec_gate: true."""
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  spec_gate: true\n", encoding="utf-8",
    )


@pytest.fixture
def spawnable(all_assignees_spawnable):
    """Every assignee maps to a real profile (see hermes_cli/conftest.py)."""
    return all_assignees_spawnable


def _new_spec_card(conn: sqlite3.Connection, *, assignee=None, body=SPEC_BODY,
                   title="spec-driven feature card") -> str:
    created_at = kbd.SPEC_GATE_LEGACY_CUTOFF + 10
    tid = kb.create_task(
        conn, title=title, body=body, assignee=assignee,
        workspace_kind="scratch",
    )
    conn.execute("UPDATE tasks SET created_at = ? WHERE id = ?", (created_at, tid))
    conn.commit()
    return tid


def _legacy_card(conn: sqlite3.Connection, *, assignee=None) -> str:
    tid = kb.create_task(
        conn, title="spec-driven feature card", body="no acceptance here",
        assignee=assignee, workspace_kind="scratch",
    )
    conn.execute(
        "UPDATE tasks SET created_at = ? WHERE id = ?",
        (kbd.SPEC_GATE_LEGACY_CUTOFF - 10, tid),
    )
    conn.commit()
    return tid


# --- gate disabled / out-of-scope cards -------------------------------------


def test_gate_disabled_dispatches_incomplete_spec_card(kanban_home, spawnable):
    """kanban.spec_gate unset (default False): behaviour is exactly upstream."""
    with kbc.connect() as conn:
        tid = _new_spec_card(conn, assignee="alice")
        res = kbd.dispatch_once(conn, dry_run=True)
    assert [t for t, _a, _w in res.spawned] == [tid]
    assert res.skipped_spec_gate == []


def test_plain_card_untouched_even_with_gate_on(kanban_home, gate_on, spawnable):
    """A card without any protocol reference is not spec-driven — never gated."""
    with kbc.connect() as conn:
        tid = kb.create_task(
            conn, title="fix flaky test", body="short body, no protocol mention",
            assignee="alice", workspace_kind="scratch",
        )
        res = kbd.dispatch_once(conn, dry_run=True)
    assert [t for t, _a, _w in res.spawned] == [tid]
    assert res.skipped_spec_gate == []


def test_legacy_card_dispatched_despite_missing_fields(kanban_home, gate_on, spawnable):
    """Pre-cutoff history is never machine-blocked, however incomplete."""
    with kbc.connect() as conn:
        tid = _legacy_card(conn, assignee="alice")
        res = kbd.dispatch_once(conn, dry_run=True)
    assert [t for t, _a, _w in res.spawned] == [tid]
    assert res.skipped_spec_gate == []


# --- gate on: incomplete NEW spec-driven card withheld ----------------------


@pytest.mark.parametrize("missing", ["assignee", "body", "acceptance"])
def test_incomplete_new_spec_card_withheld_and_kept_ready(
    kanban_home, gate_on, spawnable, missing,
):
    """Each missing field alone withholds the card; it stays READY with an
    audit event and the card row is otherwise untouched."""
    kwargs: dict = {"assignee": "alice"}
    if missing == "assignee":
        # Isolate the assignee gap: body is otherwise complete.
        kwargs = {"assignee": None, "body": FULL_BODY}
    elif missing == "body":
        # Isolate the body gap: acceptance stays in the (short) body.
        kwargs["body"] = "spec-driven\n验收: A1\n   "
    with kbc.connect() as conn:
        tid = _new_spec_card(conn, **kwargs)
        res = kbd.dispatch_once(conn, dry_run=False,
                                spawn_fn=lambda _t, _w: 4242)
        task = kb.get_task(conn, tid)
        assert task is not None
        kinds = [e.kind for e in kb.list_events(conn, tid)]
    assert res.spawned == []
    assert res.skipped_spec_gate == [(tid, f"spec_gate:{missing}")]
    assert task.status == "ready"
    assert "spec_gate_blocked" in kinds


def test_withheld_card_dispatched_once_completed(kanban_home, gate_on, spawnable):
    """The operator completes the fields; the NEXT tick dispatches normally."""
    with kbc.connect() as conn:
        tid = _new_spec_card(conn, assignee="alice")
        res1 = kbd.dispatch_once(conn, dry_run=True)
        assert res1.skipped_spec_gate
        conn.execute(
            "UPDATE tasks SET body = ? WHERE id = ?", (FULL_BODY, tid),
        )
        conn.commit()
        res2 = kbd.dispatch_once(conn, dry_run=True)
    assert res1.spawned == []
    assert res2.skipped_spec_gate == []
    assert [t for t, _a, _w in res2.spawned] == [tid]


def test_complete_new_spec_card_dispatches(kanban_home, gate_on, spawnable):
    """Assignee + substantial body + acceptance criteria = dispatch normally."""
    with kbc.connect() as conn:
        tid = _new_spec_card(conn, assignee="alice", body=FULL_BODY)
        res = kbd.dispatch_once(conn, dry_run=True)
    assert [t for t, _a, _w in res.spawned] == [tid]
    assert res.skipped_spec_gate == []


# --- config plumbing ---------------------------------------------------------


def test_spec_gate_default_off_and_registered_in_defaults():
    """The knob exists in DEFAULT_CONFIG (registry rule) and defaults to False."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["kanban"]["spec_gate"] is False
    assert kbd.spec_gate_enabled({"spec_gate": True}) is True
    assert kbd.spec_gate_enabled({"spec_gate": False}) is False
    assert kbd.spec_gate_enabled({}) is False


def test_spec_gate_config_read_failure_fails_open_to_disabled(kanban_home, monkeypatch):
    """A broken config read must not start gating a home that never opted in."""
    import hermes_cli.config as config_mod

    def raise_read_error():
        raise OSError("config unavailable")

    monkeypatch.setattr(config_mod, "load_config", raise_read_error)
    assert kbd.spec_gate_enabled() is False


# --- shared dispatch path: CLI and gateway watcher ---------------------------


def test_gateway_watcher_path_goes_through_the_same_gate(kanban_home, gate_on, spawnable):
    """The gateway dispatcher helper calls the SAME dispatch_once, so the gate
    holds there too — this is the whole point of wiring it in the core tick."""
    import gateway.kanban_watchers_dispatcher as wd

    source = inspect.getsource(wd._KanbanDispatcher.tick_once_for_board)
    assert "dispatch_once" in source
    with kbc.connect() as conn:
        tid = _new_spec_card(conn, assignee="alice")
        res = kbd.dispatch_once(conn, dry_run=True)
    assert res.skipped_spec_gate == [(tid, "spec_gate:acceptance")]
    assert res.spawned == []


def test_cli_dispatch_reports_spec_gate_withholdings(
    kanban_home, gate_on, spawnable, capsys,
):
    """`hermes kanban dispatch` surfaces withheld cards in text and JSON."""
    import argparse

    from hermes_cli import kanban_ops

    with kbc.connect() as conn:
        tid = _new_spec_card(conn, assignee="alice")
    args = argparse.Namespace(dry_run=True, json=False, max=None, failure_limit=2)
    assert kanban_ops._cmd_dispatch(args) == 0
    assert f"Spec-gate withheld (kept READY): {tid}" in capsys.readouterr().out

    args = argparse.Namespace(dry_run=True, json=True, max=None, failure_limit=2)
    assert kanban_ops._cmd_dispatch(args) == 0
    import json

    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped_spec_gate"] == [
        {"task_id": tid, "reason": "spec_gate:acceptance"},
    ]


# --- 验收编号口径：词边界 A\d+ (t_81e401f4) ----------------------------------
#
# family-ops/tests/kanban_spec_gate.py 与 validate_spec_driven.py 用的是
# `(?<![0-9A-Za-z_])A\d+(?![0-9A-Za-z_])`；旧式 A[0-9] 会把 "A2A" 里的 A2
# 当成验收编号，于是缺验收的卡反而被放行（该拦不拦）。

# 无协议标记、无验收标记的正文占位（凑足 30 字下限用）。
FILLER = "正文占位说明文字仅用于凑足正文长度下限，不含其它要素。"
PLAIN_TITLE = "常规需求卡片"


@pytest.mark.parametrize("marker", ["A1", "A10", "（A2）", "验收", "acceptance"])
def test_acceptance_marker_inside_word_boundary_counts(
    kanban_home, gate_on, spawnable, marker,
):
    """A1 / A10 /（A2）/验收/acceptance 都算验收标准 → 正常派发。"""
    with kbc.connect() as conn:
        tid = _new_spec_card(
            conn, assignee="alice", title=PLAIN_TITLE,
            body=f"spec-driven 卡\n{FILLER}\n{marker}\n",
        )
        res = kbd.dispatch_once(conn, dry_run=True)
    assert [t for t, _a, _w in res.spawned] == [tid]
    assert res.skipped_spec_gate == []


@pytest.mark.parametrize("marker", ["A2A", "X-A2B", "A2A 协作说明"])
def test_acceptance_lookalike_does_not_count(
    kanban_home, gate_on, spawnable, marker,
):
    """A2A / X-A2B 里的 A2 不是验收编号 → 缺验收，仍然拦住。"""
    with kbc.connect() as conn:
        tid = _new_spec_card(
            conn, assignee="alice", title=PLAIN_TITLE,
            body=f"spec-driven 卡\n{FILLER}\n{marker}\n",
        )
        res = kbd.dispatch_once(conn, dry_run=True)
    assert res.spawned == []
    assert res.skipped_spec_gate == [(tid, "spec_gate:acceptance")]


# --- spec-driven 卡识别口径与 family-ops 对齐 (t_81e401f4) -------------------

SPEC_CARD_MARKERS = [
    "3hometech-spec-driven-v1",
    "spec-driven",
    "initiative_id: HUB-0042",
    "initiative id",
    "HUB-0042",
    "FAMILY-0012",
    "STOCK-0007",
    "CONN-0031",
    "SM-0042",
    "sm-0042",
    "见 docs/specs/tracking.md",
    "spec.md",
    "spec_version: 1.0",
    "spec-version 1.0",
]

PLAIN_BODY_MARKERS = [
    "HUB-42",          # 四位编号才算 initiative 编号
    "SM-42",
    "A2A 协作说明",
    "常规运营需求，无协议标记",
]


@pytest.mark.parametrize("marker", SPEC_CARD_MARKERS)
def test_protocol_marker_puts_card_in_gate_scope(
    kanban_home, gate_on, spawnable, marker,
):
    """protocol 标记 / initiative_id / 四位编号 / spec 引用 → 进门禁范围。"""
    with kbc.connect() as conn:
        tid = _new_spec_card(
            conn, assignee="alice", title=PLAIN_TITLE,
            body=f"{marker}\n{FILLER}\n",
        )
        res = kbd.dispatch_once(conn, dry_run=True)
    assert res.spawned == []
    assert res.skipped_spec_gate == [(tid, "spec_gate:acceptance")]


@pytest.mark.parametrize("marker", PLAIN_BODY_MARKERS)
def test_plain_card_stays_out_of_gate_scope(
    kanban_home, gate_on, spawnable, marker,
):
    """无协议标记的普通卡（含两位编号、A2A 字样）照旧派发，不误杀。"""
    with kbc.connect() as conn:
        tid = _new_spec_card(
            conn, assignee="alice", title=PLAIN_TITLE,
            body=f"{marker}\n{FILLER}\n",
        )
        res = kbd.dispatch_once(conn, dry_run=True)
    assert [t for t, _a, _w in res.spawned] == [tid]
    assert res.skipped_spec_gate == []


# --- 审计事件幂等去重 (t_81e401f4) ------------------------------------------


def _block_events(conn: sqlite3.Connection, tid: str):
    return [e for e in kb.list_events(conn, tid) if e.kind == "spec_gate_blocked"]


def test_repeated_ticks_write_one_block_event(kanban_home, gate_on, spawnable):
    """卡未修复：连续三次 tick 被拦只留一条事件，卡仍 READY。"""
    with kbc.connect() as conn:
        tid = _new_spec_card(conn, assignee="alice")
        for _ in range(3):
            res = kbd.dispatch_once(conn, dry_run=False,
                                    spawn_fn=lambda _t, _w: 4242)
            assert res.skipped_spec_gate == [(tid, "spec_gate:acceptance")]
            assert res.spawned == []
        events = _block_events(conn, tid)
        payload = events[0].payload or {}
        task = kb.get_task(conn, tid)
    assert len(events) == 1
    assert payload["reason"] == "spec_gate:acceptance"
    assert payload["card_digest"]
    assert task is not None and task.status == "ready"


def test_changed_reason_writes_a_new_block_event(kanban_home, gate_on, spawnable):
    """缺项集合变了（reason 变化）→ 追加一条新事件，旧事件保留。"""
    with kbc.connect() as conn:
        tid = _new_spec_card(conn, assignee="alice")
        res1 = kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda _t, _w: 4242)
        assert res1.skipped_spec_gate == [(tid, "spec_gate:acceptance")]
        # 补上验收，同时清掉 assignee —— 缺项从 acceptance 变成 assignee。
        conn.execute(
            "UPDATE tasks SET body = ?, assignee = NULL WHERE id = ?",
            (SPEC_BODY + ACCEPTANCE, tid),
        )
        conn.commit()
        res2 = kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda _t, _w: 4242)
        events = _block_events(conn, tid)
    assert res2.skipped_spec_gate == [(tid, "spec_gate:assignee")]
    assert [(e.payload or {}).get("reason") for e in events] == [
        "spec_gate:acceptance", "spec_gate:assignee"]


def test_edited_card_same_reason_records_the_repair_attempt(
    kanban_home, gate_on, spawnable,
):
    """reason 相同但卡被编辑过 → 记为一次修复尝试（新事件），不是 tick 噪声。"""
    with kbc.connect() as conn:
        tid = _new_spec_card(conn, assignee="alice")
        kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda _t, _w: 4242)
        conn.execute("UPDATE tasks SET body = body || ? WHERE id = ?", (FILLER, tid))
        conn.commit()
        res = kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda _t, _w: 4242)
        events = _block_events(conn, tid)
    assert res.skipped_spec_gate == [(tid, "spec_gate:acceptance")]
    assert len(events) == 2
    # 编辑后仍缺同一项：再 tick 一次不再追加（去重对编辑后的状态同样成立）。
    with kbc.connect() as conn:
        kbd.dispatch_once(conn, dry_run=False, spawn_fn=lambda _t, _w: 4242)
        assert len(_block_events(conn, tid)) == 2
