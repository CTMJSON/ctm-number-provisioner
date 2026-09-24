"""Minimal async HTTP client for the CallTrackingMetrics API v1."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from .auth import Token, load_token

DEFAULT_BASE_URL = "https://api.calltrackingmetrics.com/api/v1"
MAX_CONCURRENCY = 4
RETRY_STATUSES = {429, 500, 502, 503, 504}
BACKOFF_SECONDS = (1.0, 2.0, 4.0)

# CTM allows roughly 10 req/s; every fan-out in this server shares this.
SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENCY)


class CTMError(RuntimeError):
    """Raised for any non-2xx CTM response or transport failure."""

    def __init__(self, status: int, body: Any, method: str, path: str) -> None:
        self.status = status
        self.body = body
        self.method = method
        self.path = path
        super().__init__(f"{method} {path} -> {status}: {_short(body)}")


def _short(body: Any, limit: int = 300) -> str:
    text = body if isinstance(body, str) else repr(body)
    return text if len(text) <= limit else text[: limit - 3] + "..."


class CTMClient:
    """Thin wrapper: auth headers, retries, 429 backoff, and page fan-out."""

    def __init__(
        self,
        token: Token | None = None,
        base_url: str | None = None,
        account_id: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.token = token or load_token()
        self.base_url = (base_url or _env_base_url()).rstrip("/")
        self.default_account_id = account_id or _env_account_id()
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"Basic {self.token.value}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
        )

    async def __aenter__(self) -> CTMClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def resolve_account_id(self, account_id: str | None = None) -> str:
        resolved = account_id or self.default_account_id
        if not resolved:
            raise CTMError(
                0, "No CTM account id configured (set CTM_ACCOUNT_ID).", "CONFIG", "account_id"
            )
        return str(resolved)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
    ) -> Any:
        clean = {k: v for k, v in (params or {}).items() if v is not None}
        last_error: Exception | None = None
        for attempt in range(len(BACKOFF_SECONDS) + 1):
            try:
                response = await self._client.request(method, path, params=clean or None, json=json)
            except httpx.TransportError as exc:
                last_error = exc
                if attempt < len(BACKOFF_SECONDS):
                    await asyncio.sleep(BACKOFF_SECONDS[attempt])
                    continue
                raise CTMError(0, str(exc), method, path) from exc

            if response.status_code in RETRY_STATUSES and attempt < len(BACKOFF_SECONDS):
                await asyncio.sleep(BACKOFF_SECONDS[attempt])
                continue
            if response.status_code >= 400:
                raise CTMError(response.status_code, _parse(response), method, path)
            return _parse(response)
        raise CTMError(0, str(last_error), method, path)

    async def get(self, path: str, **params: Any) -> Any:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json: Any | None = None, **params: Any) -> Any:
        return await self.request("POST", path, json=json, params=params)

    async def put(self, path: str, json: Any | None = None, **params: Any) -> Any:
        return await self.request("PUT", path, json=json, params=params)

    async def delete(self, path: str, **params: Any) -> Any:
        return await self.request("DELETE", path, params=params)

    async def fetch_all(self, path: str, key: str, per_page: int = 100) -> list[Any]:
        """Fetch page 1, then all remaining pages concurrently (never sequentially)."""
        first = await self.get(path, page=1, per_page=per_page)
        items = list((first or {}).get(key) or [])
        total_pages = int((first or {}).get("total_pages") or 1)
        if total_pages <= 1:
            return items

        async def one(page: int) -> list[Any]:
            async with SEMAPHORE:
                data = await self.get(path, page=page, per_page=per_page)
            return list((data or {}).get(key) or [])

        for chunk in await asyncio.gather(*(one(p) for p in range(2, total_pages + 1))):
            items.extend(chunk)
        return items


def _parse(response: httpx.Response) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        return response.text


def _env_base_url() -> str:
    import os

    return os.environ.get("CTM_BASE_URL") or DEFAULT_BASE_URL


def _env_account_id() -> str | None:
    import os

    return os.environ.get("CTM_ACCOUNT_ID")