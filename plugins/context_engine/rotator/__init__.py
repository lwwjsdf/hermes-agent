"""Rotator context engine: mechanical session rotation at 70% of the context window.

Zero LLM calls per rotation. compress() builds the child session's opening
transcript deterministically: one handoff header (anchor index extracted from the
dropped head + a session_search recovery pointer) followed by a verbatim lean tail
(official formula: 2.5% of the window, clamped to 10k-25k tokens). Session forking,
gateway remapping and lease rebinding are done by the host's legacy rotating path --
enable it per profile with compression.in_place: false.

Design decisions (family-ops#22, 2026-09-16): threshold 0.70, mechanical preload,
cron excluded (cron agents run with platform="cron" and are always fresh sessions).
The host's "Session compressed N times -- Consider /new" warning reads
compression_count; this engine keeps that attribute at <= 1 and tracks the real
rotation count in _rotation_count, so long-lived chats never see the warning.
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple

from agent.context_engine import ContextEngine


ROTATOR_THRESHOLD_PERCENT = 0.70

# Lean-tail budget: official ContextCompressor formula -- 2.5% of the window,
# clamped to [10k, 25k] tokens (matches the built-in lean mode field-tested at
# ~49k post-compaction transcripts with high recall on 500k sessions).
_TAIL_BUDGET_RATIO = 0.025
_TAIL_BUDGET_MIN_TOKENS = 10_000
_TAIL_BUDGET_MAX_TOKENS = 25_000
_TAIL_BUDGET_FALLBACK_TOKENS = 10_000

_BREAKER_LATCH_STRIKES = 3

# Platforms that never rotate. Cron builds its agents with platform="cron" and
# every run is a fresh session anyway, so rotation there is meaningless.
_INERT_PLATFORMS = {"cron"}

_ANCHOR_PATTERNS = (
    ("pr", re.compile(r"(?:PR|issue|fixes|closes)[ #-]#?(\d{1,7})", re.I)),
    ("sha", re.compile(r"\b[0-9a-f]{7,40}\b")),
    ("path", re.compile(r"\b[\w.-]+/[^\s:]{2,}\b|\b[\w.-]+\.(?:py|ts|tsx|js|mjs|md|json|ya?ml|toml)\b")),
    ("url", re.compile(r"https?://[^\s)>\]]+")),
    ("error", re.compile(r"(?:Error|Exception|Traceback|FAILED)\b[^\n]{0,100}", re.I)),
)
_ANCHOR_MAX_ENTRIES = 40
_ANCHOR_MAX_CHARS = 120
_HEADER_MAX_CHARS = 4_000


def _rough_message_tokens(message: Dict[str, Any]) -> int:
    from agent.model_metadata import estimate_messages_tokens_rough

    return estimate_messages_tokens_rough([message])


def _tail_budget_tokens(context_length: int) -> int:
    if not context_length or context_length <= 0:
        return _TAIL_BUDGET_FALLBACK_TOKENS
    return max(_TAIL_BUDGET_MIN_TOKENS, min(_TAIL_BUDGET_MAX_TOKENS, int(context_length * _TAIL_BUDGET_RATIO)))


def _extract_anchors(dropped: List[Dict[str, Any]]) -> List[str]:
    """Mechanical anchor index over the dropped head: regex extraction only, no rewriting."""
    anchors: List[str] = []
    seen = set()
    for message in dropped:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content:
            continue
        for kind, pattern in _ANCHOR_PATTERNS:
            for match in pattern.finditer(content):
                raw = match.group(0).strip()
                if not raw:
                    continue
                anchor = raw[:_ANCHOR_MAX_CHARS]
                key = (kind, anchor.lower())
                if key in seen:
                    continue
                seen.add(key)
                anchors.append("[" + kind + "] " + anchor)
                if len(anchors) >= _ANCHOR_MAX_ENTRIES:
                    return anchors
    return anchors


class RotatorEngine(ContextEngine):
    """Mechanical rotation engine. Thread-safety contract: compression runs on a pooled
    daemon thread and may be discarded on timeout, so compress() writes no external
    persistent state before the host commits -- all state is in-memory on the engine."""

    threshold_percent = ROTATOR_THRESHOLD_PERCENT
    # Routine rotation is silent background maintenance (zero-aware UX); warnings,
    # errors and manual /compress still surface through the host.
    emit_automatic_compaction_status = False

    def __init__(self) -> None:
        self._armed = True
        self._platform = ""
        self._session_id = ""
        self._rotation_count = 0
        self._cooldown_until = 0.0
        self._cooldown_reason = ""
        self._rejected_strikes = 0
        self._breaker_latched = False

    @property
    def name(self) -> str:
        return "rotator"

    # ---- token tracking ----

    def update_from_response(self, usage: Dict[str, Any]) -> None:
        if not isinstance(usage, dict):
            return
        prompt = usage.get("prompt_tokens")
        if prompt is None:
            prompt = (
                (usage.get("input_tokens") or 0)
                + (usage.get("cache_read_tokens") or 0)
                + (usage.get("cache_write_tokens") or 0)
            )
        completion = usage.get("completion_tokens", usage.get("output_tokens") or 0)
        total = usage.get("total_tokens") or ((prompt or 0) + (completion or 0))
        self.last_prompt_tokens = int(prompt or 0)
        self.last_completion_tokens = int(completion or 0)
        self.last_total_tokens = int(total or 0)

    # ---- trigger ----

    def should_compress_info(self, prompt_tokens: int = None) -> Tuple[bool, Optional[str]]:
        if not self._armed:
            return False, "inert platform: " + repr(self._platform or "unknown")
        if self._breaker_latched:
            return False, "breaker latched after repeated ineffective rotations"
        if self._cooldown_active():
            return False, "cooldown: " + self._cooldown_reason
        tokens = self.last_prompt_tokens if prompt_tokens is None else int(prompt_tokens)
        if tokens <= 0:  # includes the host's -1 "just compressed, await real usage" sentinel
            return False, None
        if self.threshold_tokens <= 0 or self.context_length <= 0:
            return False, "model context length unknown"
        if tokens >= self.threshold_tokens:
            return True, "prompt " + format(tokens, ",") + " >= threshold " + format(self.threshold_tokens, ",")
        return False, None

    def should_compress(self, prompt_tokens: int = None) -> bool:
        return self.should_compress_info(prompt_tokens)[0]

    # ---- failure backoff (in-memory; the host's durable cooldown machinery is
    # built-in-compressor-only and explicitly skips plugin engines) ----

    def _cooldown_active(self) -> bool:
        return time.time() < self._cooldown_until

    def _automatic_compression_blocked(self, ignore_cooldown: bool = False) -> bool:
        if self._breaker_latched:
            return True
        return not ignore_cooldown and self._cooldown_active()

    def _record_compression_failure_cooldown(self, seconds: float, reason: str = "") -> None:
        self._cooldown_until = max(self._cooldown_until, time.time() + max(0.0, float(seconds)))
        self._cooldown_reason = str(reason or "compression failure")

    def record_rejected_compaction(self) -> None:
        """Anti-thrash strikes for would-grow refusals; latch after 3 so automatic
        rotation stops retrying a doomed shape (manual /compress keeps force=True)."""
        self._rejected_strikes += 1
        if self._rejected_strikes >= _BREAKER_LATCH_STRIKES:
            self._breaker_latched = True

    # ---- mechanical preload ----

    def compress(
        self,
        messages: List[Dict[str, Any]],
        current_tokens: Optional[int] = None,
        focus_topic: Optional[str] = None,
        force: bool = False,
        memory_context: str = "",
    ) -> List[Dict[str, Any]]:
        usable = [m for m in messages if isinstance(m, dict) and m.get("role") != "system"]
        if len(usable) < 2:
            return list(messages)

        budget = _tail_budget_tokens(self.context_length)
        tail: List[Dict[str, Any]] = []
        spent = 0
        i = len(usable) - 1
        while i >= 0:
            message = usable[i]
            role = str(message.get("role") or "")
            cost = _rough_message_tokens(message)
            # Tool blocks travel with their assistant tool_calls caller: never split
            # the pair, even past budget (an orphaned tool result is an invalid request).
            pairs_with_tail = role in ("tool", "function") or (
                role == "assistant"
                and message.get("tool_calls")
                and tail
                and tail[-1].get("role") in ("tool", "function")
            )
            if tail and not pairs_with_tail and spent + cost > budget:
                break
            tail.append(message)
            spent += cost
            i -= 1
        tail.reverse()

        dropped = usable[: len(usable) - len(tail)]
        if not dropped:
            # Nothing exceeds the lean budget (manual /compress on a small session):
            # rotation is pointless; hand back the input and let the host no-op.
            return list(messages)

        header = self._build_header(dropped, tail, focus_topic=focus_topic)
        self._rotation_count += 1
        # The host warns "Consider /new" at compression_count >= 2; keep the public
        # counter at <= 1 and expose the real count via get_status().
        self.compression_count = min(self._rotation_count, 1)
        return [header] + [dict(m, _compaction_tail=True) for m in tail]

    def _build_header(
        self, dropped: List[Dict[str, Any]], tail: List[Dict[str, Any]], *, focus_topic: Optional[str] = None,
    ) -> Dict[str, Any]:
        anchors = _extract_anchors(dropped)
        lines = [
            "[SESSION ROTATION] The earlier part of this conversation was mechanically rotated into the",
            "archived parent session (no LLM summary was produced). The verbatim recent tail below is",
            "unchanged. The full earlier transcript remains searchable with the session_search tool --",
            "query it by keyword before assuming something was lost.",
        ]
        if focus_topic:
            lines.append("Rotation focus requested: " + focus_topic)
        if anchors:
            lines.append("Mechanical anchor index (extracted verbatim from the rotated-away head):")
            lines.extend("- " + a for a in anchors)
        content = "\n".join(lines)[:_HEADER_MAX_CHARS]
        # Role is chosen so the header never duplicates the tail's first role
        # (strict alternation invariant).
        first_tail_role = str(tail[0].get("role") or "user") if tail else "user"
        header_role = "assistant" if first_tail_role == "user" else "user"
        return {"role": header_role, "content": content}

    # ---- lifecycle ----

    def on_session_start(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        platform = str(kwargs.get("platform") or "").strip().lower()
        if platform:
            self._platform = platform
        # boundary_reason="compression" keeps state across the rotation boundary
        # (same conversation, child session); arming only changes with the platform.
        self._armed = self._platform not in _INERT_PLATFORMS

    def on_session_reset(self) -> None:
        super().on_session_reset()
        self._rotation_count = 0
        self.compression_count = 0
        self._rejected_strikes = 0
        self._breaker_latched = False
        self._cooldown_until = 0.0
        self._cooldown_reason = ""

    def get_status(self) -> Dict[str, Any]:
        status = super().get_status()
        status.update(
            engine="rotator",
            rotations=self._rotation_count,
            armed=self._armed,
            platform=self._platform,
            breaker_latched=self._breaker_latched,
            cooldown_active=self._cooldown_active(),
        )
        return status


engine = RotatorEngine()
