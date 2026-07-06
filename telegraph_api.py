from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import aiohttp


class TelegraphError(Exception):
    pass


class TelegraphClient:
    """Small async Telegra.ph client with local access-token storage."""

    def __init__(self, token: str = "", token_file: str = "telegraph_token.txt", author_name: str = "Prom Telegram Bot"):
        self.token = (token or "").strip()
        self.token_file = Path(token_file)
        self.author_name = author_name[:128] or "Prom Telegram Bot"

    async def _post(self, method: str, data: dict[str, Any]) -> dict[str, Any]:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30)) as session:
            async with session.post(f"https://api.telegra.ph/{method}", data=data) as resp:
                raw = await resp.text()
                try:
                    payload = json.loads(raw)
                except Exception as exc:
                    raise TelegraphError(f"Telegraph API returned non-JSON: {raw[:500]}") from exc
                if not payload.get("ok"):
                    raise TelegraphError(str(payload.get("error") or raw[:500]))
                return payload.get("result") or {}

    async def get_token(self) -> str:
        if self.token:
            return self.token
        if self.token_file.exists():
            token = self.token_file.read_text(encoding="utf-8").strip()
            if token:
                self.token = token
                return token
        result = await self._post(
            "createAccount",
            {
                "short_name": "prom_bot",
                "author_name": self.author_name,
            },
        )
        token = str(result.get("access_token") or "").strip()
        if not token:
            raise TelegraphError("Telegraph did not return access_token")
        self.token = token
        self.token_file.write_text(token, encoding="utf-8")
        return token

    async def create_page(self, title: str, content_nodes: list[dict[str, Any]], *, author_name: str | None = None) -> str:
        token = await self.get_token()
        data = {
            "access_token": token,
            "title": title[:256] or "Prom замовлення",
            "author_name": (author_name or self.author_name)[:128],
            "content": json.dumps(content_nodes, ensure_ascii=False),
            "return_content": "false",
        }
        try:
            result = await self._post("createPage", data)
        except TelegraphError as e:
            # If user copied an invalid token in .env, create a new local one and retry once.
            if "ACCESS_TOKEN_INVALID" in str(e).upper():
                self.token = ""
                if self.token_file.exists():
                    self.token_file.unlink()
                data["access_token"] = await self.get_token()
                result = await self._post("createPage", data)
            else:
                raise
        url = str(result.get("url") or "").strip()
        if not url:
            raise TelegraphError("Telegraph did not return page url")
        return url
