"""Persistent short-window idempotency ledger for inbound A2A messages."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DUPLICATE_PREFIX = "[duplicate-receipt]"


def canonical_message_hash(message: dict) -> str:
    """Hash stable message content for retries whose messageId changes or is absent.

    ``messageId`` is transport identity and ``contextId`` is checked independently, so both
    fields are omitted. All remaining members (including ordered Parts) are encoded as compact
    UTF-8 JSON with recursively sorted object keys; array order remains significant.
    """
    canonical = {key: value for key, value in message.items() if key not in {"messageId", "contextId"}}
    body = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class DedupLedger:
    """Thread-safe append-only ledger; malformed/unreadable persistence never blocks A2A."""

    def __init__(self, path: Path, window_seconds: float) -> None:
        self.path = path
        self.window_seconds = window_seconds
        self._lock = threading.Lock()
        self._by_key: dict[tuple[str, str], list[dict]] = {}
        self._by_task: dict[str, dict] = {}
        self._load()

    @staticmethod
    def _keys(peer: str, message: dict) -> list[tuple[str, str]]:
        keys = []
        message_id = str(message.get("messageId") or "").strip()
        if message_id:
            keys.append((peer, "id:" + message_id))
        keys.append((peer, "sha256:" + canonical_message_hash(message)))
        return keys

    def _index(self, rec: dict) -> None:
        task_id = str(rec.get("first_task_id") or "")
        if not task_id:
            return
        old = self._by_task.get(task_id)
        if old is not None:
            for key in old.get("keys", []):
                bucket = self._by_key.get(tuple(key), [])
                self._by_key[tuple(key)] = [item for item in bucket if item.get("first_task_id") != task_id]
        self._by_task[task_id] = rec
        for raw_key in rec.get("keys", []):
            key = tuple(raw_key)
            self._by_key.setdefault(key, []).append(rec)

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8-sig").splitlines()
        except (OSError, UnicodeError):
            return
        for line in lines:
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(rec, dict):
                self._index(rec)

    def _append(self, rec: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
        except Exception:
            logger.debug("A2A: dedup ledger write failed", exc_info=True)

    def claim(
        self,
        peer: str,
        message: dict,
        context_id: str,
        inbound_context_id: str,
        task_id: str,
        create_task: Callable[[], dict],
    ) -> tuple[Optional[dict], Optional[dict]]:
        """Atomically return a live duplicate, or register and create a new task."""
        now = time.time()
        keys = self._keys(peer, message)
        with self._lock:
            mismatched = False
            for key in keys:
                for rec in reversed(self._by_key.get(key, [])):
                    if now - float(rec.get("first_seen", 0)) >= self.window_seconds:
                        continue
                    if not inbound_context_id or str(rec.get("contextId") or "") == inbound_context_id:
                        rec = dict(rec)
                        rec["latest_reply_ts"] = now
                        self._index(rec)
                        self._append(rec)
                        return rec, None
                    mismatched = True
            if mismatched:
                logger.warning("A2A: idempotency key reused with a different contextId; accepting as a new task")
            task_rec = create_task()
            rec = {
                "ts": now,
                "messageId": str(message.get("messageId") or ""),
                "contextId": context_id,
                "first_seen": now,
                "first_task_id": task_id,
                "state": task_rec.get("state", ""),
                "reply": task_rec.get("reply", ""),
                "latest_reply_ts": now,
                "keys": [list(key) for key in keys],
            }
            self._index(rec)
            self._append(rec)
            return None, task_rec

    def update(self, task_id: str, state: str, reply: str) -> None:
        with self._lock:
            current = self._by_task.get(task_id)
            if current is None:
                return
            rec = dict(current)
            rec.update(ts=time.time(), state=state, reply=reply, latest_reply_ts=time.time())
            self._index(rec)
            self._append(rec)
