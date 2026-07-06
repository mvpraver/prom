from __future__ import annotations

import asyncio
import logging
import os
import json
import hashlib
from pathlib import Path
from typing import Any
from datetime import datetime, timezone, timedelta
from html import escape as html_escape

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, FSInputFile, InputMediaPhoto, InputMediaDocument
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

from db import DB
from formatters import (
    format_order,
    format_prom_message,
    format_order_from_db,
    format_order_short,
    format_order_short_from_db,
    format_orders_page,
    format_messages_page,
    strip_html,
    human_status,
    telegraph_nodes_from_order,
    telegraph_nodes_from_db,
    telegraph_title_from_summary,
    val,
    extract_order_summary,
    extract_message_summary,
    extract_message_attachments,
    is_outgoing_message,
)
from keyboards import order_keyboard, orders_page_keyboard, messages_page_keyboard, message_detail_keyboard, main_menu_keyboard
from prom_api import PromClient, PromAPIError
from telegraph_api import TelegraphClient, TelegraphError

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("prom-tg-bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
PROM_TOKEN = os.getenv("PROM_API_TOKEN", "").strip()
def parse_admin_chat_ids() -> list[int]:
    """ADMIN_CHAT_IDS supports several admins: 123,456,789.

    ADMIN_CHAT_ID is still supported for old .env files.
    """
    raw = (os.getenv("ADMIN_CHAT_IDS", "") or "").strip()
    if not raw:
        raw = (os.getenv("ADMIN_CHAT_ID", "") or "").strip()
    ids: list[int] = []
    for part in raw.replace(";", ",").replace(" ", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part)
        except ValueError:
            continue
        if value and value not in ids:
            ids.append(value)
    return ids


ADMIN_CHAT_IDS = parse_admin_chat_ids()
ADMIN_CHAT_ID = ADMIN_CHAT_IDS[0] if ADMIN_CHAT_IDS else 0  # first admin, for backward compatibility
ADMIN_CHAT_ID_SET = set(ADMIN_CHAT_IDS)
STORE_NAME = os.getenv("STORE_NAME", "Prom магазин").strip()
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "20") or "20")
SEND_EXISTING_ON_START = os.getenv("SEND_EXISTING_ON_START", "false").lower() in {"1", "true", "yes", "y"}
TG_TEXT_LIMIT = 3900
ORDERS_PAGE_LIMIT = int(os.getenv("ORDERS_PAGE_LIMIT", "10") or "10")
MESSAGES_PAGE_LIMIT = int(os.getenv("MESSAGES_PAGE_LIMIT", "10") or "10")
TELEGRAPH_ENABLED = os.getenv("TELEGRAPH_ENABLED", "true").lower() in {"1", "true", "yes", "y"}
TELEGRAPH_ACCESS_TOKEN = os.getenv("TELEGRAPH_ACCESS_TOKEN", "").strip()
# ВАЖЛИВО: старі версії ховали всі повідомлення при старті, якщо SEND_EXISTING_ON_START=false.
# Тут за замовчуванням повідомлення НЕ бутстрапляться, щоб клієнтські чати не пропадали.
BOOTSTRAP_MESSAGES_ON_START = os.getenv("BOOTSTRAP_MESSAGES_ON_START", "false").lower() in {"1", "true", "yes", "y"}
SKIP_OUTGOING_MESSAGES = os.getenv("SKIP_OUTGOING_MESSAGES", "true").lower() in {"1", "true", "yes", "y"}
MESSAGE_LIST_LIMIT = int(os.getenv("MESSAGE_LIST_LIMIT", "50") or "50")
RECENT_ORDER_MINUTES = int(os.getenv("RECENT_ORDER_MINUTES", "360") or "360")
BOT_BUILD_VERSION = "v35_orders_newest_reply_fix"

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_BOT_TOKEN is empty. Fill .env")
if not PROM_TOKEN:
    raise RuntimeError("PROM_API_TOKEN is empty. Fill .env")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
db = DB(os.getenv("DB_PATH", "bot.db"))


def is_admin(user_id: int | None) -> bool:
    if not ADMIN_CHAT_IDS:
        return True  # Useful only for initial /id setup. Set ADMIN_CHAT_IDS in production.
    if user_id is None:
        return False
    return int(user_id) in ADMIN_CHAT_ID_SET


async def notify_admins(text: str, **kwargs) -> list[Message]:
    """Send a service notification to every admin. Returns successfully sent Telegram messages."""
    sent: list[Message] = []
    for chat_id in ADMIN_CHAT_IDS:
        try:
            msg = await bot.send_message(chat_id, text, **kwargs)
            sent.append(msg)
        except Exception:
            log.exception("Failed to notify admin %s", chat_id)
    return sent


def safe_filename(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "_-" else "_" for ch in str(value))[:80] or "order"


def write_order_txt(order_id: str, html_text: str) -> str:
    folder = Path("exports") / "orders"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"order_{safe_filename(order_id)}.txt"
    path.write_text(strip_html(html_text), encoding="utf-8")
    return str(path)


def write_json_debug(filename: str, data: Any) -> str:
    folder = Path("exports") / "debug"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / safe_filename(filename)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return str(path)


async def _send_attachment_as_telegram_message(chat_id: int, att: dict, *, caption: str | None = None, reply_to_message_id: int | None = None) -> Message:
    """Send one Prom attachment to Telegram as a real photo/document."""
    url = str(att.get("url") or "").strip()
    kind = str(att.get("kind") or "file")
    name = str(att.get("name") or ("photo" if kind == "photo" else "file")).strip() or "file"
    if not url:
        raise ValueError("Attachment URL is empty")

    if kind == "photo":
        return await bot.send_photo(
            chat_id,
            photo=url,
            caption=caption,
            reply_to_message_id=reply_to_message_id,
        )

    return await bot.send_document(
        chat_id,
        document=url,
        caption=caption,
        reply_to_message_id=reply_to_message_id,
    )


def _attachment_media_groups(attachments: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    photos: list[dict] = []
    docs: list[dict] = []
    other: list[dict] = []
    for att in attachments:
        kind = str(att.get("kind") or "file")
        url = str(att.get("url") or "").strip()
        if not url:
            continue
        if kind == "photo":
            photos.append(att)
        elif kind == "pdf":
            docs.append(att)
        else:
            other.append(att)
    return photos, docs, other


async def _send_photo_group(chat_id: int, photos: list[dict], *, reply_to_message_id: int | None = None) -> list[Message]:
    """Send photos as one Telegram album when possible."""
    if not photos:
        return []
    if len(photos) == 1:
        return [await _send_attachment_as_telegram_message(chat_id, photos[0], reply_to_message_id=reply_to_message_id)]

    media = [InputMediaPhoto(media=str(att.get("url") or "")) for att in photos[:10]]
    return await bot.send_media_group(chat_id, media=media, reply_to_message_id=reply_to_message_id)


async def _send_document_group(chat_id: int, docs: list[dict], *, reply_to_message_id: int | None = None) -> list[Message]:
    """Send PDF/documents as one Telegram media group when possible."""
    if not docs:
        return []
    if len(docs) == 1:
        return [await _send_attachment_as_telegram_message(chat_id, docs[0], reply_to_message_id=reply_to_message_id)]

    media = []
    for att in docs[:10]:
        url = str(att.get("url") or "").strip()
        media.append(InputMediaDocument(media=url))
    return await bot.send_media_group(chat_id, media=media, reply_to_message_id=reply_to_message_id)


async def send_prom_message_to_telegram(chat_id: int, prom_message_id: str, prom_message: dict) -> int:
    """Send Prom message card first, then send all attachments below it.

    Photos are grouped as an album. PDFs/documents are grouped as a document album
    when Telegram accepts it. If the client sent only attachments, the message text
    is still shown as a dash in the main card.
    """
    text = format_prom_message(prom_message, STORE_NAME)
    attachments = extract_message_attachments(prom_message)

    primary_message = await bot.send_message(chat_id, text, disable_web_page_preview=True)
    await db.add_message(str(prom_message_id), primary_message.message_id)

    if not attachments:
        return primary_message.message_id

    photos, docs, other = _attachment_media_groups(attachments)

    async def _map_sent(messages: list[Message]) -> None:
        for sent in messages:
            await db.add_message(str(prom_message_id), sent.message_id)

    try:
        await _map_sent(await _send_photo_group(chat_id, photos, reply_to_message_id=primary_message.message_id))
    except Exception as e:
        log.warning("Failed to send photo group from Prom message %s: %s", prom_message_id, e)
        for att in photos:
            try:
                sent = await _send_attachment_as_telegram_message(chat_id, att, reply_to_message_id=primary_message.message_id)
                await db.add_message(str(prom_message_id), sent.message_id)
            except Exception as ee:
                log.warning("Failed to send photo attachment from Prom message %s: %s", prom_message_id, ee)

    try:
        await _map_sent(await _send_document_group(chat_id, docs, reply_to_message_id=primary_message.message_id))
    except Exception as e:
        log.warning("Failed to send document group from Prom message %s: %s", prom_message_id, e)
        for att in docs:
            try:
                sent = await _send_attachment_as_telegram_message(chat_id, att, reply_to_message_id=primary_message.message_id)
                await db.add_message(str(prom_message_id), sent.message_id)
            except Exception as ee:
                log.warning("Failed to send PDF/document attachment from Prom message %s: %s", prom_message_id, ee)

    for att in other[:10]:
        try:
            sent = await _send_attachment_as_telegram_message(chat_id, att, reply_to_message_id=primary_message.message_id)
            await db.add_message(str(prom_message_id), sent.message_id)
        except Exception as e:
            log.warning("Failed to send other attachment from Prom message %s: %s", prom_message_id, e)

    return primary_message.message_id


async def send_prom_message_to_admins(prom_message_id: str, prom_message: dict) -> int | None:
    """Send a new Prom message to every admin and map every Telegram message id for replies."""
    first_tg_message_id: int | None = None
    for chat_id in ADMIN_CHAT_IDS:
        try:
            tg_message_id = await send_prom_message_to_telegram(chat_id, prom_message_id, prom_message)
            if first_tg_message_id is None:
                first_tg_message_id = tg_message_id
        except Exception:
            log.exception("Failed to send Prom message %s to admin %s", prom_message_id, chat_id)
    return first_tg_message_id


def prom_message_key(message: dict) -> str:
    """Stable key for seen_messages and reply mapping.

    Old Prom messages use /messages/{id}. New Prom chat messages use room id, so their
    reply key is chat:<room_id>:<message_id-or-hash>. PromClient.reply_message knows how to
    send chat replies back through /chat/send_message.
    """
    if isinstance(message, dict) and message.get("__prom_channel") == "chat":
        room_id = val(message, "__chat_room_id", "room_id", "chat_id", "dialog_id", default="")
        msg_id = val(message, "id", "message_id", "chat_message_id", "msg_id", "uuid", default="")
        if room_id:
            if msg_id:
                return f"chat:{room_id}:{msg_id}"
            raw = json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)
            return f"chat:{room_id}:hash_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

    real_id = val(message, "id", "message_id", "chat_message_id", "msg_id", default="")
    if real_id:
        return str(real_id)
    raw = json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)
    return "hash_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]


def is_chat_message_key(message_key: str) -> bool:
    return str(message_key).startswith("chat:")


def has_real_prom_message_id(message_key: str) -> bool:
    return is_chat_message_key(message_key) or not str(message_key).startswith("hash_")



def _parse_prom_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def message_timestamp(message: dict) -> float:
    dt = _parse_prom_datetime(
        val(message, "date_sent", "date_created", "created_at", "date", "datetime", "created", "sent_at", "date_sent", default="")
    )
    if dt:
        return dt.timestamp()
    try:
        return float(val(message, "id", "message_id", "chat_message_id", default=0) or 0)
    except Exception:
        return 0.0


def message_room_id(message: dict, message_key: str | None = None) -> str:
    room = str(val(message, "__chat_room_id", "room_id", "chat_id", "dialog_id", default="") or "").strip()
    if room:
        return room
    key = str(message_key or "")
    if key.startswith("chat:"):
        parts = key.split(":", 2)
        if len(parts) >= 3:
            return parts[1]
    return ""


def _status_is_read(value: Any) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return False
    if text in {"unread", "new", "нове", "непрочитано", "не прочитано"}:
        return False
    return (
        text in {"read", "seen", "viewed", "прочитано", "answered", "replied", "reply_sent", "sent_reply"}
        or "read" in text
        or "seen" in text
        or "view" in text
        or "проч" in text
        or "answer" in text
        or "reply" in text
        or "відпов" in text
        or "отвеч" in text
    )


def build_message_status_overrides(messages: list[dict]) -> dict[str, str]:
    """Return local statuses for incoming messages.

    A client message is green/read when Prom says it is read OR when there is any
    newer seller/shop message in the same Prom chat room. That fixes the case where
    Prom gives old rows as unread even though we already replied.
    """
    outgoing_by_room: dict[str, list[float]] = {}
    for m in messages:
        key = prom_message_key(m)
        room = message_room_id(m, key)
        if not room:
            continue
        if is_outgoing_message(m):
            outgoing_by_room.setdefault(room, []).append(message_timestamp(m))

    overrides: dict[str, str] = {}
    for m in messages:
        key = prom_message_key(m)
        if is_outgoing_message(m):
            continue
        raw_status = val(m, "status", default="")
        room = message_room_id(m, key)
        ts = message_timestamp(m)
        answered_after = any(out_ts >= ts for out_ts in outgoing_by_room.get(room, [])) if room else False
        overrides[str(key)] = "read" if _status_is_read(raw_status) or answered_after else "unread"
    return overrides


async def cleanup_outgoing_customer_messages() -> int:
    """Remove old local rows where our own Prom replies were saved as client messages."""
    removed = 0
    try:
        rows = await db.get_all_customer_messages_raw()
    except Exception:
        return 0
    for row in rows:
        try:
            raw_text = row["raw_json"] if "raw_json" in row.keys() else ""
            raw = json.loads(raw_text) if raw_text else {}
            if isinstance(raw, dict) and is_outgoing_message(raw):
                await db.delete_customer_message(str(row["prom_message_id"]))
                removed += 1
        except Exception:
            continue
    return removed

def is_recent_order_for_notification(order: dict) -> bool:
    """If an order was saved to seen_orders without TG id, still send it if it is fresh."""
    raw = str(val(order, "date_created", "created_at", "date", default="") or "").strip()
    if not raw:
        return False
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            # Prom API dates without timezone are UTC in chat/order payloads.
            dt = dt.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - dt.astimezone(timezone.utc)
        return timedelta(seconds=0) <= age <= timedelta(minutes=RECENT_ORDER_MINUTES)
    except Exception:
        return False


async def create_telegraph_page_for_order(order: dict, order_id: str | int) -> str:
    if not TELEGRAPH_ENABLED:
        return ""
    try:
        summary = extract_order_summary(order)
        client = TelegraphClient(token=TELEGRAPH_ACCESS_TOKEN, author_name=STORE_NAME)
        title = telegraph_title_from_summary(summary)
        nodes = telegraph_nodes_from_order(order, STORE_NAME)
        return await client.create_page(title, nodes, author_name=STORE_NAME)
    except Exception as e:
        log.exception("Failed to create Telegraph page for order %s", order_id)
        if ADMIN_CHAT_IDS:
            await notify_admins(
                f"⚠️ Не вдалось створити Telegraph для замовлення <code>{order_id}</code>: <code>{str(e)[:1200]}</code>"
            )
        return ""


async def create_telegraph_page_for_db_order(order_id: str) -> str:
    order, items = await db.get_order_details(order_id)
    if not order:
        return ""
    existing = str(order["telegraph_url"] or "") if "telegraph_url" in order.keys() else ""
    if existing:
        return existing
    if not TELEGRAPH_ENABLED:
        return ""
    try:
        title = f"Prom замовлення № {order_id}"[:80]
        nodes = telegraph_nodes_from_db(order, items)
        client = TelegraphClient(token=TELEGRAPH_ACCESS_TOKEN, author_name=STORE_NAME)
        url = await client.create_page(title, nodes, author_name=STORE_NAME)
        await db.set_order_telegraph_url(order_id, url)
        return url
    except Exception as e:
        log.exception("Failed to create Telegraph page from db for order %s", order_id)
        return ""


DELIVERED_STATUS_VALUES = {
    "delivered",
    "done",
    "completed",
    "complete",
    "fulfilled",
    "success",
    "виконано",
    "виконаний",
    "выполнен",
    "выполнено",
    "виконане",
}


def order_status_is_delivered(status: Any) -> bool:
    text = str(status or "").strip().lower()
    if not text:
        return False
    if text in DELIVERED_STATUS_VALUES:
        return True
    return any(marker in text for marker in ("delivered", "completed", "fulfilled", "викон", "выполн"))


async def block_if_order_already_delivered(order_id: str) -> bool:
    """Return True if order status is already delivered and buttons must be blocked."""
    local_status = ""
    try:
        if db.conn:
            order_row, _items = await db.get_order_details(order_id)
            if order_row:
                local_status = str(order_row["prom_status"] or "") if "prom_status" in order_row.keys() else ""
                if order_status_is_delivered(local_status):
                    return True
    except Exception:
        log.exception("Failed to read local order status before changing status")

    # Local DB can be stale, so ask Prom before changing the status.
    try:
        async with PromClient(PROM_TOKEN) as prom:
            order = await prom.get_order(order_id)
        prom_status = val(order, "status", default="")
        prom_status_name = val(order, "status_name", default="")
        if order_status_is_delivered(prom_status) or order_status_is_delivered(prom_status_name):
            try:
                if db.conn and not order_status_is_delivered(local_status):
                    await db.update_order_status_local(order_id, "delivered", "status locked: order is already delivered in Prom")
            except Exception:
                log.exception("Failed to update local delivered status after Prom check")
            return True
    except Exception:
        # If Prom check fails, still rely on local DB. The actual Prom request below will fail/allow naturally.
        log.exception("Failed to verify order status in Prom before changing status")

    return False


async def send_order_to_admin(order: dict, order_id: str | int):
    """Send compact Telegram card + Telegraph page with full order details to every admin."""
    telegraph_url = await create_telegraph_page_for_order(order, order_id)
    short_text = format_order_short(order, STORE_NAME)
    if not telegraph_url:
        short_text += "\n\n⚠️ Telegraph не створився, тому повна версія буде файлом, якщо не влізе в Telegram."

    first_msg: Message | None = None
    for chat_id in ADMIN_CHAT_IDS:
        try:
            tg_msg = await bot.send_message(
                chat_id,
                short_text,
                reply_markup=order_keyboard(order_id, telegraph_url),
                disable_web_page_preview=True,
            )
            if first_msg is None:
                first_msg = tg_msg
        except Exception:
            log.exception("Failed to send order %s to admin %s", order_id, chat_id)

    # Fallback only if Telegraph failed.
    if not telegraph_url:
        full_text = format_order(order, STORE_NAME)
        for chat_id in ADMIN_CHAT_IDS:
            try:
                if len(full_text) > TG_TEXT_LIMIT:
                    path = write_order_txt(str(order_id), full_text)
                    await bot.send_document(
                        chat_id,
                        FSInputFile(path),
                        caption=f"📄 Повна версія замовлення № {order_id}",
                    )
                else:
                    await bot.send_message(chat_id, full_text, disable_web_page_preview=True)
            except Exception:
                log.exception("Failed to send order fallback %s to admin %s", order_id, chat_id)

    if first_msg is None:
        raise RuntimeError("Order was not sent to any admin")
    return first_msg, telegraph_url


async def send_order_details(chat_id: int, order_id: str):
    order, items = await db.get_order_details(order_id)
    if not order:
        await bot.send_message(chat_id, "Замовлення не знайдено в локальній базі.")
        return

    telegraph_url = str(order["telegraph_url"] or "") if "telegraph_url" in order.keys() else ""
    if not telegraph_url:
        telegraph_url = await create_telegraph_page_for_db_order(order_id)

    # /orders opens the same compact operational card as a new-order notification.
    # Full order data stays in Telegraph via the button below.
    text = format_order_short_from_db(order, items, STORE_NAME)
    if not telegraph_url:
        text += "\n\n⚠️ Telegraph не створився. Спробуй відкрити ще раз або перевір інтернет/доступ Telegraph."

    await bot.send_message(
        chat_id,
        text,
        reply_markup=order_keyboard(order_id, telegraph_url, show_db_button=False),
        disable_web_page_preview=True,
    )


async def send_orders_page(chat_id: int, offset: int = 0, edit_message: Message | None = None):
    offset = max(0, int(offset or 0))
    total = await db.count_orders()
    rows = await db.get_last_orders(ORDERS_PAGE_LIMIT, offset)
    text = format_orders_page(rows, offset, total)
    kb = orders_page_keyboard(rows, offset, ORDERS_PAGE_LIMIT, total)
    if edit_message:
        await edit_message.edit_text(text, reply_markup=kb)
    else:
        await bot.send_message(chat_id, text, reply_markup=kb)


async def send_messages_page(chat_id: int, offset: int = 0, edit_message: Message | None = None):
    offset = max(0, int(offset or 0))
    total = await db.count_customer_message_threads()
    rows = await db.get_customer_message_threads_page(MESSAGES_PAGE_LIMIT, offset)
    text = format_messages_page(rows, offset, total)
    kb = messages_page_keyboard(rows, offset, MESSAGES_PAGE_LIMIT, total)
    if edit_message:
        await edit_message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    else:
        await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)


def _raw_from_customer_message_row(row) -> dict:
    raw: dict[str, Any] = {}
    try:
        raw_text = row["raw_json"] if "raw_json" in row.keys() else ""
        raw = json.loads(raw_text) if raw_text else {}
    except Exception:
        raw = {}

    if not isinstance(raw, dict) or not raw:
        raw = {
            "id": row["prom_message_id"],
            "date_created": row["message_date"],
            "status": row["status"],
            "user_name": row["client_name"],
            "phone": row["phone"],
            "email": row["email"],
            "order_id": row["order_id"],
            "product_id": row["product_id"],
            "product_name": row["product_name"],
            "sku": row["sku"],
            "product_url": row["product_url"],
            "text": row["text"],
        }
    return raw


async def _send_raw_attachments_below(chat_id: int, prom_message_id: str, raw: dict, reply_to_message_id: int | None = None) -> None:
    attachments = extract_message_attachments(raw)
    if not attachments:
        return

    photos, docs, other = _attachment_media_groups(attachments)

    async def _map_sent(messages: list[Message]) -> None:
        for sent in messages:
            await db.add_message(str(prom_message_id), sent.message_id)

    try:
        await _map_sent(await _send_photo_group(chat_id, photos, reply_to_message_id=reply_to_message_id))
    except Exception as e:
        log.warning("Failed to send message history photo group %s: %s", prom_message_id, e)
        for att in photos:
            try:
                sent = await _send_attachment_as_telegram_message(chat_id, att, reply_to_message_id=reply_to_message_id)
                await db.add_message(str(prom_message_id), sent.message_id)
            except Exception as ee:
                log.warning("Failed to send message history photo %s: %s", prom_message_id, ee)

    try:
        await _map_sent(await _send_document_group(chat_id, docs, reply_to_message_id=reply_to_message_id))
    except Exception as e:
        log.warning("Failed to send message history document group %s: %s", prom_message_id, e)
        for att in docs:
            try:
                sent = await _send_attachment_as_telegram_message(chat_id, att, reply_to_message_id=reply_to_message_id)
                await db.add_message(str(prom_message_id), sent.message_id)
            except Exception as ee:
                log.warning("Failed to send message history document %s: %s", prom_message_id, ee)

    for att in other[:10]:
        try:
            sent = await _send_attachment_as_telegram_message(chat_id, att, reply_to_message_id=reply_to_message_id)
            await db.add_message(str(prom_message_id), sent.message_id)
        except Exception as e:
            log.warning("Failed to send message history attachment %s: %s", prom_message_id, e)


async def show_customer_message_from_db(chat_id: int, row_id: str | int, *, edit_message: Message | None = None, send_attachments: bool = False):
    row = await db.get_customer_message_by_rowid(row_id)
    if not row:
        if edit_message:
            await edit_message.edit_text("Повідомлення не знайдено в локальній базі.")
        else:
            await bot.send_message(chat_id, "Повідомлення не знайдено в локальній базі.")
        return

    raw = _raw_from_customer_message_row(row)
    prom_message_id = str(row["prom_message_id"])
    navigation = await db.get_customer_message_navigation(row_id)
    text = format_prom_message(raw, STORE_NAME, is_new=False)

    total = int(navigation.get("total") or 0)
    index = int(navigation.get("index") or 0)
    if total > 1:
        text += f"\n\n📍 Повідомлення <b>{index + 1}</b> з <b>{total}</b> у цього клієнта."

    kb = message_detail_keyboard(row_id, navigation)

    if edit_message:
        await edit_message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
        # This is the key fix for replies from the “💬 Повідомлення” menu:
        # the list message is edited into a message card, so we must force this
        # Telegram message id to point to the currently opened Prom chat/message.
        await db.map_tg_message_to_prom_message(edit_message.message_id, prom_message_id)
        await db.add_message(prom_message_id, edit_message.message_id)
        tg_message_id = edit_message.message_id
    else:
        sent = await bot.send_message(chat_id, text, reply_markup=kb, disable_web_page_preview=True)
        await db.map_tg_message_to_prom_message(sent.message_id, prom_message_id)
        await db.add_message(prom_message_id, sent.message_id)
        tg_message_id = sent.message_id

    if send_attachments:
        await _send_raw_attachments_below(chat_id, prom_message_id, raw, reply_to_message_id=tg_message_id)


async def send_customer_message_from_db(chat_id: int, row_id: str | int):
    # Compatibility for old handlers/commands: sends a separate card.
    await show_customer_message_from_db(chat_id, row_id, edit_message=None, send_attachments=True)


@dp.message(Command("start"))
async def cmd_start(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "Бот запущений.\n\n"
        "• Нові замовлення будуть приходити сюди з кнопками статусів.\n"
        "• Нові повідомлення будуть приходити сюди. Щоб відповісти клієнту — зроби reply на повідомлення бота.\n"
        "• Знизу є кнопка <b>📦 Всі замовлення</b> — відкриває базу замовлень з кнопками.\n"
        "• /id — покаже твій Telegram ID.\n"
        "• /sync_orders — підтягнути/оновити замовлення з Prom у базу.\n"
        "• /export_orders — експорт замовлень у CSV.\n"
        "• /debug_messages — діагностика чату Prom.\n"
        "• /debug_orders — діагностика замовлень Prom.",
        reply_markup=main_menu_keyboard(),
    )


@dp.message(Command("id"))
async def cmd_id(message: Message):
    await message.answer(f"Твій Telegram ID: <code>{message.from_user.id}</code>")


@dp.message(Command("check"))
async def cmd_check(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer("Перевіряю Prom вручну…")
    async with PromClient(PROM_TOKEN) as prom:
        orders_sent = await poll_orders(prom, send_to_tg=True)
        messages_sent = await poll_messages(prom, send_to_tg=True)
    await message.answer(f"Готово. Нових замовлень: {orders_sent}, нових повідомлень: {messages_sent}")






@dp.message(Command("debug_messages"))
async def cmd_debug_messages(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer("Діагностика Prom-повідомлень і Prom-чату…")
    try:
        async with PromClient(PROM_TOKEN) as prom:
            raw = await prom.raw_messages_response()
            combined = await prom.get_messages(limit=MESSAGE_LIST_LIMIT)
        legacy = raw.get("legacy_messages_list") if isinstance(raw, dict) else None
        rooms = raw.get("chat_rooms") if isinstance(raw, dict) else None
        chat_messages = raw.get("chat_messages") if isinstance(raw, dict) else None

        legacy_count = len(legacy.get("messages", [])) if isinstance(legacy, dict) and isinstance(legacy.get("messages"), list) else (len(legacy) if isinstance(legacy, list) else 0)
        rooms_count = len(rooms) if isinstance(rooms, list) else 0
        chat_count = len(chat_messages) if isinstance(chat_messages, list) else 0

        await message.answer(
            "📊 <b>Діагностика повідомлень</b>\n"
            f"/messages/list: <b>{legacy_count}</b>\n"
            f"/chat/rooms: <b>{rooms_count}</b>\n"
            f"/chat/messages_history: <b>{chat_count}</b>\n"
            f"Разом для відправки в Telegram: <b>{len(combined)}</b>"
        )

        path = write_json_debug("prom_messages_and_chat_raw.json", {"raw": raw, "combined": combined[:50]})
        await message.answer_document(FSInputFile(path), caption="📄 Повний raw JSON повідомлень і чату")

        if not combined:
            await message.answer(
                "Prom API не віддав жодного повідомлення/чату. Перевір у токені саме блок <b>Чат = читання і запис</b> і що ти тестуєш той самий магазин."
            )
            return

        sample = json.dumps(combined[:3], ensure_ascii=False, indent=2)[:3000]
        await message.answer("Перші знайдені повідомлення:\n<code>" + html_escape(sample) + "</code>")
    except Exception as e:
        log.exception("debug_messages failed")
        await message.answer(f"❌ Debug messages помилка:\n<code>{html_escape(str(e)[:3500])}</code>")

@dp.message(Command("debug_chat_endpoints"))
async def cmd_debug_chat_endpoints(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer("Перевіряю можливі endpoints для чату…")
    try:
        async with PromClient(PROM_TOKEN) as prom:
            data = await prom.probe_chat_endpoints()
        lines = ["🧪 <b>Chat endpoints debug</b>"]
        for path, info in data.items():
            if not isinstance(info, dict):
                lines.append(f"ℹ️ <code>{html_escape(str(path))}</code> — {html_escape(str(info)[:120])}")
            elif info.get("ok"):
                count = info.get("message_count", info.get("room_count", info.get("count")))
                lines.append(f"✅ <code>{html_escape(str(path))}</code> — count: <b>{html_escape(str(count))}</b>")
            else:
                lines.append(f"❌ <code>{html_escape(str(path))}</code> — {html_escape(str(info.get('error'))[:180])}")
        await message.answer("\n".join(lines[:30]))
        path = write_json_debug("prom_chat_endpoints_debug.json", data)
        await message.answer_document(FSInputFile(path), caption="📄 Повний debug endpoints")
    except Exception as e:
        log.exception("debug_chat_endpoints failed")
        await message.answer(f"❌ Debug endpoints помилка: <code>{str(e)[:3500]}</code>")


@dp.message(Command("reset_messages"))
async def cmd_reset_messages(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    if not db.conn:
        await message.answer("База ще не підключена.")
        return
    await db.conn.execute("DELETE FROM seen_messages")
    await db.conn.execute("DELETE FROM tg_message_map")
    await db.conn.commit()
    await message.answer(
        "✅ Памʼять про вже оброблені повідомлення очищена. Тепер напиши /check — бот спробує прислати повідомлення з Prom заново."
    )


@dp.message(Command("force_messages"))
async def cmd_force_messages(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer("Примусово тягну повідомлення з Prom, навіть якщо вони вже були позначені в базі…")
    try:
        async with PromClient(PROM_TOKEN) as prom:
            messages = await prom.get_messages(limit=MESSAGE_LIST_LIMIT)
            if not messages:
                await message.answer("Prom API повернув 0 повідомлень.")
                return
            sent = 0
            status_overrides = build_message_status_overrides(messages)
            for short_message in reversed(messages[-20:]):
                msg_key = prom_message_key(short_message)
                try:
                    try:
                        msg = short_message if is_chat_message_key(str(msg_key)) else (await prom.get_message(msg_key) if has_real_prom_message_id(msg_key) else short_message)
                    except Exception:
                        msg = short_message
                    msg_key = prom_message_key(msg)
                    if SKIP_OUTGOING_MESSAGES and is_outgoing_message(msg):
                        await db.add_message(str(msg_key), None)
                        continue
                    tg_message_id = await send_prom_message_to_telegram(message.chat.id, str(msg_key), msg)
                    summary = extract_message_summary(msg)
                    if not summary.get("message_id") or summary.get("message_id") == "—":
                        summary["message_id"] = msg_key
                    summary["status"] = status_overrides.get(str(msg_key), summary.get("status") or "unread")
                    await db.save_customer_message(msg, summary, STORE_NAME, tg_message_id)
                    sent += 1
                except Exception as inner:
                    await message.answer(f"⚠️ Помилка по повідомленню {msg_key}: <code>{str(inner)[:1500]}</code>")
            await message.answer(f"Готово. Примусово відправлено повідомлень: {sent}")
    except Exception as e:
        log.exception("force_messages failed")
        await message.answer(f"❌ Не вдалось отримати повідомлення: <code>{str(e)[:3500]}</code>")

@dp.message(Command("debug_orders"))
async def cmd_debug_orders(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer("Перевіряю замовлення Prom напряму…")
    try:
        async with PromClient(PROM_TOKEN) as prom:
            orders = await prom.get_orders()
        lines = [f"📦 <b>Prom API повернув замовлень:</b> {len(orders)}", ""]
        for order in orders[:8]:
            oid = val(order, "id", "order_id", default="—")
            date = val(order, "date_created", "created_at", default="—")
            status = val(order, "status_name", "status", default="—")
            client = " ".join(str(x or "").strip() for x in [val(order, "client_first_name", default=""), val(order, "client_last_name", default="")]).strip() or val(order, "phone", default="—")
            price = val(order, "price", "full_price", default="—")
            lines.append(f"🧾 <code>{html_escape(str(oid))}</code> — {html_escape(str(status))}\n📅 {html_escape(str(date))}\n👤 {html_escape(str(client))} | 💰 {html_escape(str(price))}")
        lines.append("\nЯкщо нове замовлення є тут, але не прийшло в Telegram — натисни /force_orders.")
        await message.answer("\n\n".join(lines[:20]))
        path = write_json_debug("prom_orders_debug.json", {"count": len(orders), "orders": orders[:20]})
        await message.answer_document(FSInputFile(path), caption="📄 Raw JSON останніх замовлень")
    except Exception as e:
        log.exception("debug_orders failed")
        await message.answer(f"❌ Не вдалось перевірити замовлення: <code>{str(e)[:3500]}</code>")


@dp.message(Command("reset_orders"))
async def cmd_reset_orders(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    if not db.conn:
        await message.answer("База ще не підключена.")
        return
    await db.reset_seen_orders()
    await message.answer("✅ Памʼять про вже відправлені замовлення очищена. Тепер /check або /force_orders зможе скинути замовлення заново.")


@dp.message(Command("force_orders"))
async def cmd_force_orders(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    parts = (message.text or "").split(maxsplit=1)
    try:
        limit = int(parts[1]) if len(parts) > 1 else 5
    except Exception:
        limit = 5
    limit = max(1, min(limit, 20))
    await message.answer(f"Примусово скидаю останні {limit} замовлень з Prom…")
    try:
        async with PromClient(PROM_TOKEN) as prom:
            orders = await prom.get_orders()
            if not orders:
                await message.answer("Prom API повернув 0 замовлень.")
                return
            sent = 0
            for short_order in reversed(orders[:limit]):
                order_id = val(short_order, "id", "order_id", default="")
                if not order_id:
                    continue
                try:
                    try:
                        order = await prom.get_order(order_id)
                    except Exception:
                        order = short_order
                    summary = extract_order_summary(order)
                    tg_msg, telegraph_url = await send_order_to_admin(order, order_id)
                    summary["telegraph_url"] = telegraph_url
                    await db.save_order(order, summary, STORE_NAME, tg_msg.message_id)
                    await db.add_order(str(order_id), tg_msg.message_id)
                    sent += 1
                except Exception as inner:
                    await message.answer(f"⚠️ Помилка по замовленню {order_id}: <code>{str(inner)[:1500]}</code>")
            await message.answer(f"Готово. Примусово відправлено замовлень: {sent}")
    except Exception as e:
        log.exception("force_orders failed")
        await message.answer(f"❌ Не вдалось отримати замовлення: <code>{str(e)[:3500]}</code>")




@dp.message(F.text.in_({"📦 Всі замовлення", "Всі замовлення", "Усі замовлення", "📦 Усі замовлення"}))
async def btn_all_orders(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await send_orders_page(message.chat.id, 0)



@dp.message(F.text.in_({"💬 Повідомлення", "Повідомлення", "💬 Всі повідомлення", "Всі повідомлення", "Усі повідомлення"}))
async def btn_all_messages(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    try:
        async with PromClient(PROM_TOKEN) as prom:
            await poll_messages(prom, send_to_tg=False)
        await cleanup_outgoing_customer_messages()
    except Exception:
        log.exception("failed to refresh messages before opening page")
    await send_messages_page(message.chat.id, 0)


@dp.message(F.text.in_({"🛠 Тех. підтримка", "Тех. підтримка", "Тех підтримка", "Підтримка", "🛠 Підтримка"}))
async def btn_support(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer(
        "🛠 <b>Тех. підтримка</b>\n\n"
        "Якщо бот працює неправильно або є питання по його роботі — звертайся до "
        "<a href=\"https://t.me/evilraveparty\">@evilraveparty</a>.",
        disable_web_page_preview=True,
        reply_markup=main_menu_keyboard(),
    )



@dp.message(Command("messages"))
async def cmd_messages(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    try:
        async with PromClient(PROM_TOKEN) as prom:
            await poll_messages(prom, send_to_tg=False)
        await cleanup_outgoing_customer_messages()
    except Exception:
        log.exception("failed to refresh messages before opening page")
    await send_messages_page(message.chat.id, 0)


@dp.callback_query(F.data.startswith("messages_page:"))
async def cb_messages_page(callback: CallbackQuery):
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Немає доступу", show_alert=True)
        return
    try:
        _, offset = callback.data.split(":", 1)
        await send_messages_page(callback.message.chat.id, int(offset), edit_message=callback.message)
        await callback.answer()
    except Exception as e:
        log.exception("messages page failed")
        await callback.answer("Помилка", show_alert=True)
        await callback.message.reply(f"⚠️ Не вдалось відкрити повідомлення: <code>{str(e)[:1500]}</code>")


@dp.callback_query(F.data == "messages_check")
async def cb_messages_check(callback: CallbackQuery):
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Немає доступу", show_alert=True)
        return
    await callback.answer("Оновлюю повідомлення…")
    try:
        async with PromClient(PROM_TOKEN) as prom:
            await poll_messages(prom, send_to_tg=False)
        await cleanup_outgoing_customer_messages()
        await send_messages_page(callback.message.chat.id, 0, edit_message=callback.message)
    except Exception as e:
        log.exception("messages check failed")
        await callback.message.reply(f"⚠️ Не вдалось оновити повідомлення: <code>{str(e)[:1500]}</code>")


@dp.callback_query(F.data.startswith("message_open:"))
async def cb_message_open(callback: CallbackQuery):
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Немає доступу", show_alert=True)
        return
    _, row_id = callback.data.split(":", 1)
    await callback.answer("Відкриваю повідомлення…")
    try:
        # Відкриття клієнта змінює той самий список на картку останнього повідомлення.
        await show_customer_message_from_db(callback.message.chat.id, row_id, edit_message=callback.message, send_attachments=True)
    except Exception as e:
        log.exception("message open failed")
        await callback.message.reply(f"⚠️ Не вдалось відкрити повідомлення: <code>{str(e)[:1500]}</code>")


@dp.callback_query(F.data.startswith("message_view:"))
async def cb_message_view(callback: CallbackQuery):
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Немає доступу", show_alert=True)
        return
    _, row_id = callback.data.split(":", 1)
    await callback.answer()
    try:
        # Попереднє/новіше повідомлення не створює новий меседж у боті — редагує поточну картку.
        await show_customer_message_from_db(callback.message.chat.id, row_id, edit_message=callback.message, send_attachments=False)
    except Exception as e:
        log.exception("message view failed")
        await callback.message.reply(f"⚠️ Не вдалось відкрити повідомлення: <code>{str(e)[:1500]}</code>")

@dp.message(Command("orders"))
async def cmd_orders(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await send_orders_page(message.chat.id, 0)


@dp.message(Command("sync_orders"))
async def cmd_sync_orders(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer("🔄 Оновлюю локальну базу замовлень з Prom…")
    try:
        async with PromClient(PROM_TOKEN) as prom:
            count = await sync_orders_from_prom(prom)
        await message.answer(f"✅ Базу оновлено. Оброблено замовлень: <b>{count}</b>\n\nНатисни знизу «📦 Всі замовлення» — там будуть кнопки по замовленнях і зміна статусів.")
    except Exception as e:
        log.exception("sync_orders failed")
        await message.answer(f"⚠️ Не вдалось оновити базу: <code>{str(e)[:1500]}</code>")


@dp.message(Command("order"))
async def cmd_order(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    parts = (message.text or "").split(maxsplit=1)
    if len(parts) < 2:
        await message.answer("Напиши так: <code>/order ID_замовлення</code>")
        return
    await send_order_details(message.chat.id, parts[1].strip())


@dp.message(Command("export_orders"))
async def cmd_export_orders(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    await message.answer("Готую CSV-експорт замовлень з локальної бази…")
    orders_path, items_path = await db.export_orders_csv()
    await message.answer_document(FSInputFile(orders_path), caption="📄 Замовлення Prom у CSV")
    await message.answer_document(FSInputFile(items_path), caption="📄 Товари із замовлень Prom у CSV")

@dp.callback_query(F.data.startswith("orders_page:"))
async def cb_orders_page(callback: CallbackQuery):
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Немає доступу", show_alert=True)
        return
    try:
        _, offset = callback.data.split(":", 1)
        await send_orders_page(callback.message.chat.id, int(offset), edit_message=callback.message)
        await callback.answer()
    except Exception as e:
        log.exception("orders page failed")
        await callback.answer("Помилка", show_alert=True)
        await callback.message.reply(f"⚠️ Не вдалось відкрити список: <code>{str(e)[:1500]}</code>")


@dp.callback_query(F.data == "orders_sync")
async def cb_orders_sync(callback: CallbackQuery):
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Немає доступу", show_alert=True)
        return
    await callback.answer("Оновлюю базу…")
    try:
        async with PromClient(PROM_TOKEN) as prom:
            count = await sync_orders_from_prom(prom)
        await bot.send_message(callback.message.chat.id, f"✅ Базу оновлено з Prom. Оброблено замовлень: <b>{count}</b>")
        await send_orders_page(callback.message.chat.id, 0)
    except Exception as e:
        log.exception("orders sync failed")
        await callback.message.reply(f"⚠️ Не вдалось оновити базу: <code>{str(e)[:1500]}</code>")


@dp.callback_query(F.data == "orders_export")
async def cb_orders_export(callback: CallbackQuery):
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Немає доступу", show_alert=True)
        return
    await callback.answer("Готую CSV…")
    orders_path, items_path = await db.export_orders_csv()
    await bot.send_document(callback.message.chat.id, FSInputFile(orders_path), caption="📄 Замовлення Prom у CSV")
    await bot.send_document(callback.message.chat.id, FSInputFile(items_path), caption="📄 Товари із замовлень Prom у CSV")


@dp.callback_query(F.data.startswith("order_open:"))
async def cb_order_open(callback: CallbackQuery):
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Немає доступу", show_alert=True)
        return
    _, order_id = callback.data.split(":", 1)
    await callback.answer("Відкриваю деталі…")
    await send_order_details(callback.message.chat.id, order_id)


@dp.callback_query(F.data.startswith("order_status:"))
async def cb_order_status(callback: CallbackQuery):
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Немає доступу", show_alert=True)
        return

    _, order_id, status = callback.data.split(":", 2)
    try:
        if await block_if_order_already_delivered(order_id):
            await callback.answer("Статус виконаного замовлення змінити неможливо", show_alert=True)
            return

        async with PromClient(PROM_TOKEN) as prom:
            await prom.set_order_status(order_id, status)
        await callback.answer("Статус змінено")
        await db.update_order_status_local(order_id, status, "changed by Telegram button")
        await callback.message.reply(f"✅ Статус замовлення <code>{order_id}</code> змінено на <b>{human_status(status)}</b>")
    except Exception as e:
        log.exception("Failed to set order status")
        await callback.answer("Помилка зміни статусу", show_alert=True)
        await callback.message.reply(f"⚠️ Не вдалось змінити статус: <code>{str(e)[:1500]}</code>")


@dp.callback_query(F.data.startswith("order_cancel:"))
async def cb_order_cancel(callback: CallbackQuery):
    if not is_admin(callback.from_user.id if callback.from_user else None):
        await callback.answer("Немає доступу", show_alert=True)
        return

    _, order_id, reason = callback.data.split(":", 2)
    cancellation_text = "Скасовано продавцем" if reason == "another" else None
    try:
        if await block_if_order_already_delivered(order_id):
            await callback.answer("Статус виконаного замовлення змінити неможливо", show_alert=True)
            return

        async with PromClient(PROM_TOKEN) as prom:
            await prom.set_order_status(
                order_id,
                "canceled",
                cancellation_reason=reason,
                cancellation_text=cancellation_text,
            )
        await callback.answer("Замовлення скасовано")
        await db.update_order_status_local(order_id, "canceled", f"canceled by Telegram button: {reason}")
        await callback.message.reply(f"❌ Замовлення <code>{order_id}</code> змінено на <b>{human_status('canceled')}</b>")
    except Exception as e:
        log.exception("Failed to cancel order")
        await callback.answer("Помилка скасування", show_alert=True)
        await callback.message.reply(f"⚠️ Не вдалось скасувати: <code>{str(e)[:1500]}</code>")




def _chat_room_id_from_prom_message_key(message_key: str) -> str:
    key = str(message_key or "").strip()
    if key.startswith("chat:"):
        parts = key.split(":", 2)
        if len(parts) >= 2:
            return str(parts[1] or "").strip()
    return ""


def _chat_room_id_from_raw_message(raw: dict[str, Any]) -> str:
    if not isinstance(raw, dict):
        return ""
    room_id = val(raw, "__chat_room_id", "room_id", "chat_id", "dialog_id", default="")
    if room_id and room_id != "—":
        return str(room_id).strip()
    room = raw.get("__chat_room")
    if isinstance(room, dict):
        room_id = val(room, "id", "room_id", "chat_id", "dialog_id", default="")
        if room_id and room_id != "—":
            return str(room_id).strip()
    return ""


async def _reply_targets_for_prom_message(prom_message_id: str) -> list[str]:
    """Build safe reply targets for a message opened from the menu.

    The local row can sometimes have a synthetic hash id, but raw_json still
    contains the Prom chat room id. In that case we reply to the room instead of
    the fake message id, so replies from “💬 Повідомлення” reach Prom correctly.
    """
    targets: list[str] = []

    def add(target: str):
        target = str(target or "").strip()
        if target and target not in targets:
            targets.append(target)

    add(str(prom_message_id))

    try:
        row = await db.get_customer_message_by_prom_id(str(prom_message_id))
    except Exception:
        row = None

    raw: dict[str, Any] = {}
    if row:
        try:
            raw_text = row["raw_json"] if "raw_json" in row.keys() else ""
            raw = json.loads(raw_text) if raw_text else {}
        except Exception:
            raw = {}

    room_id = _chat_room_id_from_prom_message_key(str(prom_message_id)) or _chat_room_id_from_raw_message(raw)
    if room_id:
        add(f"chat:{room_id}:reply")

    # If the saved id is a fake hash, do not waste the first API attempt on it.
    real_targets = [t for t in targets if has_real_prom_message_id(t)]
    return real_targets or targets


@dp.message(F.text)
async def reply_to_prom_message(message: Message):
    if not is_admin(message.from_user.id if message.from_user else None):
        return
    if not message.reply_to_message:
        return

    prom_message_id = await db.get_prom_message_by_tg_reply(message.reply_to_message.message_id)
    if not prom_message_id:
        return

    text = (message.text or "").strip()
    if not text:
        return

    targets = await _reply_targets_for_prom_message(str(prom_message_id))
    if not targets or not any(has_real_prom_message_id(t) for t in targets):
        await message.reply(
            "⚠️ Не знайшов реальний Prom chat/message id для відповіді. "
            "Натисни «🔄 Оновити з Prom» у повідомленнях і відкрий клієнта ще раз."
        )
        return

    errors: list[str] = []
    try:
        async with PromClient(PROM_TOKEN) as prom:
            reply_result = None
            used_target = ""
            for target in targets:
                if not has_real_prom_message_id(str(target)):
                    continue
                try:
                    reply_result = await prom.reply_message(target, text)
                    used_target = str(target)
                    break
                except Exception as inner:
                    errors.append(f"{target}: {str(inner)[:700]}")
                    continue

            if reply_result is None:
                raise PromAPIError("Не спрацював жоден варіант відповіді:\n" + "\n".join(errors[:10]))

            try:
                await prom.set_message_status(used_target or prom_message_id, "read")
            except Exception:
                pass

        try:
            await db.update_customer_conversation_status_local(used_target or prom_message_id, "read")
        except Exception:
            try:
                await db.update_customer_conversation_status_local(prom_message_id, "read")
            except Exception:
                pass

        payload_used = ""
        if isinstance(reply_result, dict):
            payload_used = str(reply_result.get("__payload_used") or "")
        if payload_used:
            await message.reply(f"✅ Відповідь відправлена в Prom. API: <code>{html_escape(payload_used)}</code>")
        else:
            await message.reply("✅ Відповідь відправлена в Prom")
    except Exception as e:
        log.exception("Failed to reply Prom message")
        details = ""
        if errors:
            details = "\n\nСпробовані цілі:\n<code>" + html_escape("\n".join(errors[:5])) + "</code>"
        await message.reply(f"⚠️ Не вдалось відправити відповідь у Prom: <code>{html_escape(str(e)[:1200])}</code>{details}")

async def sync_orders_from_prom(prom: PromClient) -> int:
    """Pull current Prom order list into local DB without Telegram spam.

    /orders shows the local DB. This command fills/refreshes that DB, updates statuses,
    keeps existing Telegraph links, and extracts Ukrainian product names + sizes from SKU.
    """
    count = 0
    orders = await prom.get_orders()
    for short_order in reversed(orders):
        order_id = val(short_order, "id", "order_id", default="")
        if not order_id:
            continue
        try:
            try:
                order = await prom.get_order(order_id)
            except Exception:
                order = short_order
            summary = extract_order_summary(order)
            await db.save_order(order, summary, STORE_NAME, None)
            await db.add_order(str(order_id), None)
            count += 1
        except Exception:
            log.exception("Failed to sync order %s", order_id)
    return count


async def poll_orders(prom: PromClient, *, send_to_tg: bool = True) -> int:
    sent = 0
    try:
        orders = await prom.get_orders()
    except Exception as e:
        log.exception("Failed to fetch orders")
        if ADMIN_CHAT_ID:
            await notify_admins(f"⚠️ Помилка отримання замовлень Prom: <code>{str(e)[:1500]}</code>")
        return 0

    # Usually API returns newest first. Reverse so Telegram receives older first.
    for short_order in reversed(orders):
        order_id = val(short_order, "id", "order_id", default="")
        if not order_id:
            continue
        seen = await db.has_order(str(order_id))
        seen_tg = await db.get_seen_order_tg_message_id(str(order_id)) if seen else None
        if seen and not (send_to_tg and ADMIN_CHAT_ID and seen_tg is None and is_recent_order_for_notification(short_order)):
            continue

        try:
            try:
                order = await prom.get_order(order_id)
            except Exception:
                order = short_order

            if seen and send_to_tg and ADMIN_CHAT_ID and seen_tg is None and not is_recent_order_for_notification(order):
                continue

            summary = extract_order_summary(order)
            if send_to_tg and ADMIN_CHAT_ID:
                tg_msg, telegraph_url = await send_order_to_admin(order, order_id)
                summary["telegraph_url"] = telegraph_url
                await db.save_order(order, summary, STORE_NAME, tg_msg.message_id)
                await db.add_order(str(order_id), tg_msg.message_id)
                sent += 1
            else:
                await db.save_order(order, summary, STORE_NAME, None)
                await db.add_order(str(order_id), None)
        except Exception as e:
            log.exception("Failed to process order %s", order_id)
            if ADMIN_CHAT_ID:
                await notify_admins(f"⚠️ Помилка обробки замовлення {order_id}: <code>{str(e)[:1500]}</code>")
    return sent


async def poll_messages(prom: PromClient, *, send_to_tg: bool = True) -> int:
    sent = 0
    try:
        messages = await prom.get_messages(limit=MESSAGE_LIST_LIMIT)
    except PromAPIError as e:
        log.warning("Failed to fetch messages: %s", e)
        if ADMIN_CHAT_ID:
            await notify_admins(f"⚠️ Помилка отримання повідомлень Prom: <code>{str(e)[:1500]}</code>")
        return 0
    except Exception:
        log.exception("Failed to fetch messages")
        return 0

    log.info("Fetched %s Prom messages", len(messages))
    status_overrides = build_message_status_overrides(messages)

    for short_message in reversed(messages):
        msg_key = prom_message_key(short_message)
        seen = await db.has_message(str(msg_key))

        try:
            try:
                msg = short_message if is_chat_message_key(str(msg_key)) else (await prom.get_message(msg_key) if has_real_prom_message_id(msg_key) else short_message)
            except Exception:
                msg = short_message

            msg_key = prom_message_key(msg)
            seen = await db.has_message(str(msg_key))

            # Never save/send our own replies as client messages. If an older version saved
            # them already, cleanup_outgoing_customer_messages() removes them from the list.
            if SKIP_OUTGOING_MESSAGES and is_outgoing_message(msg):
                await db.add_message(str(msg_key), None)
                continue

            summary = extract_message_summary(msg)
            if not summary.get("message_id") or summary.get("message_id") == "—":
                summary["message_id"] = msg_key
            # Use fixed local status: read if Prom says read OR if the shop replied later in the same chat.
            summary["status"] = status_overrides.get(str(msg_key), summary.get("status") or "unread")

            if seen:
                # Important: update old rows too. Earlier versions skipped seen messages,
                # so their status could stay red forever even after a reply.
                await db.save_customer_message(msg, summary, STORE_NAME, None)
                continue

            if send_to_tg and ADMIN_CHAT_ID:
                tg_message_id = await send_prom_message_to_admins(str(msg_key), msg)
                await db.save_customer_message(msg, summary, STORE_NAME, tg_message_id)
                sent += 1
            else:
                await db.save_customer_message(msg, summary, STORE_NAME, None)
                await db.add_message(str(msg_key), None)
        except Exception as e:
            log.exception("Failed to process message %s", msg_key)
            if ADMIN_CHAT_ID:
                await notify_admins(f"⚠️ Помилка обробки повідомлення {msg_key}: <code>{str(e)[:1500]}</code>")
    return sent


async def polling_loop():
    await db.connect()
    await cleanup_outgoing_customer_messages()
    async with PromClient(PROM_TOKEN) as prom:
        if not SEND_EXISTING_ON_START:
            log.info("Bootstrap mode: remember existing orders without sending")
            await poll_orders(prom, send_to_tg=False)
            if BOOTSTRAP_MESSAGES_ON_START:
                log.info("Bootstrap mode: remember existing messages without sending")
                await poll_messages(prom, send_to_tg=False)
            else:
                log.info("Messages are NOT bootstrapped, so customer chats won't be hidden on startup")

        if ADMIN_CHAT_IDS:
            for chat_id in ADMIN_CHAT_IDS:
                try:
                    await bot.send_message(
                        chat_id,
                        f"✅ Prom → Telegram бот запущений для магазину <b>{STORE_NAME}</b>. Інтервал перевірки: {POLL_INTERVAL} сек.\n\nЗнизу є кнопки: <b>📦 Всі замовлення</b>, <b>💬 Повідомлення</b>, <b>🛠 Тех. підтримка</b>.",
                        reply_markup=main_menu_keyboard(),
                    )
                except Exception:
                    log.exception("Failed to send startup message to admin %s", chat_id)

        while True:
            await poll_orders(prom, send_to_tg=True)
            await poll_messages(prom, send_to_tg=True)
            await asyncio.sleep(POLL_INTERVAL)


async def main():
    polling_task = asyncio.create_task(polling_loop())
    try:
        await dp.start_polling(bot)
    finally:
        polling_task.cancel()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
