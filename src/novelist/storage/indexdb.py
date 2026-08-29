"""SQLite 辅助索引（ADR-016，docs/06 §9）。

`.index.db` 仅是加速的辅助查询通道，随时可删、可从文件重建；文件（bible/memory/outline/chapters）
才是持久事实源。写操作先落文件（原子写）后同步索引。
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass


@dataclass
class IndexDb:
    """封装 .index.db 的建表与基本查询。"""

    path: str

    def connect(self) -> sqlite3.Connection:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        conn = sqlite3.connect(self.path)
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS fragments (
                  sig TEXT PRIMARY KEY,
                  kind TEXT NOT NULL,
                  vol INTEGER NOT NULL,
                  ch INTEGER NOT NULL,
                  refs TEXT,
                  score_fields TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_log (
                  seq INTEGER PRIMARY KEY AUTOINCREMENT,
                  ts TEXT NOT NULL,
                  kind TEXT NOT NULL,
                  session TEXT,
                  payload TEXT
                );
                """
            )

    def upsert_fragment(self, sig, kind, vol, ch, refs, extra) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO fragments(sig,kind,vol,ch,refs,score_fields) VALUES (?,?,?,?,?,?)",
                (sig, kind, vol, ch, refs, extra),
            )

    def write_audit(self, kind, session, payload) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO audit_log(ts,kind,session,payload) VALUES (?,?,?,?)",
                ("now", kind, session or "", payload or ""),
            )
