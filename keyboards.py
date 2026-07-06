from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton

try:
    from formatters import human_status, pick_text
except Exception:  # pragma: no cover
    def human_status(status: Any) -> str:
        return str(status or "—")
    def pick_text(value: Any) -> str:
        return str(value or "")


def row_get(row: Any, key: str, default: Any = "") -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    try:
        if key in row.keys():
            return row[key]
    except Exception:
        pass
    try:
        return row[key]
    except Exception:
        return default



def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📦 Всі замовлення")],
            [KeyboardButton(text="💬 Повідомлення")],
            [KeyboardButton(text="🛠 Тех. підтримка")],
        ],
        resize_keyboard=True,
        input_field_placeholder="Вибери дію",
        selective=False,
    )

def order_keyboard(order_id: str | int, telegraph_url: str | None = None, *, show_db_button: bool = True) -> InlineKeyboardMarkup:
    oid = str(order_id)
    rows: list[list[InlineKeyboardButton]] = []
    if telegraph_url:
        rows.append([InlineKeyboardButton(text="🌐 Повна інформація про замовлення", url=telegraph_url)])
    rows.extend(
        [
            [
                InlineKeyboardButton(text="✅ Прийнято", callback_data=f"order_status:{oid}:received"),
                InlineKeyboardButton(text="📦 Виконано", callback_data=f"order_status:{oid}:delivered"),
            ],
            [
                InlineKeyboardButton(text="❌ Скасовано", callback_data=f"order_cancel:{oid}:another"),
            ],
        ]
    )
    if show_db_button:
        rows.append([InlineKeyboardButton(text="📋 Відкрити з бази", callback_data=f"order_open:{oid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _order_button_text(row: Any) -> str:
    oid = pick_text(row_get(row, "order_id", ""))
    status = human_status(row_get(row, "prom_status", ""))
    total = pick_text(row_get(row, "total_price", ""))
    client = pick_text(row_get(row, "client_name", ""))
    parts = [f"🧾 {oid}", status]
    if total and total != "—":
        parts.append(total)
    if client and client != "—":
        parts.append(client[:24])
    return " · ".join(parts)[:64]


def orders_page_keyboard(rows, offset: int, limit: int, total: int) -> InlineKeyboardMarkup | None:
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        oid = pick_text(row["order_id"])
        if oid:
            buttons.append([InlineKeyboardButton(text=_order_button_text(row), callback_data=f"order_open:{oid}")])

    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        prev_offset = max(0, offset - limit)
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"orders_page:{prev_offset}"))
    if offset + limit < total:
        next_offset = offset + limit
        nav.append(InlineKeyboardButton(text="➡️ Далі", callback_data=f"orders_page:{next_offset}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="🔄 Оновити базу з Prom", callback_data="orders_sync")])
    buttons.append([InlineKeyboardButton(text="📄 Експорт CSV", callback_data="orders_export")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)



def _message_is_read(status: Any) -> bool:
    value = pick_text(status).strip().lower()
    if not value:
        return False
    if value in {"unread", "new", "нове", "непрочитано", "не прочитано"}:
        return False
    return (
        value in {"read", "прочитано", "seen", "viewed", "answered", "replied", "reply_sent", "sent_reply"}
        or "read" in value
        or "seen" in value
        or "view" in value
        or "проч" in value
        or "answer" in value
        or "reply" in value
        or "відпов" in value
        or "отвеч" in value
    )


def _message_button_text(row: Any) -> str:
    # Один клієнт = одна кнопка. Тільки статус + нік клієнта.
    unread_count = 0
    try:
        unread_count = int(row_get(row, "unread_count", 0) or 0)
    except Exception:
        unread_count = 0
    is_read = unread_count <= 0 and _message_is_read(row_get(row, "status", ""))
    status_icon = "🟢" if is_read else "🔴"
    client = pick_text(row_get(row, "client_name", ""))
    if not client or client == "—":
        client = "Клієнт"
    return f"{status_icon} {client}"[:64]


def messages_page_keyboard(rows, offset: int, limit: int, total: int) -> InlineKeyboardMarkup | None:
    buttons: list[list[InlineKeyboardButton]] = []
    for row in rows:
        row_id = pick_text(row_get(row, "row_num", ""))
        if row_id:
            buttons.append([InlineKeyboardButton(text=_message_button_text(row), callback_data=f"message_open:{row_id}")])

    nav: list[InlineKeyboardButton] = []
    if offset > 0:
        nav.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"messages_page:{max(0, offset - limit)}"))
    if offset + limit < total:
        nav.append(InlineKeyboardButton(text="➡️ Далі", callback_data=f"messages_page:{offset + limit}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="🔄 Оновити з Prom", callback_data="messages_check")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def message_detail_keyboard(row_id: str | int, navigation: dict[str, Any] | None = None) -> InlineKeyboardMarkup:
    navigation = navigation or {}
    buttons: list[list[InlineKeyboardButton]] = []

    older = navigation.get("older_row_id")
    newer = navigation.get("newer_row_id")
    nav: list[InlineKeyboardButton] = []
    if older:
        nav.append(InlineKeyboardButton(text="⬅️ Попереднє", callback_data=f"message_view:{older}"))
    if newer:
        nav.append(InlineKeyboardButton(text="➡️ Новіше", callback_data=f"message_view:{newer}"))
    if nav:
        buttons.append(nav)

    buttons.append([InlineKeyboardButton(text="💬 До повідомлень", callback_data="messages_page:0")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
