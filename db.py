from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite


class DB:
    def __init__(self, path: str = "bot.db"):
        self.path = path
        self.conn: aiosqlite.Connection | None = None

    async def connect(self):
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL;")
        await self.conn.execute("PRAGMA synchronous=NORMAL;")
        await self.conn.execute("PRAGMA foreign_keys=ON;")
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS seen_orders (
                order_id TEXT PRIMARY KEY,
                tg_message_id INTEGER,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS seen_messages (
                prom_message_id TEXT PRIMARY KEY,
                tg_message_id INTEGER,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tg_order_map (
                tg_message_id INTEGER PRIMARY KEY,
                order_id TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tg_message_map (
                tg_message_id INTEGER PRIMARY KEY,
                prom_message_id TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                store_name TEXT,
                tg_message_id INTEGER,
                prom_status TEXT,
                order_date TEXT,
                client_name TEXT,
                phone TEXT,
                email TEXT,
                total_price TEXT,
                payment TEXT,
                delivery TEXT,
                delivery_provider TEXT,
                delivery_city TEXT,
                delivery_warehouse TEXT,
                delivery_address TEXT,
                comment TEXT,
                order_url TEXT,
                telegraph_url TEXT,
                raw_json TEXT,
                first_seen_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS order_items (
                row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                product_id TEXT,
                product_name TEXT,
                sku TEXT,
                quantity TEXT,
                price TEXT,
                total_price TEXT,
                product_url TEXT,
                options_text TEXT,
                raw_json TEXT,
                FOREIGN KEY(order_id) REFERENCES orders(order_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS customer_messages (
                prom_message_id TEXT PRIMARY KEY,
                store_name TEXT,
                tg_message_id INTEGER,
                message_date TEXT,
                status TEXT,
                client_name TEXT,
                phone TEXT,
                email TEXT,
                order_id TEXT,
                product_id TEXT,
                product_name TEXT,
                sku TEXT,
                product_url TEXT,
                text TEXT,
                raw_json TEXT,
                first_seen_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS status_history (
                row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                old_status TEXT,
                new_status TEXT NOT NULL,
                note TEXT,
                changed_at INTEGER NOT NULL
            );
            """
        )
        await self._ensure_schema_updates()
        await self.conn.commit()

    async def _ensure_schema_updates(self):
        # Safe migrations for users who already have bot.db from an older version.
        async def ensure_column(table: str, column: str, decl: str):
            cur = await self.conn.execute(f"PRAGMA table_info({table})")
            cols = {row[1] for row in await cur.fetchall()}
            if column not in cols:
                await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

        await ensure_column("orders", "telegraph_url", "TEXT")
        await ensure_column("order_items", "options_text", "TEXT")

    async def close(self):
        if self.conn:
            await self.conn.close()

    async def has_order(self, order_id: str) -> bool:
        cur = await self.conn.execute("SELECT 1 FROM seen_orders WHERE order_id=?", (str(order_id),))
        row = await cur.fetchone()
        return row is not None

    async def add_order(self, order_id: str, tg_message_id: int | None = None):
        await self.conn.execute(
            """
            INSERT INTO seen_orders(order_id, tg_message_id, created_at) VALUES (?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                tg_message_id=COALESCE(excluded.tg_message_id, seen_orders.tg_message_id)
            """,
            (str(order_id), tg_message_id, int(time.time())),
        )
        if tg_message_id:
            await self.conn.execute(
                "INSERT OR REPLACE INTO tg_order_map(tg_message_id, order_id) VALUES (?, ?)",
                (tg_message_id, str(order_id)),
            )
        await self.conn.commit()

    async def reset_seen_orders(self):
        await self.conn.execute("DELETE FROM seen_orders")
        await self.conn.execute("DELETE FROM tg_order_map")
        await self.conn.commit()

    async def get_seen_order_tg_message_id(self, order_id: str) -> int | None:
        cur = await self.conn.execute("SELECT tg_message_id FROM seen_orders WHERE order_id=?", (str(order_id),))
        row = await cur.fetchone()
        if not row:
            return None
        return row["tg_message_id"]

    async def save_order(self, order: dict[str, Any], summary: dict[str, Any], store_name: str, tg_message_id: int | None = None):
        now = int(time.time())
        order_id = str(summary.get("order_id") or "")
        if not order_id:
            return

        cur = await self.conn.execute("SELECT prom_status FROM orders WHERE order_id=?", (order_id,))
        old = await cur.fetchone()
        old_status = old["prom_status"] if old else None
        new_status = str(summary.get("status") or "")

        await self.conn.execute(
            """
            INSERT INTO orders(
                order_id, store_name, tg_message_id, prom_status, order_date, client_name, phone, email,
                total_price, payment, delivery, delivery_provider, delivery_city, delivery_warehouse,
                delivery_address, comment, order_url, telegraph_url, raw_json, first_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                store_name=excluded.store_name,
                tg_message_id=COALESCE(excluded.tg_message_id, orders.tg_message_id),
                prom_status=excluded.prom_status,
                order_date=excluded.order_date,
                client_name=excluded.client_name,
                phone=excluded.phone,
                email=excluded.email,
                total_price=excluded.total_price,
                payment=excluded.payment,
                delivery=excluded.delivery,
                delivery_provider=excluded.delivery_provider,
                delivery_city=excluded.delivery_city,
                delivery_warehouse=excluded.delivery_warehouse,
                delivery_address=excluded.delivery_address,
                comment=excluded.comment,
                order_url=excluded.order_url,
                telegraph_url=COALESCE(excluded.telegraph_url, orders.telegraph_url),
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                order_id,
                store_name,
                tg_message_id,
                str(summary.get("status") or ""),
                str(summary.get("order_date") or ""),
                str(summary.get("client_name") or ""),
                str(summary.get("phone") or ""),
                str(summary.get("email") or ""),
                str(summary.get("total_price") or ""),
                str(summary.get("payment") or ""),
                str(summary.get("delivery") or ""),
                str(summary.get("delivery_provider") or ""),
                str(summary.get("delivery_city") or ""),
                str(summary.get("delivery_warehouse") or ""),
                str(summary.get("delivery_address") or ""),
                str(summary.get("comment") or ""),
                str(summary.get("order_url") or ""),
                (summary.get("telegraph_url") or None),
                json.dumps(order, ensure_ascii=False),
                now,
                now,
            ),
        )

        await self.conn.execute("DELETE FROM order_items WHERE order_id=?", (order_id,))
        for item in summary.get("items") or []:
            await self.conn.execute(
                """
                INSERT INTO order_items(
                    order_id, product_id, product_name, sku, quantity, price, total_price, product_url, options_text, raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_id,
                    str(item.get("product_id") or ""),
                    str(item.get("name") or ""),
                    str(item.get("sku") or ""),
                    str(item.get("quantity") or ""),
                    str(item.get("price") or ""),
                    str(item.get("total_price") or ""),
                    str(item.get("product_url") or ""),
                    str(item.get("options_text") or ""),
                    json.dumps(item.get("raw") or {}, ensure_ascii=False),
                ),
            )

        if old_status is not None and old_status != new_status and new_status:
            await self.conn.execute(
                "INSERT INTO status_history(order_id, old_status, new_status, note, changed_at) VALUES (?, ?, ?, ?, ?)",
                (order_id, old_status, new_status, "status changed from Prom data", now),
            )
        await self.conn.commit()

    async def update_order_status_local(self, order_id: str, new_status: str, note: str = ""):
        now = int(time.time())
        cur = await self.conn.execute("SELECT prom_status FROM orders WHERE order_id=?", (str(order_id),))
        row = await cur.fetchone()
        old_status = row["prom_status"] if row else None
        await self.conn.execute(
            "UPDATE orders SET prom_status=?, updated_at=? WHERE order_id=?",
            (new_status, now, str(order_id)),
        )
        await self.conn.execute(
            "INSERT INTO status_history(order_id, old_status, new_status, note, changed_at) VALUES (?, ?, ?, ?, ?)",
            (str(order_id), old_status, new_status, note, now),
        )
        await self.conn.commit()

    async def set_order_telegraph_url(self, order_id: str, telegraph_url: str):
        now = int(time.time())
        await self.conn.execute(
            "UPDATE orders SET telegraph_url=?, updated_at=? WHERE order_id=?",
            (str(telegraph_url or ""), now, str(order_id)),
        )
        await self.conn.commit()

    async def has_message(self, prom_message_id: str) -> bool:
        cur = await self.conn.execute(
            "SELECT 1 FROM seen_messages WHERE prom_message_id=?", (str(prom_message_id),)
        )
        row = await cur.fetchone()
        return row is not None

    async def add_message(self, prom_message_id: str, tg_message_id: int | None = None):
        await self.conn.execute(
            "INSERT OR IGNORE INTO seen_messages(prom_message_id, tg_message_id, created_at) VALUES (?, ?, ?)",
            (str(prom_message_id), tg_message_id, int(time.time())),
        )
        if tg_message_id:
            await self.conn.execute(
                "INSERT OR REPLACE INTO tg_message_map(tg_message_id, prom_message_id) VALUES (?, ?)",
                (tg_message_id, str(prom_message_id)),
            )
        await self.conn.commit()

    async def save_customer_message(self, message: dict[str, Any], summary: dict[str, Any], store_name: str, tg_message_id: int | None = None):
        now = int(time.time())
        mid = str(summary.get("message_id") or "")
        if not mid:
            return

        # Do not save fake/technical rows in the Messages menu:
        # - our own seller replies,
        # - rows with placeholder name like "Клієнт",
        # - empty context-only messages without text and without real attachments.
        try:
            fake_row = {
                "prom_message_id": mid,
                "client_name": summary.get("client_name"),
                "text": summary.get("text"),
                "raw_json": json.dumps(message or {}, ensure_ascii=False),
            }
            if not self._row_is_real_client_message(fake_row):
                await self.delete_customer_message(mid)
                await self.add_message(mid, tg_message_id)
                return
        except Exception:
            pass

        await self.conn.execute(
            """
            INSERT INTO customer_messages(
                prom_message_id, store_name, tg_message_id, message_date, status, client_name, phone, email,
                order_id, product_id, product_name, sku, product_url, text, raw_json, first_seen_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(prom_message_id) DO UPDATE SET
                store_name=excluded.store_name,
                tg_message_id=COALESCE(excluded.tg_message_id, customer_messages.tg_message_id),
                message_date=excluded.message_date,
                status=CASE
                    WHEN COALESCE(excluded.status, '') = '' THEN customer_messages.status
                    ELSE excluded.status
                END,
                client_name=excluded.client_name,
                phone=excluded.phone,
                email=excluded.email,
                order_id=excluded.order_id,
                product_id=excluded.product_id,
                product_name=excluded.product_name,
                sku=excluded.sku,
                product_url=excluded.product_url,
                text=excluded.text,
                raw_json=excluded.raw_json,
                updated_at=excluded.updated_at
            """,
            (
                mid,
                store_name,
                tg_message_id,
                str(summary.get("message_date") or ""),
                str(summary.get("status") or ""),
                str(summary.get("client_name") or ""),
                str(summary.get("phone") or ""),
                str(summary.get("email") or ""),
                str(summary.get("order_id") or ""),
                str(summary.get("product_id") or ""),
                str(summary.get("product_name") or ""),
                str(summary.get("sku") or ""),
                str(summary.get("product_url") or ""),
                str(summary.get("text") or ""),
                json.dumps(message, ensure_ascii=False),
                now,
                now,
            ),
        )
        await self.conn.commit()

    async def get_prom_message_by_tg_reply(self, tg_message_id: int) -> str | None:
        cur = await self.conn.execute(
            "SELECT prom_message_id FROM tg_message_map WHERE tg_message_id=?", (tg_message_id,)
        )
        row = await cur.fetchone()
        return row["prom_message_id"] if row else None

    async def get_last_orders(self, limit: int = 10, offset: int = 0):
        """Return orders from newest to oldest for the bottom “📦 Всі замовлення” menu.

        Prom order ids grow over time, while updated_at can change when we sync old
        orders. Sorting by numeric order_id first keeps the newest Prom order at the
        top and the oldest at the bottom.
        """
        cur = await self.conn.execute(
            """
            SELECT order_id, order_date, client_name, phone, total_price, prom_status, delivery_city, telegraph_url, updated_at
            FROM orders
            ORDER BY
                CASE
                    WHEN order_id GLOB '[0-9]*' THEN CAST(order_id AS INTEGER)
                    ELSE 0
                END DESC,
                updated_at DESC,
                first_seen_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return await cur.fetchall()

    async def count_orders(self) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) AS cnt FROM orders")
        row = await cur.fetchone()
        return int(row["cnt"] if row else 0)

    async def get_order_details(self, order_id: str):
        cur = await self.conn.execute("SELECT * FROM orders WHERE order_id=?", (str(order_id),))
        order = await cur.fetchone()
        cur_items = await self.conn.execute("SELECT * FROM order_items WHERE order_id=? ORDER BY row_id", (str(order_id),))
        items = await cur_items.fetchall()
        return order, items



    async def count_customer_messages(self) -> int:
        cur = await self.conn.execute("SELECT COUNT(*) AS cnt FROM customer_messages")
        row = await cur.fetchone()
        return int(row["cnt"] if row else 0)

    async def get_customer_messages_page(self, limit: int = 10, offset: int = 0):
        """Messages sorted as the user requested: unread first, then read, newest first in each group."""
        cur = await self.conn.execute(
            """
            SELECT rowid AS row_num,
                   prom_message_id, store_name, tg_message_id, message_date, status, client_name,
                   phone, email, order_id, product_id, product_name, sku, product_url, text,
                   first_seen_at, updated_at
            FROM customer_messages
            ORDER BY
                CASE
                    WHEN lower(COALESCE(status, '')) IN ('read', 'seen', 'viewed', 'прочитано', 'answered', 'replied', 'reply_sent', 'sent_reply') THEN 1
                    WHEN lower(COALESCE(status, '')) LIKE '%read%' THEN 1
                    WHEN lower(COALESCE(status, '')) LIKE '%seen%' THEN 1
                    WHEN lower(COALESCE(status, '')) LIKE '%view%' THEN 1
                    WHEN lower(COALESCE(status, '')) LIKE '%проч%' THEN 1
                    WHEN lower(COALESCE(status, '')) LIKE '%answer%' THEN 1
                    WHEN lower(COALESCE(status, '')) LIKE '%reply%' THEN 1
                    WHEN lower(COALESCE(status, '')) LIKE '%відпов%' THEN 1
                    ELSE 0
                END ASC,
                updated_at DESC,
                first_seen_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        return await cur.fetchall()

    async def get_customer_message_by_rowid(self, row_id: int | str):
        cur = await self.conn.execute(
            "SELECT rowid AS row_num, * FROM customer_messages WHERE rowid=?",
            (int(row_id),),
        )
        return await cur.fetchone()

    async def get_customer_message_by_prom_id(self, prom_message_id: str):
        cur = await self.conn.execute(
            "SELECT rowid AS row_num, * FROM customer_messages WHERE prom_message_id=?",
            (str(prom_message_id),),
        )
        return await cur.fetchone()

    async def map_tg_message_to_prom_message(self, tg_message_id: int, prom_message_id: str):
        """Force reply mapping for edited menu cards.

        Unlike add_message(), this does not depend on seen_messages and always makes
        the current Telegram message id point to the currently opened Prom message.
        """
        await self.conn.execute(
            "INSERT OR REPLACE INTO tg_message_map(tg_message_id, prom_message_id) VALUES (?, ?)",
            (int(tg_message_id), str(prom_message_id)),
        )
        await self.conn.commit()

    async def update_customer_message_status_local(self, prom_message_id: str, status: str = "read"):
        await self.conn.execute(
            "UPDATE customer_messages SET status=?, updated_at=? WHERE prom_message_id=?",
            (str(status), int(time.time()), str(prom_message_id)),
        )
        await self.conn.commit()


    async def get_all_customer_messages_raw(self):
        cur = await self.conn.execute(
            "SELECT prom_message_id, raw_json FROM customer_messages"
        )
        return await cur.fetchall()

    async def delete_customer_message(self, prom_message_id: str):
        await self.conn.execute(
            "DELETE FROM customer_messages WHERE prom_message_id=?",
            (str(prom_message_id),),
        )
        await self.conn.commit()

    async def update_customer_conversation_status_local(self, prom_message_id: str, status: str = "read"):
        """Mark all local messages from the same customer/thread as read.

        It still supports Prom chat room ids, but also groups by phone/email/name so
        one client does not stay red because an older row used another local key.
        """
        key = str(prom_message_id or "")
        now = int(time.time())

        # First: exact row -> thread identity by phone/email/client name/chat room.
        cur = await self.conn.execute(
            "SELECT rowid AS row_num, * FROM customer_messages WHERE prom_message_id=?",
            (key,),
        )
        selected = await cur.fetchone()
        if selected:
            thread_key = self._message_thread_key(selected)
            rows = await self._get_all_customer_message_rows_for_threads()
            ids = [str(row["prom_message_id"]) for row in rows if self._message_thread_key(row) == thread_key]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                await self.conn.execute(
                    f"UPDATE customer_messages SET status=?, updated_at=? WHERE prom_message_id IN ({placeholders})",
                    [str(status), now, *ids],
                )
                await self.conn.commit()
                return

        # Fallback: old Prom chat keys look like chat:<room_id>:<message_id>.
        if key.startswith("chat:"):
            parts = key.split(":", 2)
            if len(parts) >= 3 and parts[1]:
                pattern = f"chat:{parts[1]}:%"
                await self.conn.execute(
                    "UPDATE customer_messages SET status=?, updated_at=? WHERE prom_message_id LIKE ?",
                    (str(status), now, pattern),
                )
                await self.conn.commit()
                return
        await self.update_customer_message_status_local(key, status)



    # ---- Clean message threads / conversation list ----
    def _row_to_dict(self, row: Any) -> dict[str, Any]:
        if row is None:
            return {}
        if isinstance(row, dict):
            return dict(row)
        try:
            return {k: row[k] for k in row.keys()}
        except Exception:
            return {}

    def _message_is_read_value(self, status: Any) -> bool:
        value = str(status or "").strip().lower()
        if not value:
            return False
        if value in {"unread", "new", "нове", "непрочитано", "не прочитано"}:
            return False
        return (
            value in {"read", "seen", "viewed", "прочитано", "answered", "replied", "reply_sent", "sent_reply"}
            or "read" in value
            or "seen" in value
            or "view" in value
            or "проч" in value
            or "answer" in value
            or "reply" in value
            or "відпов" in value
            or "отвеч" in value
        )

    def _clean_msg_value(self, value: Any) -> str:
        text = str(value or "").strip()
        if text.lower() in {"none", "null", "undefined"}:
            return ""
        return text

    def _message_client_is_real_value(self, value: Any) -> bool:
        client = " ".join(self._clean_msg_value(value).split())
        low = client.lower()
        return bool(client) and low not in {
            "—", "-", "_", "клієнт", "клиент", "client", "customer", "buyer",
            "покупець", "покупатель", "unknown", "невідомо", "неизвестно",
        }

    def _boolish(self, value: Any) -> bool | None:
        if value is True:
            return True
        if value is False:
            return False
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value == 1:
                return True
            if value == 0:
                return False
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "y", "так", "да", "on"}:
            return True
        if text in {"0", "false", "no", "n", "ні", "нет", "off", "none", "null", ""}:
            return False
        return None

    def _raw_message_is_outgoing(self, raw: Any) -> bool:
        """Local DB-side outgoing filter so seller replies never appear in the client list."""
        if not isinstance(raw, dict):
            return False
        incoming_markers = {"in", "incoming", "inbound", "buyer", "client", "customer", "user", "visitor", "lead"}
        outgoing_markers = {"out", "outgoing", "outbound", "seller", "company", "shop", "store", "me", "owner", "operator", "manager", "admin", "merchant"}

        if "is_sender" in raw:
            b = self._boolish(raw.get("is_sender"))
            if b is not None:
                return b
        for key in ("is_outgoing", "outgoing", "from_company", "from_seller", "by_company", "is_shop", "is_operator", "is_manager", "is_admin", "is_mine", "is_my", "from_me", "mine"):
            if key in raw and self._boolish(raw.get(key)) is True:
                return True
        for key in ("direction", "message_direction", "sender_type", "author_type", "from_type", "side", "role", "sender_role", "author_role"):
            value = str(raw.get(key) or "").strip().lower()
            if value in incoming_markers:
                return False
            if value in outgoing_markers:
                return True
        for key in ("sender", "from", "author", "user", "from_user", "participant"):
            sender = raw.get(key)
            if not isinstance(sender, dict):
                continue
            for flag in ("is_sender", "is_seller", "is_company", "is_shop", "is_operator", "is_owner", "is_me", "me", "from_me", "mine"):
                if flag in sender and self._boolish(sender.get(flag)) is True:
                    return True
            for role_key in ("type", "role", "sender_type", "author_type", "from_type", "side", "kind"):
                value = str(sender.get(role_key) or "").strip().lower()
                if value in incoming_markers:
                    return False
                if value in outgoing_markers:
                    return True
        return False

    def _raw_message_has_attachment(self, raw: Any) -> bool:
        """True only for real client attachments, not product context thumbnails."""
        if not isinstance(raw, dict):
            return False
        attachment_keys = {
            "attachments", "attachment", "files", "file", "documents", "document",
            "photos", "photo", "media", "medias", "uploaded_files", "uploads",
        }
        for key, value in raw.items():
            if key in attachment_keys and value not in (None, "", [], {}):
                if key == "photo" and isinstance(value, str) and value == str(raw.get("context_item_image_url") or ""):
                    continue
                return True
        msg_type = str(raw.get("type") or raw.get("message_type") or raw.get("media_type") or "").strip().lower()
        if msg_type in {"photo", "image", "picture", "file", "document", "attachment", "pdf"}:
            url = str(raw.get("url") or raw.get("href") or raw.get("file_url") or raw.get("download_url") or "").strip()
            if url and url != str(raw.get("context_item_image_url") or ""):
                return True
        return False

    def _message_text_is_real_value(self, value: Any) -> bool:
        text = " ".join(self._clean_msg_value(value).split())
        return bool(text) and text not in {"—", "-", "_"}

    def _row_is_real_client_message(self, row: Any) -> bool:
        d = self._row_to_dict(row)
        if not self._message_client_is_real_value(d.get("client_name")):
            return False
        raw = {}
        try:
            raw_text = d.get("raw_json") or ""
            raw = json.loads(raw_text) if raw_text else {}
        except Exception:
            raw = {}
        if isinstance(raw, dict) and self._raw_message_is_outgoing(raw):
            return False
        return self._message_text_is_real_value(d.get("text")) or self._raw_message_has_attachment(raw)

    def _message_sort_ts(self, row: Any) -> float:
        d = self._row_to_dict(row)
        raw = self._clean_msg_value(d.get("message_date"))
        if raw:
            candidates = [raw.replace("Z", "+00:00")]
            # formatters.py often stores dates as: 06.07.2026 о 17:38
            candidates.append(raw.replace(" о ", " "))
            for candidate in candidates:
                try:
                    dt = datetime.fromisoformat(candidate)
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    return dt.timestamp()
                except Exception:
                    pass
                for fmt in ("%d.%m.%Y %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
                    try:
                        dt = datetime.strptime(candidate, fmt).replace(tzinfo=timezone.utc)
                        return dt.timestamp()
                    except Exception:
                        pass
        return float(d.get("first_seen_at") or d.get("updated_at") or 0)

    def _message_thread_key(self, row: Any) -> str:
        d = self._row_to_dict(row)
        # Групуємо насамперед по клієнту, щоб один і той самий клієнт не повторювався.
        # Якщо імені/телефону/пошти немає — fallback на Prom chat room.
        def clean(v: Any) -> str:
            text = str(v or "").strip()
            return "" if text in {"—", "None", "null"} else text

        phone = clean(d.get("phone"))
        email = clean(d.get("email")).lower()
        client = " ".join(clean(d.get("client_name")).lower().split())
        if phone:
            return "phone:" + phone
        if email:
            return "email:" + email
        if client:
            return "client:" + client

        mid = clean(d.get("prom_message_id"))
        if mid.startswith("chat:"):
            parts = mid.split(":", 2)
            if len(parts) >= 3 and parts[1]:
                return "chat:" + parts[1]
        return "message:" + mid

    def _message_order_key(self, row: Any) -> tuple[float, int, int]:
        d = self._row_to_dict(row)
        return (
            self._message_sort_ts(row),
            int(d.get("first_seen_at") or 0),
            int(d.get("row_num") or 0),
        )

    async def _get_all_customer_message_rows_for_threads(self):
        cur = await self.conn.execute(
            """
            SELECT rowid AS row_num,
                   prom_message_id, store_name, tg_message_id, message_date, status, client_name,
                   phone, email, order_id, product_id, product_name, sku, product_url, text,
                   first_seen_at, updated_at, raw_json
            FROM customer_messages
            ORDER BY first_seen_at DESC, updated_at DESC, rowid DESC
            """
        )
        rows = await cur.fetchall()
        rows = [row for row in rows if self._row_is_real_client_message(row)]
        rows.sort(key=self._message_sort_ts, reverse=True)
        return rows

    async def get_customer_message_threads_page(self, limit: int = 10, offset: int = 0):
        """One button per real client. Newest client conversation first, oldest last."""
        rows = await self._get_all_customer_message_rows_for_threads()
        grouped: dict[str, list[Any]] = {}
        for row in rows:
            grouped.setdefault(self._message_thread_key(row), []).append(row)

        threads: list[dict[str, Any]] = []
        for key, items in grouped.items():
            items_sorted = sorted(items, key=self._message_order_key, reverse=True)
            latest = self._row_to_dict(items_sorted[0])
            unread_count = sum(1 for item in items_sorted if not self._message_is_read_value(self._row_to_dict(item).get("status")))
            latest["thread_key"] = key
            latest["thread_total"] = len(items_sorted)
            latest["unread_count"] = unread_count
            latest["status"] = "unread" if unread_count else "read"
            latest["sort_ts"] = self._message_sort_ts(items_sorted[0])
            threads.append(latest)

        threads.sort(key=lambda d: (float(d.get("sort_ts") or 0), int(d.get("first_seen_at") or 0), int(d.get("row_num") or 0)), reverse=True)
        return threads[int(offset or 0): int(offset or 0) + int(limit or 10)]

    async def count_customer_message_threads(self) -> int:
        rows = await self._get_all_customer_message_rows_for_threads()
        return len({self._message_thread_key(row) for row in rows})

    async def get_customer_message_thread_rows_for_rowid(self, row_id: int | str):
        selected = await self.get_customer_message_by_rowid(row_id)
        if not selected:
            return [], None, -1
        key = self._message_thread_key(selected)
        rows = await self._get_all_customer_message_rows_for_threads()
        thread_rows = [row for row in rows if self._message_thread_key(row) == key]
        thread_rows.sort(key=self._message_order_key, reverse=True)
        current_index = -1
        for idx, row in enumerate(thread_rows):
            try:
                if int(row["row_num"]) == int(row_id):
                    current_index = idx
                    break
            except Exception:
                continue
        return thread_rows, selected, current_index

    async def get_customer_message_navigation(self, row_id: int | str) -> dict[str, Any]:
        rows, selected, index = await self.get_customer_message_thread_rows_for_rowid(row_id)
        if not selected or index < 0:
            return {"total": 0, "index": -1, "newer_row_id": None, "older_row_id": None}
        newer_row_id = rows[index - 1]["row_num"] if index > 0 else None
        older_row_id = rows[index + 1]["row_num"] if index + 1 < len(rows) else None
        return {
            "total": len(rows),
            "index": index,
            "newer_row_id": newer_row_id,
            "older_row_id": older_row_id,
        }

    async def export_orders_csv(self, folder: str = "exports") -> tuple[str, str]:
        folder_path = Path(folder)
        folder_path.mkdir(exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S")
        orders_path = folder_path / f"prom_orders_{ts}.csv"
        items_path = folder_path / f"prom_order_items_{ts}.csv"

        cur = await self.conn.execute(
            """
            SELECT order_id, store_name, prom_status, order_date, client_name, phone, email,
                   total_price, payment, delivery, delivery_provider, delivery_city,
                   delivery_warehouse, delivery_address, comment, order_url, telegraph_url, first_seen_at, updated_at
            FROM orders
            ORDER BY updated_at DESC
            """
        )
        orders = await cur.fetchall()
        with orders_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([c for c in orders[0].keys()] if orders else [
                "order_id", "store_name", "prom_status", "order_date", "client_name", "phone", "email",
                "total_price", "payment", "delivery", "delivery_provider", "delivery_city",
                "delivery_warehouse", "delivery_address", "comment", "order_url", "telegraph_url", "first_seen_at", "updated_at"
            ])
            for row in orders:
                writer.writerow([row[k] for k in row.keys()])

        cur = await self.conn.execute(
            """
            SELECT order_id, product_id, product_name, sku, options_text, quantity, price, total_price, product_url
            FROM order_items
            ORDER BY order_id DESC, row_id ASC
            """
        )
        items = await cur.fetchall()
        with items_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([c for c in items[0].keys()] if items else [
                "order_id", "product_id", "product_name", "sku", "options_text", "quantity", "price", "total_price", "product_url"
            ])
            for row in items:
                writer.writerow([row[k] for k in row.keys()])

        return str(orders_path), str(items_path)
