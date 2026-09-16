"""Rotator context engine (family-ops#22 prototype): mechanical 0-LLM session rotation.

Behavior contracts tested through the real paths: engine discovery, the 0.70
threshold, mechanical preload shape (verbatim tail + anchor header, alternation,
tool-pairing), cron exclusion, the muted compression counter, and the full
host rotation/rollback/fallback flow via a real AIAgent + SessionDB.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from plugins.context_engine.rotator import RotatorEngine


def _engine(context_length: int = 100_000) -> RotatorEngine:
    engine = RotatorEngine()
    engine.update_model(model="test/model", context_length=context_length)
    engine.on_session_start("sess", platform="telegram")
    return engine


def _long_messages(n: int = 400) -> list:
    messages = [{"role": "user", "content": "fixes #47831 in gateway/run_turn.py (see https://example.test/47831)"}]
    for i in range(n):
        role = "user" if i % 2 == 0 else "assistant"
        messages.append({"role": role, "content": "turn " + str(i) + " " + "payload " * 40})
    return messages


class TestTrigger:
    def test_threshold_is_70_percent_of_window(self):
        engine = _engine(context_length=100_000)
        assert engine.threshold_tokens == 70_000
        assert engine.should_compress(69_999) is False
        assert engine.should_compress(70_000) is True

    def test_usage_tracking_from_response_buckets(self):
        engine = _engine()
        engine.update_from_response({
            "prompt_tokens": 72_000, "completion_tokens": 800, "total_tokens": 72_800,
        })
        assert engine.last_prompt_tokens == 72_000
        assert engine.should_compress() is True

    def test_post_compression_sentinel_does_not_retrigger(self):
        engine = _engine()
        engine.update_from_response({"prompt_tokens": 90_000})
        engine.last_prompt_tokens = -1  # host sentinel: compression just ran
        assert engine.should_compress() is False

    def test_cron_sessions_are_inert(self):
        engine = _engine()
        engine.on_session_start("cron-run", platform="cron")
        assert engine._armed is False
        assert engine.should_compress(95_000) is False
        assert "inert" in (engine.should_compress_info(95_000)[1] or "")


class TestMechanicalPreload:
    def test_compress_is_verbatim_tail_plus_header_no_llm(self):
        engine = _engine()
        messages = _long_messages()
        out = engine.compress(messages)
        assert len(out) < len(messages)
        header, tail = out[0], out[1:]
        assert header["content"].startswith("[SESSION ROTATION]")
        assert "session_search" in header["content"]
        # Anchor index captured the dropped head's references.
        assert "- [pr] fixes #47831" in header["content"]
        # The tail is verbatim (modulo the _compaction_tail bookkeeping tag).
        stripped = [{k: v for k, v in m.items() if k != "_compaction_tail"} for m in tail]
        assert stripped == messages[len(messages) - len(tail):]
        # Alternation: no two adjacent messages share a role.
        roles = [m["role"] for m in out]
        assert all(a != b for a, b in zip(roles, roles[1:]))

    def test_tool_call_pairs_are_never_split(self):
        engine = _engine()
        filler = [{"role": "user", "content": "filler " + "x" * 400} for _ in range(300)]
        messages = filler + [
            {"role": "user", "content": "run the check"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "ok"},
            {"role": "assistant", "content": "done"},
        ]
        out = engine.compress(messages)
        roles = [(m.get("role"), m.get("tool_call_id")) for m in out]
        tool_idx = [i for i, (role, _) in enumerate(roles) if role == "tool"]
        assert tool_idx, "the tool exchange must be kept in the tail"
        i = tool_idx[0]
        assert out[i - 1].get("role") == "assistant" and out[i - 1].get("tool_calls")

    def test_public_compression_count_never_reaches_warning_threshold(self):
        engine = _engine()
        for _ in range(5):
            engine.compress(_long_messages())
        assert engine.compression_count <= 1  # host warns at >= 2
        assert engine.get_status()["rotations"] == 5

    def test_small_transcript_is_returned_unchanged(self):
        engine = _engine()
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        out = engine.compress(messages)
        assert [m["content"] for m in out] == [m["content"] for m in messages]


class TestFailureBackoff:
    def test_failure_cooldown_blocks_automatic_retries(self):
        engine = _engine()
        engine._record_compression_failure_cooldown(60, "session_split_failed: boom")
        assert engine._automatic_compression_blocked() is True
        assert engine.should_compress(95_000) is False
        assert engine._automatic_compression_blocked(ignore_cooldown=True) is False

    def test_rejected_compactions_latch_the_breaker(self):
        engine = _engine()
        for _ in range(3):
            engine.record_rejected_compaction()
        assert engine._breaker_latched is True
        assert engine.should_compress(95_000) is False


class TestDiscovery:
    def test_engine_loads_through_real_discovery_path(self):
        from plugins.context_engine import discover_context_engines, load_context_engine

        loaded = load_context_engine("rotator")
        assert isinstance(loaded, RotatorEngine)
        names = {name for name, _desc, _ok in discover_context_engines()}
        assert "rotator" in names

    def test_config_selects_rotator_engine(self, tmp_path: Path):
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "context:\n  engine: rotator\ncompression:\n  in_place: false\n"
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(home), "OPENROUTER_API_KEY": "test-key"}):
            from agent.agent_init import _select_context_engine

            selected = _select_context_engine({"context": {"engine": "rotator"}})
            assert isinstance(selected, RotatorEngine)

    def test_full_agent_resolution_chain_activates_rotator(self, tmp_path: Path):
        """E2E: a temp HERMES_HOME config selects the engine through AIAgent init —
        the same chain a rotator-test profile exercises at startup."""
        home = tmp_path / ".hermes"
        home.mkdir()
        (home / "config.yaml").write_text(
            "context:\n  engine: rotator\ncompression:\n  in_place: false\n"
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(home), "OPENROUTER_API_KEY": "test-key"}):
            from run_agent import AIAgent

            agent = AIAgent(
                api_key="test-key",
                base_url="https://openrouter.ai/api/v1",
                model="test/model",
                platform="telegram",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        assert isinstance(agent.context_compressor, RotatorEngine)
        assert agent.compression_in_place is False
        assert agent.context_compressor.threshold_percent == 0.70


def _build_agent(db, session_id: str, in_place: bool):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            platform="telegram",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )
    agent.context_compressor = _engine()
    agent.compression_in_place = in_place
    return agent


class TestHostRotationFlow:
    """Acceptance: drive the real compress_context rotation path with the real engine."""

    def test_rotation_forks_child_with_rebuilt_prompt_and_mechanical_opening(self, tmp_path: Path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        parent = "ROT_PARENT"
        db.create_session(parent, source="telegram")
        agent = _build_agent(db, parent, in_place=False)
        messages = _long_messages()

        returned, system_prompt = agent._compress_context(
            messages, "original system prompt", approx_tokens=90_000
        )
        child = agent.session_id
        assert child != parent  # rotation happened
        # Child opening = handoff header + verbatim tail (mechanical, 0 LLM calls).
        assert returned[0]["content"].startswith("[SESSION ROTATION]")
        assert "session_search" in returned[0]["content"]
        assert returned[1:]  # tail present
        # The boundary rebuilt the system prompt (host re-runs the full builder, which
        # is where MEMORY/USER blocks are re-injected on a memory-enabled profile).
        assert system_prompt and system_prompt != "original system prompt"
        row = db.get_session(child)
        assert row is not None and row["parent_session_id"] == parent
        assert row["system_prompt"] == system_prompt
        parent_row = db.get_session(parent)
        assert parent_row["ended_at"] is not None  # archived, still searchable
        db.close()

    def test_rotation_failure_rolls_back_to_live_parent_and_arms_cooldown(self, tmp_path: Path):
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        parent = "ROT_FAIL_PARENT"
        db.create_session(parent, source="telegram")
        agent = _build_agent(db, parent, in_place=False)
        original = _long_messages()

        with patch.object(type(db), "publish_compression_child", side_effect=RuntimeError("disk full")):
            returned, _ = agent._compress_context(original, "sys", approx_tokens=90_000)

        assert agent.session_id == parent  # rollback kept the parent live
        assert [(m["role"], m["content"]) for m in returned] == [
            (m["role"], m["content"]) for m in original
        ]
        assert db.get_session(parent)["ended_at"] is None
        assert db.find_live_compression_child(parent) is None
        assert agent.context_compressor._automatic_compression_blocked() is True  # cooldown armed
        db.close()

    def test_in_place_fallback_commits_with_the_same_engine(self, tmp_path: Path):
        """When rotation is off (e.g. the gateway's 85% hygiene agent forces in-place),
        the engine's mechanical output still commits through the in-place path."""
        from hermes_state import SessionDB

        db = SessionDB(db_path=tmp_path / "state.db")
        session_id = "ROT_INPLACE"
        db.create_session(session_id, source="telegram")
        agent = _build_agent(db, session_id, in_place=True)
        messages = _long_messages()

        returned, _ = agent._compress_context(messages, "sys", approx_tokens=90_000)

        assert agent.session_id == session_id  # no rotation
        assert len(returned) < len(messages)
        assert returned[0]["content"].startswith("[SESSION ROTATION]")
        assert agent._last_compression_attempt_in_place is True
        db.close()
