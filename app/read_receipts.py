"""Small persistent ledger for Telegram messages awaiting a MAX read mark.

Only IDs and timestamps are stored; message text and attachments are never saved.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

MAX_PENDING_AGE_MS = 30 * 24 * 60 * 60 * 1000


class ReadReceiptStore:
    def __init__(self, path: str):
        self.path = Path(path)
        self.pending: list[dict[str, int]] = []
        self.marks: dict[str, dict[str, int]] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.pending = data.get("pending", [])
            self.marks = data.get("marks", {})
            self._prune()
        except (OSError, ValueError, TypeError):
            log.exception("Could not load read receipt state; leaving it untouched")
            self.pending = []
            self.marks = {}

    def _prune(self) -> None:
        cutoff = int(time.time() * 1000) - MAX_PENDING_AGE_MS
        self.pending = [
            record for record in self.pending
            if int(record.get("max_time", 0)) >= cutoff
        ]
        self.marks = {
            chat_id: fresh for chat_id, peers in self.marks.items()
            if (fresh := {peer_id: int(mark) for peer_id, mark in peers.items()
                          if int(mark) >= cutoff})
        }

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".read_receipts.", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"pending": self.pending, "marks": self.marks}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def add(self, chat_id: int, peer_id: int, max_time: int,
            tg_message_id: int) -> bool:
        """Remember a DM send; return True if a read mark already covers it."""
        record = {
            "chat_id": int(chat_id),
            "peer_id": int(peer_id),
            "max_time": int(max_time),
            "tg_message_id": int(tg_message_id),
        }
        self.pending = [p for p in self.pending if not (
            p["chat_id"] == record["chat_id"]
            and p["tg_message_id"] == record["tg_message_id"]
        )]
        self.pending.append(record)
        self._prune()
        self._save()
        return self.marks.get(str(chat_id), {}).get(str(peer_id), 0) >= max_time

    def mark_read(self, chat_id: int, peer_id: int, mark: int) -> list[int]:
        """Advance one peer's mark and list Telegram messages now read."""
        chat_key, peer_key = str(chat_id), str(peer_id)
        peer_marks = self.marks.setdefault(chat_key, {})
        peer_marks[peer_key] = max(int(mark), peer_marks.get(peer_key, 0))
        self._prune()
        self._save()
        return [
            p["tg_message_id"] for p in self.pending
            if p["chat_id"] == int(chat_id)
            and p["peer_id"] == int(peer_id)
            and p["max_time"] <= peer_marks[peer_key]
        ]

    def acknowledge(self, chat_id: int, tg_message_id: int) -> None:
        self.pending = [p for p in self.pending if not (
            p["chat_id"] == int(chat_id)
            and p["tg_message_id"] == int(tg_message_id)
        )]
        self._save()
