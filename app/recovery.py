"""Durable delivery IDs and reconnect checkpoint for MAX history recovery."""

import os
import sqlite3
import time


class RecoveryStore:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS delivered (chat_id TEXT NOT NULL, message_id TEXT NOT NULL, delivered_at INTEGER NOT NULL, PRIMARY KEY (chat_id, message_id))")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)")
        # Telegram message id (unique within the one supergroup) → MAX message, for replies
        self.db.execute("CREATE TABLE IF NOT EXISTS tg_links (tg_message_id INTEGER PRIMARY KEY, chat_id TEXT NOT NULL, message_id TEXT NOT NULL, created_at INTEGER NOT NULL)")
        self.db.commit()

    def get(self, key: str) -> int | None:
        row = self.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key,value) VALUES (?,?)", (key, value))
        self.db.commit()

    def seen(self, chat_id, message_id) -> bool:
        return self.db.execute("SELECT 1 FROM delivered WHERE chat_id=? AND message_id=?", (str(chat_id), str(message_id))).fetchone() is not None

    def mark(self, chat_id, message_id) -> None:
        self.db.execute("INSERT OR IGNORE INTO delivered VALUES (?,?,?)", (str(chat_id), str(message_id), int(time.time() * 1000)))
        self.db.commit()

    def link_tg(self, tg_message_id, chat_id, message_id) -> None:
        self.db.execute("INSERT OR REPLACE INTO tg_links VALUES (?,?,?,?)", (int(tg_message_id), str(chat_id), str(message_id), int(time.time() * 1000)))
        self.db.commit()

    def max_for_tg(self, tg_message_id) -> tuple[str, str] | None:
        row = self.db.execute("SELECT chat_id, message_id FROM tg_links WHERE tg_message_id=?", (int(tg_message_id),)).fetchone()
        return (row[0], row[1]) if row else None

    def prune(self) -> None:
        now = int(time.time() * 1000)
        self.db.execute("DELETE FROM delivered WHERE delivered_at < ?", (now - 7 * 86400000,))
        self.db.execute("DELETE FROM tg_links WHERE created_at < ?", (now - 90 * 86400000,))
        self.db.commit()
