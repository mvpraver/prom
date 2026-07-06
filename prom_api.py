from __future__ import annotations

import aiohttp
from typing import Any


class PromAPIError(Exception):
    pass


class PromClient:
    def __init__(self, token: str, base_url: str = "https://my.prom.ua/api/v1"):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.session: aiohttp.ClientSession | None = None

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        )
        return self

    async def __aexit__(self, exc_type, exc, tb):
        if self.session:
            await self.session.close()

    async def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> Any:
        if not self.session:
            raise RuntimeError("PromClient must be used as async context manager")

        url = f"{self.base_url}/{path.lstrip('/')}"
        async with self.session.request(method, url, params=params, json=json) as resp:
            text = await resp.text()
            if resp.status < 200 or resp.status >= 300:
                raise PromAPIError(f"Prom API {resp.status} {method} {path}: {text[:1000]}")
            if not text:
                return {}
            try:
                return await resp.json(content_type=None)
            except Exception:
                return {"raw": text}

    # Orders
    async def get_orders(self, *, status: str | None = None) -> list[dict]:
        params = {}
        if status:
            params["status"] = status
        data = await self.request("GET", "/orders/list", params=params or None)
        return _list_from_response(data, "orders")

    async def get_order(self, order_id: str | int) -> dict:
        data = await self.request("GET", f"/orders/{order_id}")
        return data.get("order", data)

    async def get_product(self, product_id: str | int) -> dict:
        """Best-effort product lookup used to enrich Prom Chat context messages."""
        last_error: Exception | None = None
        for path in (f"/products/{product_id}", f"/products/{product_id}/"):
            try:
                data = await self.request("GET", path)
                if isinstance(data, dict):
                    return data.get("product", data)
                return {"id": product_id, "raw": data}
            except Exception as e:
                last_error = e
                continue
        raise last_error or PromAPIError(f"Product {product_id} not found")

    async def set_order_status(
        self,
        order_id: str | int,
        status: str,
        *,
        cancellation_reason: str | None = None,
        cancellation_text: str | None = None,
    ) -> dict:
        body: dict[str, Any] = {"ids": [int(order_id) if str(order_id).isdigit() else order_id], "status": status}
        if cancellation_reason:
            body["cancellation_reason"] = cancellation_reason
        if cancellation_text:
            body["cancellation_text"] = cancellation_text
        return await self.request("POST", "/orders/set_status", json=body)

    # Messages + Chat
    async def get_messages(self, *, limit: int | None = None, status: str | None = None) -> list[dict]:
        """Return both old Prom messages and new Prom Chat messages.

        Prom has two different APIs:
        - /messages/list = old seller messages/questions
        - /chat/rooms + /chat/messages_history = actual Prom chat rooms
        The first one can legally return 0 even when the seller has chat messages.
        """
        combined: list[dict] = []

        # 1) Old messages API
        try:
            params = {}
            if limit:
                params["limit"] = limit
            if status:
                params["status"] = status
            data = await self.request("GET", "/messages/list", params=params or None)
            combined.extend(_extract_messages(data))
        except Exception:
            # Do not fail chat polling if the old Messages endpoint is unavailable.
            pass

        # 2) New chat API
        try:
            combined.extend(await self.get_chat_messages(limit=limit or 50))
        except Exception:
            # Keep old behavior if chat API is unavailable. /debug_chat_endpoints will show details.
            pass

        # De-duplicate by channel+room+message id or raw hash fallback
        seen: set[str] = set()
        unique: list[dict] = []
        for msg in combined:
            key = _message_identity(msg)
            if key in seen:
                continue
            seen.add(key)
            unique.append(msg)
        return unique

    async def raw_messages_response(self) -> Any:
        legacy = None
        rooms = None
        chat_messages = None
        try:
            legacy = await self.request("GET", "/messages/list")
        except Exception as e:
            legacy = {"error": str(e)}
        try:
            rooms = await self.get_chat_rooms()
        except Exception as e:
            rooms = {"error": str(e)}
        try:
            chat_messages = await self.get_chat_messages(limit=20)
        except Exception as e:
            chat_messages = {"error": str(e)}
        return {"legacy_messages_list": legacy, "chat_rooms": rooms, "chat_messages": chat_messages}

    async def get_chat_rooms(self) -> list[dict]:
        data = await self.request("GET", "/chat/rooms")
        return _extract_rooms(data)

    async def get_chat_messages(self, *, limit: int = 50) -> list[dict]:
        rooms = await self.get_chat_rooms()

        # Best-effort enrichment: Prom Chat rooms often contain only room id + buyer_client_id.
        # Pull recent orders once and match by client_id/order_id so Telegram can show client, phone,
        # product, SKU and Ukrainian product title instead of blanks.
        recent_orders: list[dict] = []
        try:
            recent_orders = await self.get_orders()
        except Exception:
            recent_orders = []

        product_cache: dict[str, dict | None] = {}

        async def product_from_context(ctx_msg: dict) -> dict:
            """Turn Prom context rows into a product object.

            In your raw JSON Prom sends a separate chat row with type=context,
            context_item_type=product and context_item_id=<product_id>. The next
            message in that room is the actual buyer text. Older bot versions ignored
            that context row, so product/title were blank.
            """
            product_id = _as_text_id(
                ctx_msg.get("context_item_id")
                or ctx_msg.get("product_id")
                or ctx_msg.get("item_id")
                or ctx_msg.get("good_id")
            )
            product: dict[str, Any] = {}
            if product_id:
                if product_id not in product_cache:
                    try:
                        product_cache[product_id] = await self.get_product(product_id)
                    except Exception:
                        product_cache[product_id] = None
                cached = product_cache.get(product_id)
                if isinstance(cached, dict):
                    product.update(cached)
            if product_id and not product.get("id"):
                product["id"] = product_id
            image = ctx_msg.get("context_item_image_url") or ctx_msg.get("image") or ctx_msg.get("image_url")
            if image and not product.get("image"):
                product["image"] = image
            return product

        all_messages: list[dict] = []
        # Newest active rooms first is usually enough. Keep it sane to avoid API spam.
        for room in rooms[:80]:
            room_id = _room_id(room)
            if room_id is None:
                continue
            histories = await self._get_chat_history_variants(room, limit=limit)

            # If Prom gives the room and last_message_id but does not give the message body via history,
            # still create a notification row. This lets the bot detect new chat activity by last_message_id.
            if not histories and isinstance(room, dict) and room.get("last_message_id") not in (None, ""):
                histories = [{
                    "id": room.get("last_message_id"),
                    "message_id": room.get("last_message_id"),
                    "text": "⚠️ Prom показав активність у чаті, але API не віддав текст через /chat/messages_history. Відкрий чат у Prom або скинь debug-файл.",
                    "date_created": room.get("date_sent"),
                    "status": room.get("status"),
                    "__room_activity_fallback": True,
                }]

            # Keep chronological order inside a room so a product context row can enrich
            # the following buyer message.
            try:
                histories = sorted(
                    histories,
                    key=lambda m: (
                        str(m.get("date_sent") or m.get("date_created") or m.get("created_at") or "") if isinstance(m, dict) else "",
                        str(m.get("id") or m.get("message_id") or "") if isinstance(m, dict) else "",
                    ),
                )
            except Exception:
                pass

            last_context_product: dict[str, Any] | None = None
            for msg in histories:
                if not isinstance(msg, dict):
                    continue

                msg_type = str(msg.get("type") or msg.get("message_type") or "").strip().lower()
                body = msg.get("body") or msg.get("text") or msg.get("message") or msg.get("content") or ""
                is_product_context = (
                    msg_type == "context"
                    and str(msg.get("context_item_type") or msg.get("item_type") or "").strip().lower() in {"product", "good", "item"}
                )
                if is_product_context:
                    product = await product_from_context(msg)
                    if product:
                        last_context_product = {
                            "product": product,
                            "product_id": product.get("id") or msg.get("context_item_id"),
                            "context_item_id": msg.get("context_item_id"),
                            "context_item_image_url": msg.get("context_item_image_url"),
                        }
                    # Context-only rows are not real client messages, do not send them to Telegram.
                    if body in (None, ""):
                        continue

                merged = dict(msg)
                merged["__prom_channel"] = "chat"
                merged["__chat_room_id"] = room_id
                merged["__chat_room"] = room

                # Apply last product context to the next message in this room.
                if last_context_product:
                    for key, value in last_context_product.items():
                        if value not in (None, "", [], {}) and merged.get(key) in (None, "", [], {}):
                            merged[key] = value
                    product = last_context_product.get("product")
                    if isinstance(product, dict):
                        merged.setdefault("product", product)
                        merged.setdefault("product_id", product.get("id"))
                        merged.setdefault("product_url", product.get("url") or product.get("product_url") or product.get("link"))
                        merged.setdefault("sku", product.get("sku") or product.get("article") or product.get("vendor_code"))
                        name_ml = product.get("name_multilang") if isinstance(product.get("name_multilang"), dict) else {}
                        merged.setdefault("product_name", name_ml.get("uk") or name_ml.get("ua") or product.get("name"))

                # Copy useful fields from room to message if Prom keeps them at room level.
                if isinstance(room, dict):
                    for key in (
                        "client", "customer", "buyer", "user", "sender", "author", "from_user",
                        "product", "item", "good", "product_id", "product_name", "product_url", "sku",
                        "order_id", "phone", "email", "client_name", "customer_name", "buyer_name",
                        "sender_name", "author_name", "title", "name",
                        "buyer_client_id", "ident", "last_message_id", "date_sent",
                        "direction", "message_direction", "sender_type", "last_message_direction", "last_message_sender_type"
                    ):
                        if key in room and merged.get(key) in (None, "", [], {}):
                            merged[key] = room.get(key)
                merged = _enrich_chat_message_from_orders(merged, recent_orders)
                all_messages.append(merged)
        return all_messages


    async def _get_chat_history_variants(self, room: Any, *, limit: int = 50) -> list[dict]:
        """Try many real-world Prom chat history parameter shapes.

        Important fix: older code returned immediately when the first parameter variant
        returned HTTP 200 but an empty list. On some Prom accounts wrong params return
        empty lists instead of errors, so we must try all variants and return the first
        non-empty response.
        """
        if isinstance(room, dict):
            room_id = _room_id(room)
            ident = room.get("ident")
            last_message_id = room.get("last_message_id")
        else:
            room_id = room
            ident = None
            last_message_id = None

        param_variants: list[dict[str, Any]] = []
        base_ids = []
        if room_id not in (None, ""):
            base_ids.extend([
                ("room_id", room_id),
                ("id", room_id),
                ("chat_id", room_id),
                ("room", room_id),
                ("uuid", room_id),
            ])
        if ident not in (None, ""):
            base_ids.extend([
                ("ident", ident),
                ("room_ident", ident),
                ("room_id", ident),
                ("id", ident),
            ])

        for key, value in base_ids:
            param_variants.append({key: value, "limit": limit})

        # Some history endpoints require a cursor based on the latest message id from /chat/rooms.
        if last_message_id not in (None, ""):
            cursor_keys = [
                "last_message_id", "message_id", "from_message_id", "start_message_id",
                "offset_message_id", "offset_id", "before_id", "after_id", "max_id", "min_id", "last_id",
            ]
            for key, value in base_ids:
                for cursor_key in cursor_keys:
                    param_variants.append({key: value, cursor_key: last_message_id, "limit": limit})

        first_empty: list[dict] | None = None
        last_error: Exception | None = None
        for params in param_variants:
            try:
                data = await self.request("GET", "/chat/messages_history", params=params)
                messages = _extract_messages(data)
                if messages:
                    return messages
                if first_empty is None:
                    first_empty = messages
            except Exception as e:
                last_error = e
                continue

        if first_empty is not None:
            return first_empty
        if last_error:
            raise last_error
        return []

    async def probe_chat_endpoints(self) -> dict[str, Any]:
        """Try official and possible chat/message endpoints and return compact results for debugging."""
        result: dict[str, Any] = {}

        simple_paths = [
            "/messages/list",
            "/chat/rooms",
            "/chats/list",
            "/chats",
            "/chat/list",
            "/conversations/list",
            "/dialogs/list",
        ]
        for path in simple_paths:
            try:
                data = await self.request("GET", path)
                result[path] = {
                    "ok": True,
                    "type": type(data).__name__,
                    "message_count": len(_extract_messages(data)),
                    "room_count": len(_extract_rooms(data)),
                    "sample": data[:2] if isinstance(data, list) else data,
                }
            except Exception as e:
                result[path] = {"ok": False, "error": str(e)[:1000]}

        # If /chat/rooms works, probe history for the newest rooms with several params.
        try:
            rooms = await self.get_chat_rooms()
            result["__rooms_found"] = len(rooms)
            for room in rooms[:5]:
                rid = _room_id(room)
                if rid is None:
                    continue
                last_id = room.get("last_message_id") if isinstance(room, dict) else None
                ident = room.get("ident") if isinstance(room, dict) else None
                result[f"__room_sample:{rid}"] = {
                    "id": rid,
                    "ident": ident,
                    "last_message_id": last_id,
                    "date_sent": room.get("date_sent") if isinstance(room, dict) else None,
                    "buyer_client_id": room.get("buyer_client_id") if isinstance(room, dict) else None,
                }
                variants = [
                    ("room_id", {"room_id": rid, "limit": 20}),
                    ("id", {"id": rid, "limit": 20}),
                    ("chat_id", {"chat_id": rid, "limit": 20}),
                    ("room", {"room": rid, "limit": 20}),
                ]
                if ident not in (None, ""):
                    variants.extend([
                        ("ident", {"ident": ident, "limit": 20}),
                        ("room_ident", {"room_ident": ident, "limit": 20}),
                    ])
                if last_id not in (None, ""):
                    variants.extend([
                        ("room_id+last_message_id", {"room_id": rid, "last_message_id": last_id, "limit": 20}),
                        ("room_id+message_id", {"room_id": rid, "message_id": last_id, "limit": 20}),
                        ("room_id+from_message_id", {"room_id": rid, "from_message_id": last_id, "limit": 20}),
                        ("room_id+before_id", {"room_id": rid, "before_id": last_id, "limit": 20}),
                        ("room_id+after_id", {"room_id": rid, "after_id": last_id, "limit": 20}),
                    ])
                for label, params in variants:
                    key = f"/chat/messages_history {label} {rid}"
                    try:
                        data = await self.request("GET", "/chat/messages_history", params=params)
                        result[key] = {
                            "ok": True,
                            "message_count": len(_extract_messages(data)),
                            "sample": data,
                        }
                    except Exception as e:
                        result[key] = {"ok": False, "error": str(e)[:1000], "room_sample": room}
        except Exception as e:
            result["__rooms_probe_error"] = str(e)[:1000]

        return result

    async def get_message(self, prom_message_id: str | int) -> dict:
        # Chat messages are already fetched from /chat/messages_history. They cannot be fetched by /messages/{id}.
        if str(prom_message_id).startswith("chat:"):
            return {"id": str(prom_message_id), "__prom_channel": "chat"}
        data = await self.request("GET", f"/messages/{prom_message_id}")
        return data.get("message", data)

    async def set_message_status(self, prom_message_id: str | int, status: str = "read") -> dict:
        if str(prom_message_id).startswith("chat:"):
            room_id = str(prom_message_id).split(":", 2)[1]
            variants = [
                {"room_id": room_id},
                {"id": room_id},
                {"chat_id": room_id},
            ]
            last_error: Exception | None = None
            for body in variants:
                try:
                    return await self.request("POST", "/chat/mark_message_read", json=body)
                except PromAPIError as e:
                    last_error = e
            raise last_error or PromAPIError("Prom chat mark read failed")

        body = {"ids": [int(prom_message_id) if str(prom_message_id).isdigit() else prom_message_id], "status": status}
        return await self.request("POST", "/messages/set_status", json=body)

    async def reply_message(self, prom_message_id: str | int, text: str) -> dict:
        """Reply to either old /messages or new /chat room message.

        IMPORTANT: Prom can answer with HTTP 200 and still return a JSON body like
        {"status":"error", ...}. Older bot versions treated any HTTP 200 as success,
        so Telegram could show "sent" while Prom silently rejected the payload.
        This version checks the body and tries several officially/commonly used payload shapes.
        """
        if str(prom_message_id).startswith("chat:"):
            parts = str(prom_message_id).split(":", 2)
            room_id = parts[1] if len(parts) > 1 else ""
            room_ident = ""
            try:
                for room in await self.get_chat_rooms():
                    if str(_room_id(room)) == str(room_id):
                        room_ident = _as_text_id(room.get("ident") if isinstance(room, dict) else "")
                        break
            except Exception:
                room_ident = ""

            targets: list[tuple[str, str]] = []
            for key, value in (
                ("room_id", room_id),
                ("id", room_id),
                ("chat_id", room_id),
                ("room", room_id),
                ("dialog_id", room_id),
                ("conversation_id", room_id),
                ("ident", room_ident),
                ("room_ident", room_ident),
            ):
                if value:
                    targets.append((key, value))

            variants: list[dict[str, Any]] = []
            for key, value in targets:
                # JSON-body variants
                for text_key in ("text", "message", "body", "content"):
                    variants.append({"params": None, "json": {key: value, text_key: text}, "label": f"json:{key}+{text_key}"})
                # Nested-message variants, just in case Prom expects an object.
                variants.extend([
                    {"params": None, "json": {key: value, "message": {"text": text}}, "label": f"json:{key}+message.text"},
                    {"params": None, "json": {key: value, "message": {"body": text}}, "label": f"json:{key}+message.body"},
                    {"params": None, "json": {key: value, "type": "text", "text": text}, "label": f"json:{key}+type+text"},
                ])
                # Query-param variants
                for text_key in ("text", "message", "body", "content"):
                    variants.append({"params": {key: value}, "json": {text_key: text}, "label": f"params:{key}+json:{text_key}"})

            errors: list[str] = []
            for variant in variants:
                try:
                    data = await self.request(
                        "POST",
                        "/chat/send_message",
                        params=variant.get("params"),
                        json=variant.get("json"),
                    )
                    err = _prom_body_error(data)
                    if err:
                        errors.append(f"{variant['label']}: {err}; response={_short(data)}")
                        continue
                    if isinstance(data, dict):
                        data = dict(data)
                        data["__payload_used"] = variant.get("label")
                    return data
                except PromAPIError as e:
                    errors.append(f"{variant['label']}: {str(e)[:600]}")
                    continue
            raise PromAPIError("Prom chat send_message failed. Tried variants:\n" + "\n".join(errors[:20]))

        mid = int(prom_message_id) if str(prom_message_id).isdigit() else prom_message_id
        variants = [
            {"id": mid, "message": text},
            {"id": mid, "text": text},
            {"message_id": mid, "message": text},
            {"message_id": mid, "text": text},
        ]
        errors: list[str] = []
        for body in variants:
            try:
                data = await self.request("POST", "/messages/reply", json=body)
                err = _prom_body_error(data)
                if err:
                    errors.append(f"body={body}: {err}; response={_short(data)}")
                    continue
                return data
            except PromAPIError as e:
                errors.append(str(e)[:600])
        raise PromAPIError("Prom message reply failed. Tried variants:\n" + "\n".join(errors[:20]))

def _short(value: Any, limit: int = 700) -> str:
    try:
        import json as _json
        text = _json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text[:limit]


def _prom_body_error(data: Any) -> str | None:
    """Return error text if Prom returned an error inside a 2xx JSON body."""
    if not isinstance(data, dict):
        return None
    status = str(data.get("status") or data.get("result") or "").lower().strip()
    code = str(data.get("code") or data.get("error_code") or "").lower().strip()
    title = str(data.get("title") or "").lower().strip()
    if status in {"error", "fail", "failed", "false"}:
        return str(data.get("message") or data.get("error") or data.get("errors") or "status=error")
    if data.get("error") or data.get("errors"):
        return str(data.get("error") or data.get("errors"))
    if code.startswith(("400", "401", "403", "404", "422", "500")):
        return str(data.get("message") or data.get("title") or data.get("code"))
    if title in {"bad request", "forbidden", "not found", "unauthorized", "unprocessable entity"}:
        return str(data.get("message") or data.get("title"))
    # Some Prom errors are nested inside data.
    nested = data.get("data")
    if isinstance(nested, dict):
        nstatus = str(nested.get("status") or nested.get("result") or "").lower().strip()
        if nstatus in {"error", "fail", "failed", "false"}:
            return str(nested.get("message") or nested.get("error") or nested.get("errors") or "nested status=error")
        if nested.get("error") or nested.get("errors"):
            return str(nested.get("error") or nested.get("errors"))
    return None


def _as_text_id(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value).strip()


def _order_client_id(order: dict) -> str:
    if not isinstance(order, dict):
        return ""
    direct = _as_text_id(order.get("client_id"))
    if direct:
        return direct
    client = order.get("client")
    if isinstance(client, dict):
        return _as_text_id(client.get("id") or client.get("client_id"))
    return ""


def _order_id(order: dict) -> str:
    if not isinstance(order, dict):
        return ""
    return _as_text_id(order.get("id") or order.get("order_id"))


def _find_related_order(message: dict, orders: list[dict]) -> dict | None:
    if not isinstance(message, dict):
        return None
    order_id = _as_text_id(message.get("order_id") or message.get("order") or "")
    if order_id:
        for order in orders:
            if _order_id(order) == order_id:
                return order

    room = message.get("__chat_room") if isinstance(message.get("__chat_room"), dict) else {}
    buyer_client_id = _as_text_id(
        message.get("buyer_client_id")
        or message.get("client_id")
        or (room.get("buyer_client_id") if isinstance(room, dict) else "")
    )
    # buyer_client_id can be 0 when Prom does not expose the buyer account/client yet.
    if buyer_client_id and buyer_client_id != "0":
        for order in orders:
            if _order_client_id(order) == buyer_client_id:
                return order
    return None


def _enrich_chat_message_from_orders(message: dict, orders: list[dict]) -> dict:
    """Add client/order/product info to a chat message using recent Prom orders.

    Prom /chat/rooms can return only room metadata. If buyer_client_id matches a recent
    order's client_id, this makes Telegram notifications useful: client name, phone,
    order id, first product, SKU, url, and all order products are attached.
    """
    if not isinstance(message, dict):
        return message
    order = _find_related_order(message, orders)
    if not order:
        return message

    enriched = dict(message)
    enriched["__related_order"] = order
    enriched["__related_order_id"] = _order_id(order)
    products = order.get("products") or order.get("items") or []
    if isinstance(products, dict):
        products = list(products.values())
    if isinstance(products, list):
        enriched["__related_order_products"] = products

    if not enriched.get("order_id"):
        enriched["order_id"] = _order_id(order)

    client = order.get("client") if isinstance(order.get("client"), dict) else {}
    if client and not enriched.get("client"):
        enriched["client"] = client
    if not enriched.get("client_name"):
        first = client.get("first_name") or order.get("client_first_name") or ""
        last = client.get("last_name") or order.get("client_last_name") or ""
        full = " ".join(str(x).strip() for x in (first, last) if x not in (None, "")).strip()
        if full:
            enriched["client_name"] = full
    if not enriched.get("phone"):
        enriched["phone"] = order.get("phone") or client.get("phone")
    if not enriched.get("email"):
        enriched["email"] = order.get("email")

    # If the chat API did not tell the product, use the first product from the matched order.
    if isinstance(products, list) and products:
        first_product = next((p for p in products if isinstance(p, dict)), None)
        if first_product and not isinstance(enriched.get("product"), dict):
            enriched["product"] = first_product
        if first_product:
            enriched.setdefault("product_id", first_product.get("id") or first_product.get("product_id"))
            enriched.setdefault("sku", first_product.get("sku") or first_product.get("article") or first_product.get("vendor_code"))
            enriched.setdefault("product_url", first_product.get("url") or first_product.get("product_url") or first_product.get("link"))
            if not enriched.get("product_name"):
                name_ml = first_product.get("name_multilang") if isinstance(first_product.get("name_multilang"), dict) else {}
                enriched["product_name"] = name_ml.get("uk") or name_ml.get("ua") or first_product.get("name")
    return enriched


def _list_from_response(data: Any, key: str) -> list[dict]:
    if isinstance(data, dict):
        value = data.get(key)
        if isinstance(value, list):
            return value
        # Some APIs wrap lists inside data/items/results
        for alt in ("data", "items", "results"):
            value = data.get(alt)
            if isinstance(value, list):
                return value
    if isinstance(data, list):
        return data
    return []



def _room_id(room: dict) -> Any:
    if not isinstance(room, dict):
        return None
    for key in ("id", "room_id", "chat_id", "dialog_id", "conversation_id", "uuid"):
        if key in room and room.get(key) not in (None, ""):
            return room.get(key)
    return None


def _extract_rooms(data: Any) -> list[dict]:
    """Return chat rooms from Prom response shapes.

    Real Prom response can be nested like:
    {"status":"ok", "data": {"rooms": [...]}}
    Older code looked only for top-level rooms and therefore returned 0.
    """
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    if not isinstance(data, dict):
        return []

    for key in ("rooms", "chats", "dialogs", "conversations", "items", "results"):
        value = data.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

    nested = data.get("data")
    if isinstance(nested, dict):
        rooms = _extract_rooms(nested)
        if rooms:
            return rooms
    elif isinstance(nested, list):
        return [x for x in nested if isinstance(x, dict)]

    # Last-resort shallow recursive scan for unusual wrappers.
    for value in data.values():
        if isinstance(value, dict):
            rooms = _extract_rooms(value)
            if rooms:
                return rooms
    return []


def _message_identity(msg: dict) -> str:
    if not isinstance(msg, dict):
        return str(msg)
    channel = str(msg.get("__prom_channel") or "messages")
    room_id = str(msg.get("__chat_room_id") or msg.get("room_id") or msg.get("chat_id") or "")
    for key in ("id", "message_id", "chat_message_id", "msg_id", "uuid"):
        if msg.get(key) not in (None, ""):
            return f"{channel}:{room_id}:{msg.get(key)}"
    import json, hashlib
    raw = json.dumps(msg, ensure_ascii=False, sort_keys=True, default=str)
    return f"{channel}:{room_id}:hash_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:24]

def _extract_messages(data: Any) -> list[dict]:
    """Return messages from common Prom response shapes.

    Handles top-level and nested wrappers, including:
    {"messages": [...]}, {"data": {"messages": [...]}} and
    chat/thread rows with nested message lists.
    """
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]

    if not isinstance(data, dict):
        return []

    for direct_key in ("messages", "message_list"):
        value = data.get(direct_key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]

    # Prom often wraps the useful payload inside data: {...}
    nested_data = data.get("data")
    if isinstance(nested_data, dict):
        nested_messages = _extract_messages(nested_data)
        if nested_messages:
            return nested_messages

    for key in ("chats", "dialogs", "conversations", "threads", "items", "results"):
        value = data.get(key)
        if isinstance(value, list):
            flat: list[dict] = []
            for obj in value:
                if not isinstance(obj, dict):
                    continue
                nested = obj.get("messages") or obj.get("items") or obj.get("last_messages") or obj.get("history")
                if isinstance(nested, list):
                    for msg in nested:
                        if isinstance(msg, dict):
                            merged = dict(msg)
                            for mk in ("chat_id", "room_id", "dialog_id", "conversation_id", "thread_id", "client", "customer", "buyer", "user", "sender", "author", "product", "item", "good", "product_id", "product_name", "product_url", "sku", "order_id", "buyer_client_id"):
                                if mk in obj and mk not in merged:
                                    merged[mk] = obj.get(mk)
                            flat.append(merged)
                else:
                    # Some APIs return one chat/thread row as latest-message row.
                    # Do not treat pure room lists as messages.
                    if any(k in obj for k in ("text", "message", "body", "content", "last_message", "last_message_id")):
                        flat.append(obj)
            if flat:
                return flat

    # Last-resort recursive scan, but avoid treating rooms list as messages.
    for value in data.values():
        if isinstance(value, dict):
            nested_messages = _extract_messages(value)
            if nested_messages:
                return nested_messages

    return []
