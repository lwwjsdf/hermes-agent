"""Scheduled wake lifecycle regression coverage for family-ops#190."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd


@pytest.fixture
def scheduled_board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _events(conn, task_id, *kinds):
    return [event for event in kb.list_events(conn, task_id) if event.kind in kinds]


def test_dispatch_tick_wakes_once_recovers_after_restart_and_allows_reschedule(
    scheduled_board, monkeypatch,
):
    now = datetime.now(timezone.utc)
    due_reason = f"SCHEDULED_UNTIL={(now - timedelta(seconds=5)).isoformat()} 等待维护窗口"
    future_reason = f"SCHEDULED_UNTIL={(now + timedelta(hours=1)).isoformat()} later"
    spawns = []
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda _name: True)

    with kbc.connect() as conn:
        due = kb.create_task(conn, title="due", assignee="worker")
        future = kb.create_task(conn, title="future", assignee="worker")
        manual = kb.create_task(conn, title="manual", assignee="worker")
        assert kb.schedule_task(conn, due, reason=due_reason)
        assert kb.schedule_task(conn, future, reason=future_reason)
        assert kb.schedule_task(conn, manual, reason="manual parking")

        def spawn(task, _workspace, _board=None):
            spawns.append(task.id)
            return 4242

        kbd.dispatch_once(conn, spawn_fn=spawn)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert kb.get_task(conn, due).status == "running"
        assert kb.get_task(conn, future).status == "scheduled"
        assert kb.get_task(conn, manual).status == "scheduled"
        wakes = _events(conn, due, "scheduled_wake")
        assert len(wakes) == 1
        assert wakes[0].payload["late_seconds"] >= 4
        assert wakes[0].payload["source_event_id"]
        assert _events(conn, future, "scheduled_wake", "scheduled_wake_missed") == []
        assert spawns == [due]

    # A new connection models a gateway restart crossing the wake point.
    with kbc.connect() as conn:
        kbd.dispatch_once(conn, spawn_fn=lambda *_args: spawns.append("duplicate"))
        assert len(_events(conn, due, "scheduled_wake")) == 1
        assert spawns == [due]

        # A new scheduled event is a new accounting source and may wake once.
        conn.execute("UPDATE tasks SET status = 'ready' WHERE id = ?", (due,))
        assert kb.schedule_task(conn, due, reason=due_reason)
        kbd.dispatch_once(conn, spawn_fn=spawn)
        assert len(_events(conn, due, "scheduled_wake")) == 2
        assert spawns == [due, due]

        missed = kb.create_task(conn, title="missed", assignee="worker")
        missed_reason = f"SCHEDULED_UNTIL={(now - timedelta(seconds=700)).isoformat()} overdue"
        assert kb.schedule_task(conn, missed, reason=missed_reason)
        kbd.dispatch_once(conn, spawn_fn=spawn, scheduled_wake_grace_seconds=600)
        missed_events = _events(conn, missed, "scheduled_wake_missed")
        assert len(missed_events) == 1
        assert missed_events[0].payload["late_seconds"] >= 699

        from gateway.kanban_watchers_notifier import TERMINAL_KINDS, diagnostic_event
        assert diagnostic_event(missed_events[0])
        assert "scheduled_wake_missed" in TERMINAL_KINDS
        assert not diagnostic_event(_events(conn, due, "scheduled_wake")[-1])
        assert "scheduled_wake" not in TERMINAL_KINDS


@pytest.mark.parametrize(
    ("reason", "semantic"),
    [
        ("SCHEDULED_UNTIL=not-a-date", "invalid scheduled wake timestamp"),
        ("SCHEDULED_UNTIL=2026-10-14T13:30:00", "requires an explicit timezone"),
        ("SCHEDULED_UNTIL=2026-02-30T13:30:00+08:00", "invalid scheduled wake timestamp"),
        (
            "SCHEDULED_UNTIL=2026-10-14T13:30:00+08:00 SCHEDULED_UNTIL=2026-10-15T13:30:00+08:00",
            "must appear exactly once",
        ),
    ],
)
def test_invalid_markers_are_rejected_at_both_entries_and_legacy_alerts_once(
    scheduled_board, monkeypatch, capsys, reason, semantic,
):
    from gateway.kanban_watchers_notifier import (
        TERMINAL_KINDS, _EVENT_FORMATTERS, diagnostic_event,
    )
    from tools import kanban_tools as kt

    with kbc.connect() as conn:
        task_id = kb.create_task(conn, title="invalid", assignee="worker")
        kb.claim_task(conn, task_id)
        run_id = kb.get_task(conn, task_id).current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    tool_result = json.loads(kt._handle_schedule({"reason": reason}))
    assert semantic in tool_result["error"]
    assert "expected SCHEDULED_UNTIL=" in tool_result["error"]

    monkeypatch.delenv("HERMES_KANBAN_TASK")
    monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    sub = parser.add_subparsers(dest="command")
    kc.build_parser(sub)
    args = parser.parse_args(["kanban", "schedule", task_id, reason])
    assert kc.kanban_command(args) != 0
    cli_error = capsys.readouterr().err
    assert semantic in cli_error
    assert "expected SCHEDULED_UNTIL=" in cli_error
    with kbc.connect() as conn:
        assert kb.get_task(conn, task_id).status == "running"

        # Simulate a pre-fix row, which must alert but remain parked.
        assert kb.schedule_task(conn, task_id, reason="manual parking")
        source = _events(conn, task_id, "scheduled")[-1]
        conn.execute(
            "UPDATE task_events SET payload = ? WHERE id = ?",
            (json.dumps({"reason": reason}), source.id),
        )
        assert kb.sweep_scheduled_tasks(conn) == 0
        assert kb.sweep_scheduled_tasks(conn) == 0
        assert kb.get_task(conn, task_id).status == "scheduled"
        alerts = _events(conn, task_id, "scheduled_wake_unparsable")
        assert len(alerts) == 1
        assert alerts[0].payload["source_event_id"] == source.id

    class StubNotification:
        head = "task"

    alert = alerts[0]
    assert alert.kind in TERMINAL_KINDS
    assert diagnostic_event(alert)
    assert alert.kind in _EVENT_FORMATTERS
    assert "needs attention" in _EVENT_FORMATTERS[alert.kind](alert, StubNotification())[0]
