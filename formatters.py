from __future__ import annotations

import os
import re
import json
from datetime import datetime, timezone, timedelta
from html import escape
from typing import Any

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

KYIV_FIXED_TZ = timezone(timedelta(hours=3))

def kyiv_tz():
    """Return Europe/Kyiv timezone. On Windows ZoneInfo may need tzdata, so fallback to UTC+3."""
    if ZoneInfo:
        try:
            return ZoneInfo("Europe/Kyiv")
        except Exception:
            return KYIV_FIXED_TZ
    return KYIV_FIXED_TZ

def to_kyiv(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(kyiv_tz())

FORMATTERS_VERSION = "v34_clean_messages_orders_reply"

EMPTY = (None, "", [], {})


def is_empty(value: Any) -> bool:
    return value in EMPTY


def e(value: Any) -> str:
    return escape(str(value if not is_empty(value) else "—"))


def val(data: dict, *keys: str, default: Any = "—") -> Any:
    if not isinstance(data, dict):
        return default
    for key in keys:
        if key in data and not is_empty(data[key]):
            return data[key]
    return default


def dig(data: Any, *path: str, default: Any = "") -> Any:
    cur = data
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur if not is_empty(cur) else default


def first_non_empty(*values: Any) -> Any:
    for value in values:
        if not is_empty(value):
            return value
    return ""


def find_first(data: Any, keys: tuple[str, ...]) -> Any:
    """Recursively find the first non-empty value by key name."""
    if isinstance(data, dict):
        for key in keys:
            if key in data and not is_empty(data[key]):
                return data[key]
        for value in data.values():
            found = find_first(value, keys)
            if not is_empty(found):
                return found
    elif isinstance(data, list):
        for item in data:
            found = find_first(item, keys)
            if not is_empty(found):
                return found
    return ""


def pick_text(value: Any) -> str:
    """Return readable text, preferring Ukrainian fields when Prom returns multilingual data."""
    if is_empty(value):
        return ""
    if isinstance(value, dict):
        # Multilingual Prom/product structures can look like {"uk": "...", "ru": "..."}.
        for key in ("uk", "ua", "uk_UA", "uk-UA", "ukr", "ukrainian"):
            if value.get(key) not in EMPTY:
                return pick_text(value[key])
        # Then common display fields. Their value can also be multilingual.
        for key in ("name", "name_uk", "title", "title_uk", "caption", "value", "value_uk", "text", "label", "id"):
            if value.get(key) not in EMPTY:
                return pick_text(value[key])
        parts = []
        for k, v in value.items():
            if not is_empty(v) and not isinstance(v, (dict, list)):
                parts.append(f"{k}: {v}")
        return "; ".join(parts[:5])
    if isinstance(value, list):
        return ", ".join(pick_text(x) for x in value if not is_empty(x))
    return str(value)


def money(v: Any) -> str:
    txt = pick_text(v).strip()
    return txt or "—"


def format_quantity(qty: Any, unit: Any = "") -> str:
    q = pick_text(qty).strip() or "1"
    q = q.replace(".0", "") if re.fullmatch(r"\d+\.0", q) else q
    unit_t = pick_text(unit).strip()
    if unit_t and unit_t != "—":
        return f"{q} {unit_t}"
    return q


def format_date(value: Any) -> str:
    """Format Prom UTC dates into Ukrainian time: 06.07.2026 о 17:17."""
    raw = pick_text(value).strip()
    if not raw:
        return "—"

    # If text contains ISO date inside a longer string, take only the date part.
    iso_match = re.search(r"\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?", raw)
    if iso_match:
        raw = iso_match.group(0)

    # Unix timestamp / milliseconds
    if re.fullmatch(r"\d{10,13}", raw):
        try:
            ts = int(raw)
            if ts > 10_000_000_000:
                ts = ts / 1000
            return to_kyiv(datetime.fromtimestamp(ts, tz=timezone.utc)).strftime("%d.%m.%Y о %H:%M")
        except Exception:
            pass

    # /Date(1712345678000)/
    m = re.search(r"Date\((\d{10,13})\)", raw)
    if m:
        return format_date(m.group(1))

    candidates = [raw, raw.replace("Z", "+00:00"), raw.replace(" ", "T")]
    for cand in candidates:
        try:
            cand2 = re.sub(r"([+-]\d{2})(\d{2})$", r"\1:\2", cand)
            dt = datetime.fromisoformat(cand2)
            return to_kyiv(dt).strftime("%d.%m.%Y о %H:%M")
        except Exception:
            continue

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M:%S", "%d.%m.%Y %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw, fmt)
            return to_kyiv(dt).strftime("%d.%m.%Y о %H:%M") if "%H" in fmt else dt.strftime("%d.%m.%Y")
        except Exception:
            continue
    return raw

def clean_text(text: Any) -> str:
    t = pick_text(text).strip()
    t = re.sub(r"\s+", " ", t)
    return t


def clean_product_name(text: Any) -> str:
    """Make long marketplace titles readable while keeping Ukrainian product name."""
    t = clean_text(text)
    if not t:
        return ""
    # Prom titles are often SEO strings: "Name | keywords | keywords".
    # In Telegram/Telegraph the first part is the real product name.
    first = re.split(r"\s*[|｜]\s*", t, maxsplit=1)[0].strip()
    return first or t


def parse_size_from_sku(sku: Any) -> str:
    """For this Prom store SKUs look like COLORCODE-WIDTH-HEIGHT, e.g. 1019-850-1600."""
    raw = clean_text(sku)
    if not raw:
        return ""
    nums = re.findall(r"\d+", raw)
    if len(nums) >= 3:
        width, height = nums[-2], nums[-1]
        try:
            w, h = int(width), int(height)
            # Avoid nonsense matches; blinds dimensions are usually in millimeters.
            if 100 <= w <= 4000 and 300 <= h <= 4000:
                return f"{w} × {h} мм"
        except Exception:
            pass
    return ""


def parse_color_from_product_name(name: Any) -> str:
    t = clean_product_name(name)
    if not t:
        return ""
    # Examples from the real Prom JSON: "Ролети на вікна білі", "Ролети на вікна темно-сірі".
    m = re.search(r"на\s+вікна\s+([^,|]+)$", t, flags=re.I)
    if m:
        color = clean_text(m.group(1))
        if color and not re.search(r"ролет|штор|жалюз", color, flags=re.I):
            return color
    return ""


def clean_city(text: Any) -> str:
    t = clean_text(text)
    if not t:
        return ""
    # If Prom puts full address into city, keep only city part.
    parts = [p.strip() for p in re.split(r"[,;]", t) if p.strip()]
    first = parts[0] if parts else t
    first = re.sub(r"^(м\.|місто|город|г\.)\s*", "", first, flags=re.I).strip()
    first = re.sub(r"\s+", " ", first)
    return first


def city_from_address(address: Any) -> str:
    t = clean_text(address)
    if not t:
        return ""
    m = re.search(r"(?:^|[,;\s])(м\.|місто|город|г\.)\s*([^,;]+)", t, flags=re.I)
    if m:
        return clean_city(m.group(2))
    # Common address format: "Рівне, Відділення №..."
    parts = [p.strip() for p in re.split(r"[,;]", t) if p.strip()]
    if len(parts) > 1 and not re.search(r"відділення|поштомат|вул\.|улиц|буд\.|№|нова пошта|укрпошта", parts[0], re.I):
        return clean_city(parts[0])
    return ""


def remove_city_from_address(address: Any, city: Any = "") -> str:
    t = clean_text(address)
    c = clean_city(city)
    if not t:
        return ""
    # Remove leading "м. Київ," / "Київ," if the city is shown separately.
    if c:
        patterns = [
            rf"^(м\.|місто|город|г\.)\s*{re.escape(c)}\s*[,;:-]?\s*",
            rf"^{re.escape(c)}\s*[,;:-]?\s*",
        ]
        for pat in patterns:
            t2 = re.sub(pat, "", t, flags=re.I).strip()
            if t2 != t:
                t = t2
                break
    # Remove duplicate prefixes left by delivery services.
    t = re.sub(r"^(адреса|адрес|delivery address)\s*[:\-]\s*", "", t, flags=re.I).strip()
    return t


def looks_like_warehouse(text: Any) -> bool:
    t = clean_text(text).lower()
    return bool(re.search(r"відділення|отделение|поштомат|postomat|warehouse|нова пошта|укрпошта|np\s*№|№", t))


def get_nested_dicts(order: dict) -> list[dict]:
    keys = (
        "delivery", "delivery_data", "delivery_provider_data", "delivery_provider", "shipping",
        "shipping_data", "delivery_address", "address", "recipient", "receiver",
    )
    result = [order]
    for key in keys:
        obj = order.get(key)
        if isinstance(obj, dict):
            result.append(obj)
    return result


def get_from_sources(sources: list[dict], *keys: str) -> Any:
    for src in sources:
        found = val(src, *keys, default="")
        if not is_empty(found):
            return found
    return ""


def extract_client_name(data: dict) -> str:
    direct = val(data, "client_name", "customer_name", "buyer", "name", default="")
    if isinstance(direct, dict):
        direct = pick_text(direct)
    if direct:
        return str(direct)
    parts = [
        val(data, "client_first_name", "first_name", "firstname", default=""),
        val(data, "client_last_name", "last_name", "lastname", default=""),
        val(data, "client_second_name", "middle_name", default=""),
    ]
    return " ".join(str(x) for x in parts if x).strip() or "—"



OPTION_CONTAINER_KEYS = (
    "selected_options", "options", "product_options", "order_options", "attributes", "properties",
    "parameters", "params", "characteristics", "features", "variant", "variation", "modification",
    "modifications", "variations", "dimension", "dimensions", "size_data", "custom_fields",
)

OPTION_META_KEYS = {
    "id", "product_id", "offer_id", "external_id", "sku", "article", "vendor_code", "code",
    "name", "name_uk", "product_name", "title", "title_uk", "url", "link", "product_url",
    "portal_url", "site_url", "price", "unit_price", "price_with_discount", "total_price",
    "total", "total_sum", "sum", "quantity", "qty", "amount", "count", "raw", "product",
    "image", "image_url", "photo", "photos", "description",
}

UK_OPTION_LABELS = {
    "size": "Розмір",
    "width": "Ширина",
    "height": "Висота",
    "length": "Довжина",
    "color": "Колір",
    "colour": "Колір",
    "material": "Матеріал",
    "variant": "Різновид",
    "variation": "Різновид",
    "modification": "Модифікація",
    "option": "Опція",
    "parameter": "Параметр",
    "attribute": "Характеристика",
    "characteristic": "Характеристика",
}


def uk_option_label(key: Any) -> str:
    raw = clean_text(key)
    if not raw:
        return "Різновид"
    low = raw.lower().strip()
    return UK_OPTION_LABELS.get(low, raw[:1].upper() + raw[1:])


def _option_pair_from_dict(obj: dict[str, Any]) -> tuple[str, str] | None:
    label = first_non_empty(
        val(obj, "name_uk", "name", "title_uk", "title", "label", "caption", default=""),
        val(obj, "parameter_name", "attribute_name", "property_name", "option_name", "characteristic_name", "feature_name", default=""),
    )
    value = first_non_empty(
        val(obj, "value_uk", "value", "text", "selected_value", "option_value", "attribute_value", "property_value", "characteristic_value", "feature_value", default=""),
        val(obj, "values", "items", default=""),
    )
    # Some APIs use {"title": "80x160"} as a whole variant; don't duplicate title as label+value.
    if label and value and pick_text(label).strip() != pick_text(value).strip():
        return uk_option_label(label), pick_text(value)
    if value and not label:
        return "Різновид", pick_text(value)
    return None


def extract_item_options(item: dict[str, Any], product_obj: dict[str, Any] | None = None) -> list[tuple[str, str]]:
    """Extract selected product variation/size/options from many possible Prom API shapes."""
    product_obj = product_obj if isinstance(product_obj, dict) else {}
    pairs: list[tuple[str, str]] = []

    def add(label: Any, value: Any):
        label_t = uk_option_label(label)
        value_t = pick_text(value).strip()
        if not value_t or value_t == "—":
            return
        # Avoid useless duplicates like ID/price or product name as an option.
        if label_t.lower() in {"id", "product_id", "sku", "article", "price", "quantity", "url"}:
            return
        pair = (label_t, value_t)
        key = (label_t.lower(), value_t.lower())
        existing = {(a.lower(), b.lower()) for a, b in pairs}
        if key not in existing:
            pairs.append(pair)

    def walk_container(value: Any, default_label: str = "Різновид"):
        if is_empty(value):
            return
        if isinstance(value, list):
            for x in value:
                walk_container(x, default_label)
            return
        if isinstance(value, dict):
            pair = _option_pair_from_dict(value)
            if pair:
                add(pair[0], pair[1])
                return
            for k, v in value.items():
                if k in OPTION_META_KEYS or is_empty(v):
                    continue
                if isinstance(v, (dict, list)):
                    walk_container(v, uk_option_label(k))
                else:
                    add(k, v)
            return
        add(default_label, value)

    # Direct well-known selected-option containers.
    for src in (item, product_obj):
        for key in OPTION_CONTAINER_KEYS:
            if key in src and not is_empty(src.get(key)):
                walk_container(src.get(key), uk_option_label(key))

    # Direct dimension/color fields are important for ролети/жалюзі.
    for src in (item, product_obj):
        width = first_non_empty(val(src, "width", "shirina", "ширина", default=""), val(src, "w", default=""))
        height = first_non_empty(val(src, "height", "visota", "висота", default=""), val(src, "h", default=""))
        if width and height:
            add("Розмір", f"{pick_text(width)} × {pick_text(height)}")
        else:
            if width:
                add("Ширина", width)
            if height:
                add("Висота", height)
        for key in ("size", "rozmir", "розмір", "color", "colour", "kolir", "колір"):
            if key in src and not is_empty(src[key]):
                add(key, src[key])

    # Fallback for this exact Prom store: SKU contains dimensions, e.g. 1019-850-1600.
    already_has_size = any(re.search(r"розмір|ширина|висота|довжина", label, flags=re.I) for label, _ in pairs)
    sku_size = parse_size_from_sku(first_non_empty(val(item, "sku", default=""), val(product_obj, "sku", default="")))
    if sku_size and not already_has_size:
        add("Розмір", sku_size)

    # Fallback color from Ukrainian title, e.g. "Ролети на вікна білі".
    already_has_color = any(re.search(r"колір|color|colour", label, flags=re.I) for label, _ in pairs)
    product_name = first_non_empty(
        dig(item, "name_multilang", "uk", default=""),
        dig(item, "name_multilang", "ua", default=""),
        val(item, "name_uk", "name", default=""),
        dig(product_obj, "name_multilang", "uk", default=""),
        val(product_obj, "name_uk", "name", default=""),
    )
    color = parse_color_from_product_name(product_name)
    if color and not already_has_color:
        add("Колір", color)

    return pairs[:20]


def format_options_text(options: list[tuple[str, str]]) -> str:
    return "; ".join(f"{label}: {value}" for label, value in options if value)

def extract_order_items(order: dict) -> list[dict[str, Any]]:
    raw_items = (
        order.get("products")
        or order.get("items")
        or order.get("order_items")
        or order.get("positions")
        or []
    )
    if isinstance(raw_items, dict):
        raw_items = list(raw_items.values())
    if not isinstance(raw_items, list):
        return []

    items = []
    for p in raw_items:
        if not isinstance(p, dict):
            continue
        product_obj = p.get("product") if isinstance(p.get("product"), dict) else {}
        product_id = first_non_empty(
            val(p, "product_id", "id", default=""),
            val(product_obj, "id", "product_id", default=""),
        )
        # Prefer Ukrainian names if Prom returns multilingual product data.
        # In the real Prom JSON for this store, Ukrainian title is inside product["name_multilang"]["uk"].
        name = first_non_empty(
            dig(p, "name_multilang", "uk", default=""),
            dig(p, "name_multilang", "ua", default=""),
            dig(product_obj, "name_multilang", "uk", default=""),
            dig(product_obj, "name_multilang", "ua", default=""),
            val(p, "name_uk", "product_name_uk", "title_uk", default=""),
            val(product_obj, "name_uk", "product_name_uk", "title_uk", default=""),
            val(p, "name_multilang", default=""),
            val(product_obj, "name_multilang", default=""),
            val(p, "name", "product_name", "title", default=""),
            val(product_obj, "name", "product_name", "title", default=""),
        )
        sku = first_non_empty(
            val(p, "sku", "article", "vendor_code", "external_id", "code", "product_sku", default=""),
            val(product_obj, "sku", "article", "vendor_code", "external_id", "code", "product_sku", default=""),
        )
        product_url = first_non_empty(
            val(p, "url", "link", "product_url", "portal_url", "site_url", default=""),
            val(product_obj, "url", "link", "product_url", "portal_url", "site_url", default=""),
        )
        options = extract_item_options(p, product_obj)
        items.append(
            {
                "product_id": pick_text(product_id),
                "name": clean_product_name(name) or "Товар",
                "sku": pick_text(sku) or "—",
                "quantity": format_quantity(val(p, "quantity", "qty", "amount", "count", default="1"), val(p, "measure_unit", "unit", default="")) or "1",
                "price": money(val(p, "price", "unit_price", "price_with_discount", default="")),
                "total_price": money(val(p, "total_price", "sum", "total", "total_sum", default="")),
                "product_url": pick_text(product_url) or "—",
                "options": options,
                "options_text": format_options_text(options),
                "raw": p,
            }
        )
    return items



def payment_status_from_order(order: dict) -> str:
    """Return Ukrainian payment status close to what Prom shows in cabinet."""
    if not isinstance(order, dict):
        return ""
    payment_data = order.get("payment_data") if isinstance(order.get("payment_data"), dict) else {}
    raw = first_non_empty(
        val(payment_data, "status_name", "status_title", "title", default=""),
        val(order, "payment_status_name", "payment_status_title", default=""),
        val(payment_data, "status", default=""),
        val(order, "payment_status", default=""),
        find_first(order, ("payment_status_name", "payment_status_title")),
    )
    text = pick_text(raw).strip()
    key = text.lower().strip()
    key = key.replace("_", "-").replace(" ", "-")
    mapping = {
        "paid": "Оплачено",
        "paid-out": "Оплачено",
        "paidout": "Оплачено",
        "completed": "Оплачено",
        "success": "Оплачено",
        "successful": "Оплачено",
        "оплачено": "Оплачено",
        "оплачен": "Оплачено",
        "unpaid": "Не оплачено",
        "not-paid": "Не оплачено",
        "not_paid": "Не оплачено",
        "new": "Не оплачено",
        "pending": "Не оплачено",
        "created": "Не оплачено",
        "не-оплачено": "Не оплачено",
        "неоплачено": "Не оплачено",
        "не-оплачен": "Не оплачено",
        "refunded": "Повернено",
        "refund": "Повернено",
        "returned": "Повернено",
        "повернено": "Повернено",
        "возвращено": "Повернено",
        "canceled": "Скасовано",
        "cancelled": "Скасовано",
        "canceled-by-user": "Скасовано",
        "cancelled-by-user": "Скасовано",
        "скасовано": "Скасовано",
        "отменено": "Скасовано",
        "processing": "В обробці",
        "hold": "В обробці",
        "authorized": "В обробці",
    }
    if key in mapping:
        return mapping[key]
    return text


def is_prom_payment(payment: Any, order: dict | None = None) -> bool:
    text = pick_text(payment).lower()
    if "пром" in text or "prom" in text or "evopay" in text:
        return True
    if isinstance(order, dict):
        pdata = order.get("payment_data") if isinstance(order.get("payment_data"), dict) else {}
        if pick_text(pdata.get("type")).lower() in {"evopay", "prom", "prompay", "prom-payment"}:
            return True
    return False


def payment_status_emoji(status: Any) -> str:
    text = pick_text(status).lower()
    if "оплач" in text and "не" not in text:
        return "🟢"
    if "не оплач" in text or "очіку" in text or "оброб" in text:
        return "🟡"
    if "скас" in text or "повер" in text:
        return "🔴"
    return "ℹ️"


def delivery_point_from_summary(s: dict[str, Any]) -> str:
    city = clean_city(s.get("delivery_city") or "")
    warehouse = clean_text(s.get("delivery_warehouse") or "")
    address = remove_city_from_address(s.get("delivery_address") or "", city)
    parts = []
    if city and city != "—":
        parts.append(city)
    point = warehouse if warehouse and warehouse != "—" else address
    if point and point != "—":
        # Avoid repeating the city if Prom address already starts with it.
        point_clean = remove_city_from_address(point, city) if city else point
        if point_clean and point_clean != "—":
            parts.append(point_clean)
    return ", ".join(parts) or "—"

def extract_order_summary(order: dict) -> dict[str, Any]:
    sources = get_nested_dicts(order)

    raw_address = first_non_empty(
        get_from_sources(sources, "delivery_address", "shipping_address", "address", "full_address", "recipient_address"),
        find_first(order, ("delivery_address", "shipping_address", "full_address", "recipient_address")),
    )
    raw_city = first_non_empty(
        get_from_sources(sources, "delivery_city", "city", "city_name", "recipient_city"),
        find_first(order, ("delivery_city", "city_name", "recipient_city")),
        city_from_address(raw_address),
    )
    city = clean_city(raw_city) or city_from_address(raw_address)

    raw_warehouse = first_non_empty(
        get_from_sources(
            sources,
            "delivery_warehouse", "warehouse", "warehouse_name", "department", "department_name",
            "branch", "branch_name", "office", "office_name", "post_office", "pickup_point",
        ),
        find_first(order, ("warehouse_name", "department_name", "post_office", "pickup_point")),
    )

    address = remove_city_from_address(raw_address, city)
    warehouse = clean_text(raw_warehouse)
    if not warehouse and looks_like_warehouse(address):
        warehouse = address
        address = ""
    if warehouse and address and clean_text(warehouse).lower() == clean_text(address).lower():
        address = ""

    payment_obj = first_non_empty(
        val(order, "payment_option", "payment_method", "payment", "payment_type", default=""),
        find_first(order, ("payment_option", "payment_method", "payment_type")),
    )
    delivery_method = first_non_empty(
        val(order, "delivery_option", "delivery_method", "delivery_type", "shipping_method", default=""),
        get_from_sources(sources, "delivery_method", "delivery_type", "shipping_method", "name"),
    )

    return {
        "order_id": pick_text(val(order, "id", "order_id", default="")),
        "status": pick_text(val(order, "status", "order_status", default="")),
        "order_date": format_date(val(order, "date_created", "created_at", "date", "datetime", "time_created", default="")),
        "client_name": extract_client_name(order),
        "phone": pick_text(val(order, "phone", "client_phone", "buyer_phone", "customer_phone", default="") or find_first(order, ("phone", "client_phone", "buyer_phone", "customer_phone", "recipient_phone_number"))) or "—",
        "email": pick_text(val(order, "email", "client_email", "buyer_email", "customer_email", default="") or find_first(order, ("email", "client_email", "buyer_email", "customer_email"))) or "—",
        "total_price": money(val(order, "price", "full_price", "total_price", "total", "sum", default="")),
        "payment": pick_text(payment_obj) or "—",
        "payment_status": payment_status_from_order(order),
        "delivery": pick_text(delivery_method) or "—",
        "delivery_provider": "",  # not shown in Telegram anymore
        "delivery_city": city or "—",
        "delivery_warehouse": warehouse or "—",
        "delivery_address": address or "—",
        "comment": pick_text(val(order, "client_notes", "comment", "note", "notes", "buyer_comment", default="")) or "—",
        "order_url": pick_text(val(order, "url", "link", "order_url", default="")) or "—",
        "items": extract_order_items(order),
    }



def first_dict_by_keys(data: Any, keys: tuple[str, ...]) -> dict:
    """Recursively find the first dict stored under one of the key names."""
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, dict):
                return value
        for value in data.values():
            found = first_dict_by_keys(value, keys)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = first_dict_by_keys(item, keys)
            if found:
                return found
    return {}


def name_from_person(obj: Any) -> str:
    if not isinstance(obj, dict):
        return pick_text(obj)
    direct = first_non_empty(
        obj.get("full_name"), obj.get("display_name"), obj.get("name"), obj.get("username"),
        obj.get("title"), obj.get("fio"), obj.get("label"),
    )
    if direct:
        return pick_text(direct)
    parts = [
        pick_text(obj.get("first_name") or obj.get("firstname")),
        pick_text(obj.get("second_name") or obj.get("middle_name") or obj.get("middlename")),
        pick_text(obj.get("last_name") or obj.get("lastname")),
    ]
    return " ".join(part for part in parts if part).strip()


def product_dict_from_any(data: Any) -> dict:
    keys = (
        "product", "item", "good", "goods", "product_info", "item_info", "context_product",
        "related_product", "portal_product", "product_card", "source_product", "subject_product",
    )
    found = first_dict_by_keys(data, keys)
    return found if isinstance(found, dict) else {}


def product_name_from_any(data: Any) -> str:
    product = product_dict_from_any(data)
    candidates = [
        dig(product, "name_multilang", "uk", default=""),
        dig(product, "name_multilang", "ua", default=""),
        val(product, "name_uk", "product_name_uk", "title_uk", default=""),
        val(data, "product_name_uk", "product_title_uk", "title_uk", default="") if isinstance(data, dict) else "",
        val(product, "name", "product_name", "title", default=""),
        find_first(data, ("product_name_uk", "product_title_uk", "product_name", "product_title", "item_name", "good_name")),
    ]
    return clean_product_name(first_non_empty(*candidates))


def _iter_nested_values(data: Any):
    if isinstance(data, dict):
        for value in data.values():
            yield value
            yield from _iter_nested_values(value)
    elif isinstance(data, list):
        for item in data:
            yield item
            yield from _iter_nested_values(item)


def _find_company_id_for_prom_subdomain(data: Any) -> str:
    """Prom chat room ident usually looks like buyerId_companyId_buyer.
    For your shop this gives 4215376 -> https://cs4215376.prom.ua/.
    """
    for value in _iter_nested_values(data):
        text = pick_text(value)
        if not text:
            continue
        m = re.search(r"(?:^|\D)\d{3,}_(\d{5,})_buyer(?:$|\D)", text)
        if m:
            return m.group(1)
    return ""


def product_url_fallback_from_id(product_id: Any, data: Any) -> str:
    pid = pick_text(product_id).strip()
    if not pid or pid == "—" or not re.fullmatch(r"\d+", pid):
        return ""

    # Optional manual override in .env, for example:
    # STORE_PUBLIC_URL=https://cs4215376.prom.ua
    base = (
        os.getenv("STORE_PUBLIC_URL")
        or os.getenv("STORE_BASE_URL")
        or os.getenv("PROM_STORE_URL")
        or ""
    ).strip().rstrip("/")

    if not base:
        company_id = _find_company_id_for_prom_subdomain(data)
        if company_id:
            base = f"https://cs{company_id}.prom.ua"

    if base:
        # Prom normally redirects/opens by product id even without the slug.
        return f"{base}/p{pid}.html"

    # Last fallback. This is still a product page by product id, not an image URL.
    return f"https://prom.ua/ua/p{pid}.html"


def product_url_from_any(data: Any) -> str:
    """Return a real product URL, not an image URL from Prom chat context.

    Prom Chat often gives only context_item_id + context_item_image_url.
    When the real product URL is missing, build a product link from the context product id
    and the shop id inside room ident, for example:
    40029140_4215376_buyer + 3096794416 -> https://cs4215376.prom.ua/p3096794416.html
    """
    product = product_dict_from_any(data)

    candidates: list[Any] = []
    if isinstance(product, dict):
        for key in (
            "product_url", "portal_url", "site_url", "product_link", "link", "url", "href",
            "external_url", "public_url", "landing_url", "page_url",
        ):
            candidates.append(product.get(key))
    if isinstance(data, dict):
        for key in (
            "product_url", "portal_url", "product_link", "site_url", "link", "url", "href",
            "external_url", "public_url", "landing_url", "page_url",
        ):
            candidates.append(data.get(key))

    # Only search product-specific keys globally. Generic url/href often finds product images.
    candidates.append(find_first(data, (
        "product_url", "portal_url", "product_link", "site_url",
        "external_url", "public_url", "landing_url", "page_url",
    )))

    for candidate in candidates:
        url = pick_text(candidate).strip()
        if url and url != "—" and re.match(r"^https?://", url) and not is_image_url(url):
            return url

    # If Prom gave only a product context id, construct a real product page link.
    fallback = product_url_fallback_from_id(product_id_from_any(data), data)
    if fallback and not is_image_url(fallback):
        return fallback
    return ""


def product_id_from_any(data: Any) -> str:
    product = product_dict_from_any(data)

    direct_context_id = ""
    if isinstance(data, dict):
        item_type = pick_text(data.get("context_item_type") or data.get("item_type")).strip().lower()
        if item_type in {"product", "good", "item"}:
            direct_context_id = pick_text(data.get("context_item_id") or data.get("context_product_id"))

    nested_context_id = ""
    nested_type = pick_text(find_first(data, ("context_item_type", "item_type"))).strip().lower()
    if nested_type in {"product", "good", "item"}:
        nested_context_id = pick_text(find_first(data, ("context_item_id", "context_product_id")))

    return pick_text(
        val(product, "id", "product_id", "item_id", "good_id", default="")
        or direct_context_id
        or (val(data, "product_id", "item_id", "good_id", default="") if isinstance(data, dict) else "")
        or nested_context_id
        or find_first(data, ("product_id", "item_id", "good_id"))
    )


def sku_from_any(data: Any) -> str:
    product = product_dict_from_any(data)
    return pick_text(
        val(product, "sku", "article", "vendor_code", "external_id", default="")
        or (val(data, "sku", "article", "vendor_code", "external_id", default="") if isinstance(data, dict) else "")
        or find_first(data, ("sku", "article", "vendor_code", "external_id"))
    )


def message_sender_dict(message: dict) -> dict:
    # Incoming Prom Chat messages often keep buyer name under sender/from/author.
    for key in ("sender", "from", "author", "user", "from_user", "participant", "client", "customer", "buyer"):
        value = message.get(key) if isinstance(message, dict) else None
        if isinstance(value, dict):
            return value
    return first_dict_by_keys(message, ("sender", "from", "author", "from_user", "participant", "buyer", "client", "customer"))

def extract_message_summary(message: dict) -> dict[str, Any]:
    room = message.get("__chat_room") or {}
    if not isinstance(room, dict):
        room = {}
    related_order = message.get("__related_order") or {}
    if not isinstance(related_order, dict):
        related_order = {}
    related_summary = extract_order_summary(related_order) if related_order else {}
    related_items = related_summary.get("items") or []

    sender_obj = message_sender_dict(message)
    product = product_dict_from_any(message) or product_dict_from_any(room)
    client_obj = first_non_empty(
        message.get("client"), message.get("customer"), message.get("buyer"),
        room.get("client"), room.get("customer"), room.get("buyer"),
        sender_obj,
    )
    if not isinstance(client_obj, dict):
        client_obj = {}

    text = first_non_empty(
        val(message, "message", "text", "body", "content", "content_text", "html", default=""),
        val(message, "last_message", "last_text", default=""),
        val(room, "last_message", "last_text", default=""),
        find_first(message, ("message", "text", "body", "content", "content_text")),
    )
    mid = pick_text(val(message, "id", "message_id", "chat_message_id", "msg_id", "uuid", default=""))
    if not mid and message.get("__prom_channel") == "chat" and message.get("__chat_room_id"):
        mid = f"chat:{message.get('__chat_room_id')}"

    client_name = first_non_empty(
        val(message, "user_name", "client_name", "customer_name", "buyer_name", "sender_name", "author_name", "from_name", "name", default=""),
        name_from_person(client_obj),
        name_from_person(sender_obj),
        val(room, "user_name", "client_name", "customer_name", "buyer_name", "sender_name", "title", "name", default=""),
        related_summary.get("client_name", ""),
    )
    if isinstance(client_obj, dict) and (client_obj.get("first_name") or client_obj.get("last_name")):
        client_name = name_from_person(client_obj)

    direct_product_name = clean_product_name(first_non_empty(
        product_name_from_any(message),
        product_name_from_any(room),
        val(product, "name_uk", "product_name_uk", "title_uk", default="") if isinstance(product, dict) else "",
        dig(product, "name_multilang", "uk", default="") if isinstance(product, dict) else "",
        dig(product, "name_multilang", "ua", default="") if isinstance(product, dict) else "",
        val(message, "product_name", "product_title", default=""),
        val(product, "name", "product_name", "title", default="") if isinstance(product, dict) else "",
    ))

    direct_sku = pick_text(first_non_empty(
        sku_from_any(message),
        sku_from_any(room),
        val(product, "sku", "article", "vendor_code", "external_id", default="") if isinstance(product, dict) else "",
    ))
    direct_url = pick_text(first_non_empty(
        product_url_from_any(message),
        product_url_from_any(room),
        val(product, "url", "link", "product_url", "portal_url", default="") if isinstance(product, dict) else "",
    ))
    direct_product_id = pick_text(first_non_empty(
        product_id_from_any(message),
        product_id_from_any(room),
        val(product, "id", "product_id", default="") if isinstance(product, dict) else "",
    ))

    products_text = ""
    if related_items:
        lines = []
        for idx, item in enumerate(related_items[:6], 1):
            opts = _item_options_from_any(item)
            opt_text = "; ".join(f"{a}: {b}" for a, b in opts[:3])
            sku = pick_text(item.get("sku"))
            qty = format_quantity(item.get("quantity"), item.get("measure_unit"))
            # In Telegram chat notifications we do NOT show calculated size separately.
            # Size is already encoded in SKU, and full details remain in Telegraph/order page.
            opts = [(a, b) for a, b in opts if not re.search(r"розмір|ширина|висота|довжина", a, flags=re.I)]
            opt_text = "; ".join(f"{a}: {b}" for a, b in opts[:3])
            line = f"{idx}. {pick_text(item.get('name'))}"
            if sku and sku != "—":
                line += f" | Артикул: {sku}"
            if opt_text:
                line += f" | {opt_text}"
            line += f" | К-сть: {qty}"
            lines.append(line)
        if len(related_items) > 6:
            lines.append(f"… ще {len(related_items) - 6} товарів")
        products_text = "\n".join(lines)

    if not direct_product_name and related_items:
        direct_product_name = pick_text(related_items[0].get("name")) if len(related_items) == 1 else f"Кілька товарів ({len(related_items)} позицій)"
    if (not direct_sku or direct_sku == "—") and related_items:
        direct_sku = pick_text(related_items[0].get("sku"))
    if (not direct_url or direct_url == "—") and related_items:
        direct_url = pick_text(related_items[0].get("product_url"))
    if (not direct_product_id or direct_product_id == "—") and related_items:
        direct_product_id = pick_text(related_items[0].get("product_id"))

    phone = first_non_empty(
        val(message, "user_phone", "phone", "client_phone", "sender_phone", "customer_phone", "buyer_phone", default=""),
        val(client_obj, "phone", "phone_number", default=""),
        val(sender_obj, "phone", "phone_number", default=""),
        related_summary.get("phone", ""),
        find_first(message, ("user_phone", "phone", "client_phone", "sender_phone", "customer_phone", "buyer_phone")),
    )
    email = first_non_empty(
        val(message, "user_email", "email", "client_email", "sender_email", "customer_email", "buyer_email", default=""),
        val(client_obj, "email", default=""),
        val(sender_obj, "email", default=""),
        related_summary.get("email", ""),
        find_first(message, ("user_email", "email", "client_email", "sender_email", "customer_email", "buyer_email")),
    )

    return {
        "message_id": mid,
        "message_date": format_date(val(message, "date_created", "created_at", "date", "datetime", "created", "sent_at", "date_sent", default="") or val(room, "date_sent", "date_created", "created_at", "date", "datetime", default="")),
        "status": pick_text(val(message, "status", default="") or val(room, "status", default="")),
        "client_name": pick_text(client_name) or "—",
        "phone": pick_text(phone) or "—",
        "email": pick_text(email) or "—",
        "order_id": pick_text(val(message, "order_id", default="") or val(room, "order_id", default="") or message.get("__related_order_id") or related_summary.get("order_id", "")) or "—",
        "product_id": direct_product_id or "—",
        "product_name": direct_product_name or "—",
        "sku": direct_sku or "—",
        "size": parse_size_from_sku(direct_sku) or "—",
        "product_url": direct_url or "—",
        "products_text": products_text,
        "text": pick_text(text) or "—",
    }

def _boolish(value: Any) -> bool | None:
    """Return True/False for Prom/API boolean values including 1/0 strings."""
    if value is True:
        return True
    if value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value == 1:
            return True
        if value == 0:
            return False
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "так", "да", "on"}:
        return True
    if text in {"0", "false", "no", "n", "ні", "нет", "off", "none", "null", ""}:
        return False
    return None


def is_outgoing_message(message: dict) -> bool:
    """Detect messages sent by the seller/shop so Telegram receives only clients' texts.

    Prom Chat usually has `is_sender`: false = buyer/client, true = shop/seller.
    This version also understands 1/0 and cleans old DB rows where our replies were saved as client messages.
    """
    if not isinstance(message, dict):
        return False

    def text_at(*keys: str) -> str:
        return str(val(message, *keys, default="") or "").strip().lower()

    incoming_markers = {"in", "incoming", "inbound", "buyer", "client", "customer", "user", "visitor", "lead"}
    outgoing_markers = {"out", "outgoing", "outbound", "seller", "company", "shop", "store", "me", "owner", "operator", "manager", "admin", "merchant"}

    # Prom Chat's strongest flag. Check it before generic `type`, because `type=message`
    # exists on both buyer and seller messages.
    if "is_sender" in message:
        b = _boolish(message.get("is_sender"))
        if b is not None:
            return b

    for key in ("is_outgoing", "outgoing", "from_company", "from_seller", "by_company", "is_shop", "is_operator", "is_manager", "is_admin", "is_mine", "is_my", "from_me", "mine"):
        b = _boolish(message.get(key)) if key in message else None
        if b is True:
            return True
    for key in ("is_incoming", "incoming", "from_buyer", "from_client", "from_customer", "is_client", "is_buyer", "is_customer"):
        b = _boolish(message.get(key)) if key in message else None
        if b is True:
            return False

    for key in ("direction", "message_direction", "sender_type", "author_type", "from_type", "side", "role", "sender_role", "author_role"):
        value = text_at(key)
        if value in incoming_markers:
            return False
        if value in outgoing_markers:
            return True

    sender = message_sender_dict(message)
    if isinstance(sender, dict):
        for key in ("is_sender", "is_seller", "is_company", "is_shop", "is_operator", "is_owner", "is_me", "me", "from_me", "mine"):
            b = _boolish(sender.get(key)) if key in sender else None
            if b is True:
                return True
        for key in ("is_buyer", "is_client", "is_customer", "incoming"):
            b = _boolish(sender.get(key)) if key in sender else None
            if b is True:
                return False
        for key in ("type", "role", "sender_type", "author_type", "from_type", "side", "kind"):
            value = str(sender.get(key) or "").strip().lower()
            if value in incoming_markers:
                return False
            if value in outgoing_markers:
                return True

    room = message.get("__chat_room") if isinstance(message.get("__chat_room"), dict) else {}
    if isinstance(room, dict):
        for key in ("last_message_is_sender", "is_sender"):
            b = _boolish(room.get(key)) if key in room else None
            if b is True:
                return True
            if b is False:
                return False
        for key in ("last_message_direction", "last_message_sender_type", "last_sender_type"):
            value = str(room.get(key) or "").strip().lower()
            if value in incoming_markers:
                return False
            if value in outgoing_markers:
                return True

    # Last-resort shop-name fallback. Do not use this as the main rule, because a buyer
    # could theoretically have a similar name. It only helps old/polluted local rows.
    shop_names = {"ролл сан", "sunroll", "sun roll", "roll sun"}
    uname = text_at("user_name", "sender_name", "author_name", "from_name")
    if uname in shop_names:
        return True

    return False

def is_image_url(url: Any) -> bool:
    u = pick_text(url).strip().lower()
    if not u:
        return False
    return (
        "images.prom.ua" in u
        or re.search(r"\.(jpg|jpeg|png|webp|gif)(?:[?#].*)?$", u) is not None
        or "/w100_h100_" in u
        or "/w200_h200_" in u
        or "/w640_h640_" in u
    )


def tg_link(url: str, label: str) -> str:
    url = pick_text(url).strip()
    # Never show product image URLs as “відкрити товар”.
    if is_image_url(url):
        return "—"
    if url and url != "—" and re.match(r"^https?://", url):
        return f'<a href="{e(url)}">{e(label)}</a>'
    return "—"


def _item_options_from_any(item: dict[str, Any]) -> list[tuple[str, str]]:
    opts = item.get("options") or []
    if isinstance(opts, list) and opts and isinstance(opts[0], (tuple, list)):
        return [(pick_text(a), pick_text(b)) for a, b in opts if pick_text(b)]
    text = pick_text(item.get("options_text") or "")
    result: list[tuple[str, str]] = []
    if text:
        for part in re.split(r";\s*", text):
            if not part.strip():
                continue
            if ":" in part:
                a, b = part.split(":", 1)
                result.append((a.strip(), b.strip()))
            else:
                result.append(("Різновид", part.strip()))
    return result


def format_item(item: dict[str, Any], idx: int) -> str:
    lines = [f"<b>{idx}. {e(item['name'])}</b>"]
    if pick_text(item.get("sku")) not in ("", "—"):
        lines.append(f"🔢 Артикул: <code>{e(item['sku'])}</code>")
    elif pick_text(item.get("product_id")) not in ("", "—"):
        lines.append(f"🆔 ID товару: <code>{e(item['product_id'])}</code>")

    options = _item_options_from_any(item)
    if options:
        lines.append("📐 <b>Розмір / різновид:</b>")
        for label, value in options[:12]:
            lines.append(f"• {e(label)}: <b>{e(value)}</b>")

    qty = pick_text(item.get("quantity")) or "1"
    price = pick_text(item.get("price")) or "—"
    total = pick_text(item.get("total_price")) or "—"
    lines.append(f"📦 Кількість: <b>{e(qty)}</b>")
    if total != "—" and total != price:
        lines.append(f"💰 Ціна: {e(price)} | Разом: <b>{e(total)}</b>")
    else:
        lines.append(f"💰 Ціна: <b>{e(price)}</b>")
    link = tg_link(item.get("product_url", ""), "відкрити товар")
    if link != "—":
        lines.append(f"🔗 {link}")
    return "\n".join(lines)


def format_order(order: dict, store_name: str) -> str:
    s = extract_order_summary(order)
    items_text = []
    for idx, item in enumerate(s["items"], 1):
        items_text.append(format_item(item, idx))
    if not items_text:
        items_text.append("—")

    address_line = s["delivery_address"] if s["delivery_address"] != "—" else ""
    warehouse_line = s["delivery_warehouse"] if s["delivery_warehouse"] != "—" else ""
    point_value = warehouse_line or address_line or "—"
    order_link = tg_link(s["order_url"], "відкрити замовлення")

    text = (
        f"🆕 <b>Нове замовлення</b>\n"
        f"🏪 <b>{e(store_name)}</b>\n"
        f"🧾 № <code>{e(s['order_id'])}</code>\n"
        f"📅 {e(s['order_date'])}\n"
        f"📌 Статус: <b>{e(human_status(s['status']))}</b>\n"
    )
    if order_link != "—":
        text += f"🔗 {order_link}\n"

    text += (
        f"\n👤 <b>Клієнт</b>\n"
        f"{e(s['client_name'])}\n"
        f"📞 <code>{e(s['phone'])}</code>\n"
        f"✉️ {e(s['email'])}\n\n"
        f"🛒 <b>Що замовили</b>\n"
        + "\n\n".join(items_text)
        + "\n\n"
        f"💰 <b>Сума:</b> {e(s['total_price'])}\n"
        f"💳 <b>Оплата:</b> {e(s['payment'])}\n\n"
        f"🚚 <b>Доставка</b>\n"
        f"Спосіб: {e(s['delivery'])}\n"
        f"Місто: <b>{e(s['delivery_city'])}</b>\n"
        f"Відділення/адреса: {e(point_value)}\n"
    )
    if warehouse_line and address_line and warehouse_line != address_line:
        text += f"Адреса: {e(address_line)}\n"
    text += f"\n💬 <b>Коментар:</b> {e(s['comment'])}"
    return text




def _first_url_from_dict(data: Any) -> str:
    """Find a usable URL inside an attachment-like object without grabbing product context images."""
    if not isinstance(data, dict):
        return ""
    preferred = (
        "url", "file_url", "download_url", "original_url", "source_url", "content_url",
        "attachment_url", "document_url", "photo_url", "image_url", "src", "href", "link",
    )
    for key in preferred:
        value = pick_text(data.get(key))
        if value.startswith("http://") or value.startswith("https://"):
            return value
    # one-level recursive fallback for nested file/source structures
    for value in data.values():
        if isinstance(value, dict):
            found = _first_url_from_dict(value)
            if found:
                return found
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    found = _first_url_from_dict(item)
                    if found:
                        return found
    return ""


def _filename_from_url(url: str) -> str:
    try:
        name = re.sub(r"[?#].*$", "", url.rstrip("/")).split("/")[-1]
        return name or "файл"
    except Exception:
        return "файл"


def _normalize_attachment(item: Any) -> dict[str, str] | None:
    """Normalize Prom chat/file attachment into {url, kind, name, mime}."""
    if is_empty(item):
        return None

    if isinstance(item, str):
        url = item.strip()
        if not re.match(r"^https?://", url):
            return None
        name = _filename_from_url(url)
        mime = ""
        raw_kind = ""
    elif isinstance(item, dict):
        url = _first_url_from_dict(item)
        if not url:
            return None
        name = pick_text(first_non_empty(
            item.get("name"), item.get("filename"), item.get("file_name"), item.get("title"), item.get("caption"),
            item.get("original_name"), item.get("display_name"), _filename_from_url(url),
        ))
        mime = pick_text(first_non_empty(
            item.get("mime"), item.get("mime_type"), item.get("content_type"), item.get("type_mime"),
        )).lower()
        raw_kind = pick_text(first_non_empty(
            item.get("type"), item.get("kind"), item.get("media_type"), item.get("file_type"), item.get("attachment_type"),
        )).lower()
    else:
        return None

    low_url = url.lower()
    low_name = name.lower()
    is_pdf_file = "pdf" in mime or "pdf" in raw_kind or low_url.endswith(".pdf") or low_name.endswith(".pdf")
    is_img = (
        "image" in mime
        or raw_kind in {"image", "photo", "picture", "img"}
        or is_image_url(url)
    )
    if is_pdf_file:
        kind = "pdf"
    elif is_img:
        kind = "photo"
    else:
        kind = "file"

    return {"url": url, "kind": kind, "name": name or ("фото" if kind == "photo" else "файл"), "mime": mime}


def extract_message_attachments(message: dict) -> list[dict[str, str]]:
    """Return photos/PDFs/files attached by a client to a Prom message.

    Supports common Prom/API shapes: attachments, files, photos, document, media.
    Product context image URLs are ignored because they are товарні фото, not client files.
    """
    if not isinstance(message, dict):
        return []

    attachment_keys = (
        "attachments", "attachment", "files", "file", "documents", "document",
        "photos", "photo", "media", "medias", "uploaded_files", "uploads",
    )

    raw_items: list[Any] = []
    for key in attachment_keys:
        value = message.get(key)
        if is_empty(value):
            continue
        if isinstance(value, list):
            raw_items.extend(value)
        else:
            raw_items.append(value)

    # Some APIs return a file-like message directly: {type: "photo", url: "..."}.
    # Do not do this for Prom product context messages, because their image is just a product thumbnail.
    msg_type = pick_text(message.get("type") or message.get("message_type") or message.get("media_type")).lower()
    if msg_type in {"photo", "image", "picture", "file", "document", "attachment", "pdf"}:
        raw_items.append(message)

    result: list[dict[str, str]] = []
    seen: set[str] = set()
    context_image = pick_text(message.get("context_item_image_url"))
    for item in raw_items:
        att = _normalize_attachment(item)
        if not att:
            continue
        url = att.get("url", "")
        if not url or url in seen:
            continue
        if context_image and url == context_image:
            continue
        seen.add(url)
        result.append(att)
    return result


def format_attachments_block(attachments: list[dict[str, str]]) -> str:
    """Only show a compact attachment counter in the text card.

    The actual photos/PDF/files are sent below the card by main.py as Telegram media.
    No “відкрити фото/PDF” links here.
    """
    if not attachments:
        return ""
    photo_count = sum(1 for a in attachments if a.get("kind") == "photo")
    pdf_count = sum(1 for a in attachments if a.get("kind") == "pdf")
    file_count = sum(1 for a in attachments if a.get("kind") not in {"photo", "pdf"})
    parts = []
    if photo_count:
        parts.append(f"{photo_count} фото")
    if pdf_count:
        parts.append(f"{pdf_count} PDF")
    if file_count:
        parts.append(f"{file_count} файл(и)")
    return "📎 <b>Вкладення:</b> " + ", ".join(parts)

def format_prom_message(message: dict, store_name: str, *, is_new: bool = True) -> str:
    s = extract_message_summary(message)
    attachments = extract_message_attachments(message)
    product_link = tg_link(s.get("product_url", ""), "відкрити товар")

    # User-facing compact layout for Telegram.
    # New live notifications say “Нове повідомлення”,
    # but messages opened from the “Повідомлення” menu are shown as regular history cards.
    title = "Нове повідомлення" if is_new else "Повідомлення"
    text = f"💬 <b>{title} з {e(store_name)}!</b>"

    message_date = format_date(s.get("message_date", ""))
    if message_date and message_date != "—":
        text += f"\n🕒 {e(message_date)}"

    text += f"\n\n👤 <b>{e(s.get('client_name') or '—')}</b>"

    product_name = pick_text(s.get("product_name", ""))
    sku = pick_text(s.get("sku", ""))
    products_text = pick_text(s.get("products_text", ""))

    if product_name and product_name != "—":
        text += f"\n\n🛒 <b>{e(product_name)}</b>"
        if sku and sku != "—":
            text += f"\n🔢 Артикул: <code>{e(sku)}</code>"
        if product_link != "—":
            text += f"\n🔗 {product_link}"
    elif products_text:
        text += f"\n\n🛒 <b>Товари / пов’язане замовлення:</b>\n{e(products_text)}"
    elif product_link != "—":
        text += f"\n\n🔗 {product_link}"

    body_text = pick_text(s.get('text') or '')
    if not body_text or body_text == '—':
        body_text = "—"

    text += f"\n\n💬 <b>Текст:</b>\n{e(body_text)}"

    attachments_block = format_attachments_block(attachments)
    if attachments_block:
        text += f"\n\n{attachments_block}"

    # Самі фото/PDF/файли main.py надсилає окремо нижче повідомлення.

    text += "\n\n↩️ Відповідь: зроби <b>reply</b> на це повідомлення."
    return text

def format_order_from_db(order, items) -> str:
    if not order:
        return "Замовлення не знайдено в локальній базі."
    items_text = []
    for idx, item in enumerate(items, 1):
        item_data = {
            "name": item["product_name"],
            "sku": item["sku"],
            "quantity": item["quantity"],
            "price": item["price"],
            "total_price": item["total_price"],
            "product_url": item["product_url"],
            "product_id": item["product_id"],
            "options_text": item["options_text"] if "options_text" in item.keys() else "",
        }
        items_text.append(format_item(item_data, idx))

    city = clean_city(order["delivery_city"] or "") or "—"
    address = remove_city_from_address(order["delivery_address"] or "", city)
    warehouse = clean_text(order["delivery_warehouse"] or "")
    point_value = warehouse if warehouse and warehouse != "—" else (address if address else "—")

    text = (
        f"🧾 <b>Замовлення № {e(order['order_id'])}</b>\n\n"
        f"📌 Статус: <b>{e(human_status(order['prom_status']))}</b>\n"
        f"📅 Дата: {e(format_date(order['order_date']))}\n\n"
        f"👤 <b>Клієнт</b>\n"
        f"{e(order['client_name'])}\n"
        f"📞 <code>{e(order['phone'])}</code>\n"
        f"✉️ {e(order['email'])}\n\n"
        f"🛒 <b>Що замовили</b>\n"
        + ("\n\n".join(items_text) if items_text else "—")
        + "\n\n"
        f"💰 <b>Сума:</b> {e(order['total_price'])}\n"
        f"💳 <b>Оплата:</b> {e(order['payment'])}\n\n"
        f"🚚 <b>Доставка</b>\n"
        f"Спосіб: {e(order['delivery'])}\n"
        f"Місто: <b>{e(city)}</b>\n"
        f"Відділення/адреса: {e(point_value)}"
    )
    if warehouse and address and warehouse.lower() != address.lower():
        text += f"\nАдреса: {e(address)}"
    if order["comment"]:
        text += f"\n\n💬 <b>Коментар:</b> {e(order['comment'])}"
    return text



def human_status(status: Any) -> str:
    raw = pick_text(status).strip()
    key = raw.lower()
    mapping = {
        "pending": "🆕 Нове",
        "new": "🆕 Нове",
        "received": "✅ Прийнято",
        "accepted": "✅ Прийнято",
        "processing": "🔄 В роботі",
        "paid": "💰 Оплачено",
        "delivered": "📦 Виконано",
        "completed": "📦 Виконано",
        "done": "📦 Виконано",
        "canceled": "❌ Скасовано",
        "cancelled": "❌ Скасовано",
        "declined": "❌ Скасовано",
        "draft": "📝 Чернетка",
        "новий": "🆕 Нове",
        "новое": "🆕 Нове",
        "новый": "🆕 Нове",
        "прийнято": "✅ Прийнято",
        "принято": "✅ Прийнято",
        "принят": "✅ Прийнято",
        "оплачено": "💰 Оплачено",
        "оплачено/підтверджено": "💰 Оплачено",
        "виконано": "📦 Виконано",
        "выполнено": "📦 Виконано",
        "выполнен": "📦 Виконано",
        "доставлено": "📦 Виконано",
        "скасовано": "❌ Скасовано",
        "отменено": "❌ Скасовано",
        "отменен": "❌ Скасовано",
    }
    return mapping.get(key, f"📌 {raw or '—'}")


def strip_html(text: str) -> str:
    """Make Telegram HTML text readable in .txt files."""
    text = re.sub(r"<a\s+href=\"([^\"]+)\">([^<]+)</a>", r"\2: \1", text)
    text = re.sub(r"</?(b|strong|i|em|u|s|code|pre)>", "", text)
    text = re.sub(r"<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&").replace("&quot;", '"')
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def order_total_quantity(items: list[dict[str, Any]]) -> str:
    total = 0.0
    has_number = False
    for item in items:
        raw = pick_text(item.get("quantity") or "")
        m = re.search(r"\d+(?:[\.,]\d+)?", raw)
        if m:
            has_number = True
            total += float(m.group(0).replace(",", "."))
    if not has_number:
        return str(len(items))
    if total.is_integer():
        return str(int(total))
    return str(total).replace(".", ",")


def format_order_short(order: dict, store_name: str) -> str:
    """Compact Telegram card for a new order. Full data goes to Telegraph."""
    s = extract_order_summary(order)
    items = s.get("items") or []

    sku_lines: list[str] = []
    for item in items:
        sku = pick_text(item.get("sku") or "").strip()
        pid = pick_text(item.get("product_id") or "").strip()
        qty = pick_text(item.get("quantity") or "").strip()
        code = sku or (f"ID {pid}" if pid and pid != "—" else "—")
        if code and code != "—":
            q = f" × {e(qty)}" if qty and qty not in {"—", "1", "1 шт.", "1 шт"} else ""
            sku_lines.append(f"• <code>{e(code)}</code>{q}")
    if not sku_lines:
        sku_lines.append("• —")

    payment = pick_text(s.get("payment") or "—") or "—"
    payment_status = pick_text(s.get("payment_status") or "").strip()
    delivery_point = delivery_point_from_summary(s)

    text = (
        f"🆕 <b>Нове замовлення з {e(store_name)}!</b>\n"
        f"🧾 № <code>{e(s['order_id'])}</code>\n"
        f"🕒 {e(s['order_date'])}\n\n"
        f"👤 <b>{e(s['client_name'])}</b>\n"
        f"📞 <code>{e(s['phone'])}</code>\n\n"
        f"🛒 <b>Артикул(и):</b>\n"
        + "\n".join(sku_lines)
        + "\n\n"
        f"🚚 <b>Доставка:</b> {e(s['delivery'])}\n"
        f"📍 {e(delivery_point)}\n\n"
        f"💳 <b>Оплата:</b> {e(payment)}"
    )

    if is_prom_payment(payment, order):
        status_text = payment_status or "Не оплачено"
        text += f"\n{payment_status_emoji(status_text)} <b>Статус оплати:</b> {e(status_text)}"

    return text




def _row_get(row: Any, key: str, default: Any = "") -> Any:
    try:
        if hasattr(row, "keys") and key in row.keys():
            value = row[key]
            return default if is_empty(value) else value
    except Exception:
        pass
    if isinstance(row, dict):
        return row.get(key, default)
    return default


def format_order_short_from_db(order: Any, items: list[Any], store_name: str) -> str:
    """Compact Telegram card for an order opened from /orders.

    It mirrors the new-order notification: only key operational fields in Telegram,
    while the full order data is kept in Telegraph.
    """
    if not order:
        return "Замовлення не знайдено в локальній базі."

    # Try to use the original Prom JSON for payment status and other nested fields.
    raw_order: dict[str, Any] = {}
    try:
        raw_json = _row_get(order, "raw_json", "")
        if raw_json:
            raw_order = json.loads(raw_json)
    except Exception:
        raw_order = {}

    if raw_order:
        s = extract_order_summary(raw_order)
    else:
        s = {}

    # Override with database fields, because local DB can contain updated status/Telegraph info.
    s.update(
        {
            "order_id": pick_text(_row_get(order, "order_id", s.get("order_id", ""))),
            "status": pick_text(_row_get(order, "prom_status", s.get("status", ""))),
            "order_date": format_date(_row_get(order, "order_date", s.get("order_date", ""))),
            "client_name": pick_text(_row_get(order, "client_name", s.get("client_name", ""))) or "—",
            "phone": pick_text(_row_get(order, "phone", s.get("phone", ""))) or "—",
            "email": pick_text(_row_get(order, "email", s.get("email", ""))) or "—",
            "total_price": pick_text(_row_get(order, "total_price", s.get("total_price", ""))) or "—",
            "payment": pick_text(_row_get(order, "payment", s.get("payment", ""))) or "—",
            "delivery": pick_text(_row_get(order, "delivery", s.get("delivery", ""))) or "—",
            "delivery_city": clean_city(_row_get(order, "delivery_city", s.get("delivery_city", ""))) or "—",
            "delivery_warehouse": pick_text(_row_get(order, "delivery_warehouse", s.get("delivery_warehouse", ""))) or "—",
            "delivery_address": pick_text(_row_get(order, "delivery_address", s.get("delivery_address", ""))) or "—",
            "comment": pick_text(_row_get(order, "comment", s.get("comment", ""))) or "—",
            "order_url": pick_text(_row_get(order, "order_url", s.get("order_url", ""))) or "—",
        }
    )

    # If DB has a dedicated payment_status column in a newer schema, prefer it.
    db_payment_status = pick_text(_row_get(order, "payment_status", "")).strip()
    if db_payment_status:
        s["payment_status"] = db_payment_status
    elif not s.get("payment_status") and raw_order:
        s["payment_status"] = payment_status_from_order(raw_order)

    item_dicts: list[dict[str, Any]] = []
    for item in items or []:
        item_dicts.append(
            {
                "product_id": pick_text(_row_get(item, "product_id", "")),
                "name": clean_product_name(_row_get(item, "product_name", "")) or "Товар",
                "sku": pick_text(_row_get(item, "sku", "")) or "—",
                "quantity": pick_text(_row_get(item, "quantity", "")) or "1",
                "price": pick_text(_row_get(item, "price", "")) or "—",
                "total_price": pick_text(_row_get(item, "total_price", "")) or "—",
                "product_url": pick_text(_row_get(item, "product_url", "")) or "—",
                "options_text": pick_text(_row_get(item, "options_text", "")),
            }
        )
    if item_dicts:
        s["items"] = item_dicts

    items_s = s.get("items") or []
    sku_lines: list[str] = []
    for item in items_s:
        sku = pick_text(item.get("sku") or "").strip()
        pid = pick_text(item.get("product_id") or "").strip()
        qty = pick_text(item.get("quantity") or "").strip()
        code = sku or (f"ID {pid}" if pid and pid != "—" else "—")
        if code and code != "—":
            q = f" × {e(qty)}" if qty and qty not in {"—", "1", "1 шт.", "1 шт"} else ""
            sku_lines.append(f"• <code>{e(code)}</code>{q}")
    if not sku_lines:
        sku_lines.append("• —")

    payment = pick_text(s.get("payment") or "—") or "—"
    payment_status = pick_text(s.get("payment_status") or "").strip()
    delivery_point = delivery_point_from_summary(s)

    text = (
        f"🧾 <b>Замовлення № {e(s.get('order_id') or '—')}</b>\n"
        f"📌 {e(human_status(s.get('status') or ''))}\n"
        f"🕒 {e(s.get('order_date') or '—')}\n\n"
        f"👤 <b>{e(s.get('client_name') or '—')}</b>\n"
        f"📞 <code>{e(s.get('phone') or '—')}</code>\n\n"
        f"🛒 <b>Артикул(и):</b>\n"
        + "\n".join(sku_lines)
        + "\n\n"
        f"🚚 <b>Доставка:</b> {e(s.get('delivery') or '—')}\n"
        f"📍 {e(delivery_point)}\n\n"
        f"💳 <b>Оплата:</b> {e(payment)}"
    )

    if is_prom_payment(payment, raw_order or None):
        status_text = payment_status or "Не оплачено"
        text += f"\n{payment_status_emoji(status_text)} <b>Статус оплати:</b> {e(status_text)}"

    return text

def format_orders_page(rows, offset: int = 0, total: int | None = None) -> str:
    if not rows:
        return "📦 <b>Замовлень у локальній базі поки немає.</b>"
    total_text = f" з {total}" if total is not None else ""
    return (
        f"📦 <b>Замовлення {offset + 1}–{offset + len(rows)}{total_text}</b>\n\n"
        "Натисни на потрібне замовлення нижче.\n"
        "Відкриється коротка картка, Telegraph і кнопки зміни статусу."
    )



def message_read_status(status: Any) -> bool:
    value = pick_text(status).strip().lower()
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


def human_message_status(status: Any) -> str:
    return "✅ Прочитане" if message_read_status(status) else "🔴 Непрочитане"


def format_messages_page(rows, offset: int = 0, total: int | None = None) -> str:
    if not rows:
        return "💬 <b>Повідомлень у локальній базі поки немає.</b>"

    total_text = f" з {total}" if total is not None else ""
    return (
        f"💬 <b>Повідомлення {offset + 1}–{offset + len(rows)}{total_text}</b>\n\n"
        "🔴 — непрочитано / треба відповісти\n"
        "🟢 — прочитано / вже відповіли\n\n"
        "Найновіші клієнти зверху, старіші знизу.\n"
        "Натисни на клієнта нижче, щоб відкрити переписку."
    )


# ---------- Telegraph page formatting ----------

def _tg_clean(value: Any) -> str:
    return clean_text(value) or "—"


def _node(tag: str, children: list[Any] | str, attrs: dict[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(children, str):
        children = [children]
    n: dict[str, Any] = {"tag": tag, "children": children}
    if attrs:
        n["attrs"] = attrs
    return n


def _p(*parts: Any) -> dict[str, Any]:
    children: list[Any] = []
    for i, part in enumerate(parts):
        if i:
            children.append({"tag": "br"})
        children.append(part if isinstance(part, dict) else str(part))
    return _node("p", children)


def _strong(text: Any) -> dict[str, Any]:
    return _node("strong", str(text))


def _link(url: Any, label: str) -> dict[str, Any] | str:
    url_t = pick_text(url).strip()
    if url_t and url_t != "—" and re.match(r"^https?://", url_t):
        return _node("a", label, {"href": url_t})
    return label


def _li(label: str, value: Any) -> dict[str, Any]:
    return _node("li", [_strong(label), f": {_tg_clean(value)}"])


def telegraph_nodes_from_order(order: dict, store_name: str) -> list[dict[str, Any]]:
    s = extract_order_summary(order)
    return telegraph_nodes_from_summary(s, store_name)


def telegraph_nodes_from_db(order, items) -> list[dict[str, Any]]:
    if not order:
        return [_p("Замовлення не знайдено в локальній базі.")]
    item_dicts = []
    for item in items:
        item_dicts.append(
            {
                "product_id": item["product_id"],
                "name": item["product_name"],
                "sku": item["sku"],
                "quantity": item["quantity"],
                "price": item["price"],
                "total_price": item["total_price"],
                "product_url": item["product_url"],
                "options_text": item["options_text"] if "options_text" in item.keys() else "",
            }
        )
    s = {
        "order_id": order["order_id"],
        "status": order["prom_status"],
        "order_date": format_date(order["order_date"]),
        "client_name": order["client_name"],
        "phone": order["phone"],
        "email": order["email"],
        "total_price": order["total_price"],
        "payment": order["payment"],
        "payment_status": "",
        "delivery": order["delivery"],
        "delivery_city": clean_city(order["delivery_city"] or "") or "—",
        "delivery_warehouse": order["delivery_warehouse"],
        "delivery_address": remove_city_from_address(order["delivery_address"] or "", order["delivery_city"] or ""),
        "comment": order["comment"],
        "order_url": order["order_url"],
        "items": item_dicts,
    }
    return telegraph_nodes_from_summary(s, order["store_name"] or "Prom магазин")


def telegraph_nodes_from_summary(s: dict[str, Any], store_name: str) -> list[dict[str, Any]]:
    """Full order page for Telegraph: the same useful info as the full Prom order, without raw/debug noise."""
    nodes: list[dict[str, Any]] = []
    nodes.append(_node("h3", f"Замовлення № {_tg_clean(s.get('order_id'))}"))

    payment = s.get("payment")
    payment_status = s.get("payment_status") or ""
    order_info = [
        _li("Магазин", store_name),
        _li("Дата", s.get("order_date")),
        _li("Статус замовлення", human_status(s.get("status"))),
        _li("Сума", s.get("total_price")),
        _li("Оплата", payment),
    ]
    if payment_status:
        order_info.append(_li("Статус оплати", payment_status))
    nodes.append(_node("ul", order_info))

    order_url = pick_text(s.get("order_url")).strip()
    if order_url and order_url != "—" and re.match(r"^https?://", order_url):
        nodes.append(_p(_link(order_url, "Відкрити замовлення в Prom")))

    nodes.append(_node("h3", "Покупець"))
    nodes.append(
        _node(
            "ul",
            [
                _li("Імʼя", s.get("client_name")),
                _li("Телефон", s.get("phone")),
                _li("Email", s.get("email")),
            ],
        )
    )

    nodes.append(_node("h3", "Товари"))
    items = s.get("items") or []
    if not items:
        nodes.append(_p("Товарів у відповіді Prom API немає."))
    for idx, item in enumerate(items, 1):
        nodes.append(_node("h4", f"{idx}. {_tg_clean(item.get('name'))}"))
        li_nodes = []
        if pick_text(item.get("sku")) not in ("", "—"):
            li_nodes.append(_li("Артикул", item.get("sku")))
        if pick_text(item.get("product_id")) not in ("", "—"):
            li_nodes.append(_li("ID товару", item.get("product_id")))
        for label, value in _item_options_from_any(item):
            li_nodes.append(_li(label, value))
        li_nodes.extend(
            [
                _li("Кількість", item.get("quantity")),
                _li("Ціна", item.get("price")),
                _li("Разом", item.get("total_price")),
            ]
        )
        product_url = pick_text(item.get("product_url")).strip()
        if product_url and product_url != "—" and re.match(r"^https?://", product_url):
            li_nodes.append(_node("li", [_link(product_url, "Відкрити товар")]))
        nodes.append(_node("ul", li_nodes))

    nodes.append(_node("h3", "Доставка"))
    city = clean_city(s.get("delivery_city") or "") or "—"
    address = remove_city_from_address(s.get("delivery_address") or "", city)
    warehouse = clean_text(s.get("delivery_warehouse") or "") or "—"
    delivery_items = [
        _li("Спосіб", s.get("delivery")),
        _li("Місто", city),
        _li("Відділення / адреса", warehouse),
    ]
    if address and address != "—" and address.lower() != warehouse.lower():
        delivery_items.append(_li("Адреса", address))
    nodes.append(_node("ul", delivery_items))

    comment = pick_text(s.get("comment") or "").strip()
    if comment and comment != "—":
        nodes.append(_node("h3", "Коментар клієнта"))
        nodes.append(_p(comment))
    return nodes


def telegraph_title_from_summary(summary: dict[str, Any]) -> str:
    oid = pick_text(summary.get("order_id") or "") or "замовлення"
    client = pick_text(summary.get("client_name") or "").strip()
    base = f"Prom замовлення № {oid}"
    if client and client != "—":
        base += f" — {client}"
    return base[:80]
