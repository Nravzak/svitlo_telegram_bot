import aiosqlite
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from config import DB_PATH


def _utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    """
    Async SQLite wrapper (aiosqlite).
    One table `chats` for MVP + lightweight migrations.
    """

    def __init__(self, db_path: str = DB_PATH) -> None:
        self.db_path = db_path

    async def init(self) -> None:
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL;")
            await db.execute("PRAGMA synchronous=NORMAL;")
            await db.execute("PRAGMA foreign_keys=ON;")

            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS chats (
                    chat_id INTEGER PRIMARY KEY,
                    group_name TEXT,
                    group_selected INTEGER NOT NULL DEFAULT 0,
                    notify_enabled INTEGER NOT NULL DEFAULT 0,

                    last_schedule_hash TEXT,
                    last_message_id INTEGER,
                    last_notified_outage_start TEXT,

                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

            # --- lightweight migration for existing DBs (add missing columns) ---
            db.row_factory = aiosqlite.Row
            cur = await db.execute("PRAGMA table_info(chats);")
            cols = [r["name"] for r in await cur.fetchall()]

            if "group_selected" not in cols:
                await db.execute(
                    "ALTER TABLE chats ADD COLUMN group_selected INTEGER NOT NULL DEFAULT 0;"
                )

            # Индексы на будущее (не обязательны, но полезны)
            await db.execute(
                "CREATE INDEX IF NOT EXISTS idx_chats_notify ON chats(notify_enabled);"
            )
            await db.commit()

    async def get_or_create_chat(self, chat_id: int) -> Dict[str, Any]:
        row = await self.get_chat(chat_id)
        if row:
            return row

        now = _utc_iso()
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO chats (chat_id, created_at, updated_at)
                VALUES (?, ?, ?)
                """,
                (chat_id, now, now),
            )
            await db.commit()

        return await self.get_chat(chat_id)  # type: ignore

    async def get_chat(self, chat_id: int) -> Optional[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(
                "SELECT * FROM chats WHERE chat_id = ?",
                (chat_id,),
            )
            row = await cur.fetchone()
            if not row:
                return None
            return dict(row)

    async def list_chats(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM chats")
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def list_notify_chats(self) -> List[Dict[str, Any]]:
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute("SELECT * FROM chats WHERE notify_enabled = 1")
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def set_group(self, chat_id: int, group_name: str) -> None:
        # important: mark group as selected so /start shows schedule next time
        await self._update(
            chat_id,
            {"group_name": group_name, "group_selected": 1},
        )

    async def set_notify(self, chat_id: int, enabled: bool) -> None:
        await self._update(chat_id, {"notify_enabled": 1 if enabled else 0})

    async def toggle_notify(self, chat_id: int) -> bool:
        chat = await self.get_or_create_chat(chat_id)
        new_value = 0 if int(chat["notify_enabled"]) == 1 else 1
        await self._update(chat_id, {"notify_enabled": new_value})
        return new_value == 1

    async def set_last_message_id(self, chat_id: int, message_id: int) -> None:
        await self._update(chat_id, {"last_message_id": message_id})

    async def update_schedule_hash(self, chat_id: int, schedule_hash: str) -> None:
        await self._update(chat_id, {"last_schedule_hash": schedule_hash})

    async def set_last_notified_outage_start(self, chat_id: int, outage_start_iso: str) -> None:
        await self._update(chat_id, {"last_notified_outage_start": outage_start_iso})

    async def _update(self, chat_id: int, fields: Dict[str, Any]) -> None:
        if not fields:
            return

        fields = dict(fields)
        fields["updated_at"] = _utc_iso()

        keys = list(fields.keys())
        sets = ", ".join([f"{k} = ?" for k in keys])
        values = [fields[k] for k in keys] + [chat_id]

        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"UPDATE chats SET {sets} WHERE chat_id = ?",
                values,
            )
            await db.commit()


# Удобный singleton (по желанию)
db = Database()
