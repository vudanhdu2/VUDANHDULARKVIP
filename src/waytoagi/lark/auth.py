"""Async LarkAuth — quản lý tenant_access_token + low-level HTTP với retry/rate-limit.

Hỗ trợ cả Feishu CN (https://open.feishu.cn) và Larksuite (https://open.larksuite.com)
qua tham số `open_url`. Một process có thể giữ 2 instance — 1 cho SRC, 1 cho DST.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any, Literal, overload

import httpx
import structlog
from aiolimiter import AsyncLimiter
from tenacity import (
    AsyncRetrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = structlog.get_logger(__name__)


class LarkAPIError(Exception):
    """Lark API trả non-zero code."""

    def __init__(self, code: int, msg: str, path: str = "") -> None:
        super().__init__(f"[{code}] {msg} ({path})")
        self.code = code
        self.msg = msg
        self.path = path


_RETRYABLE_HTTP_STATUS = frozenset({429, 500, 502, 503, 504})
_RETRYABLE_LARK_CODES = frozenset({99991400, 230001})  # rate-limit, frequency control


class LarkAuth:
    """Async client cho Lark Open API.

    Thread-safe trong cùng event loop, KHÔNG share giữa loops khác nhau.
    """

    def __init__(
        self,
        app_id: str,
        app_secret: str,
        open_url: str = "https://open.larksuite.com/open-apis",
        rate_limit_rps: int = 5,
        timeout: float = 30.0,
    ) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        self.open_url = open_url.rstrip("/")
        self._token: str | None = None
        self._token_expire_at = 0.0
        self._token_lock = asyncio.Lock()
        self._limiter = AsyncLimiter(rate_limit_rps, 1.0)
        self._client = httpx.AsyncClient(timeout=timeout)
        self._log = logger.bind(open_url=self.open_url, app_id=self.app_id)

    async def __aenter__(self) -> LarkAuth:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ============================================================
    # Token management
    # ============================================================

    async def get_tenant_token(self) -> str:
        """Trả về tenant_access_token hợp lệ, refresh khi sắp hết hạn."""
        if self._token and time.time() < self._token_expire_at:
            return self._token
        async with self._token_lock:
            if self._token and time.time() < self._token_expire_at:
                return self._token
            await self._refresh_token()
            assert self._token is not None
            return self._token

    async def _refresh_token(self) -> None:
        url = f"{self.open_url}/auth/v3/tenant_access_token/internal"
        async with self._limiter:
            r = await self._client.post(url, json={
                "app_id": self.app_id,
                "app_secret": self.app_secret,
            })
        data = r.json()
        if data.get("code") != 0:
            raise LarkAPIError(data.get("code", -1), data.get("msg", ""), url)
        self._token = data["tenant_access_token"]
        self._token_expire_at = time.time() + int(data.get("expire", 7200)) - 60
        self._log.info("tenant_token_refreshed", expire_in_sec=int(data.get("expire", 7200)))

    async def auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {await self.get_tenant_token()}",
            "Content-Type": "application/json; charset=utf-8",
        }

    # ============================================================
    # Low-level request with retry
    # ============================================================

    @overload
    async def request(
        self, method: str, path: str,
        *,
        params: Mapping[str, Any] | None = ...,
        json_body: Any | None = ...,
        raw: Literal[False] = False,
    ) -> dict[str, Any]: ...

    @overload
    async def request(
        self, method: str, path: str,
        *,
        params: Mapping[str, Any] | None = ...,
        json_body: Any | None = ...,
        raw: Literal[True],
    ) -> httpx.Response: ...

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        raw: bool = False,
    ) -> dict[str, Any] | httpx.Response:
        """Generic request với retry + rate limit.

        - `raw=True` → trả raw httpx.Response (cho download).
        - Else → parse json, raise LarkAPIError nếu code != 0.
        """
        url = f"{self.open_url}{path}"

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(4),
            wait=wait_exponential(multiplier=1, min=1, max=10),
            retry=retry_if_exception_type((httpx.HTTPError, LarkAPIError)),
            reraise=True,
        ):
            with attempt:
                async with self._limiter:
                    headers = await self.auth_headers()
                    if raw:
                        # binary download — bỏ Content-Type json
                        headers.pop("Content-Type", None)
                    r = await self._client.request(
                        method, url,
                        headers=headers,
                        params=params,
                        json=json_body,
                    )

                if raw:
                    if r.status_code in _RETRYABLE_HTTP_STATUS:
                        raise LarkAPIError(r.status_code, "retryable http", path)
                    return r

                if r.status_code in _RETRYABLE_HTTP_STATUS:
                    raise LarkAPIError(r.status_code, "retryable http", path)

                data: dict[str, Any] = r.json()
                code = data.get("code", -1)
                if code in _RETRYABLE_LARK_CODES:
                    raise LarkAPIError(code, data.get("msg", ""), path)
                if code != 0:
                    # non-retryable — raise once, no retry
                    self._log.warning("lark_api_nonzero", path=path, code=code, msg=data.get("msg"))
                    raise LarkAPIError(code, data.get("msg", ""), path)
                return data

        raise RuntimeError("unreachable")  # pragma: no cover

    # ============================================================
    # Convenience verbs
    # ============================================================

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return await self.request("GET", path, params=params)

    async def post(self, path: str, json_body: Any | None = None) -> dict[str, Any]:
        return await self.request("POST", path, json_body=json_body)

    async def put(self, path: str, json_body: Any | None = None) -> dict[str, Any]:
        return await self.request("PUT", path, json_body=json_body)

    async def patch(self, path: str, json_body: Any | None = None) -> dict[str, Any]:
        return await self.request("PATCH", path, json_body=json_body)

    async def delete(self, path: str) -> dict[str, Any]:
        return await self.request("DELETE", path)

    async def get_raw(
        self, path: str, params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        return await self.request("GET", path, params=params, raw=True)

    async def post_form(
        self,
        path: str,
        *,
        data: Mapping[str, str],
        files: Mapping[str, tuple[str, bytes, str]],
    ) -> dict[str, Any]:
        """POST multipart form (cho upload media)."""
        url = f"{self.open_url}{path}"
        async with self._limiter:
            token = await self.get_tenant_token()
            r = await self._client.post(
                url,
                headers={"Authorization": f"Bearer {token}"},
                data=dict(data),
                files=dict(files),
                timeout=120.0,
            )
        result: dict[str, Any] = r.json()
        return result
