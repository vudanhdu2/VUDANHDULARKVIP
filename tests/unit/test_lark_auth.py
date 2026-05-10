"""Unit tests cho LarkAuth — mock httpx bằng respx."""

from __future__ import annotations

import httpx
import pytest
import respx

from waytoagi.lark.auth import LarkAPIError, LarkAuth

OPEN_URL = "https://open.example.com/open-apis"


@pytest.fixture
def auth() -> LarkAuth:
    return LarkAuth(
        app_id="cli_test", app_secret="secret_test",
        open_url=OPEN_URL, rate_limit_rps=50,
    )


@pytest.mark.unit
class TestTokenLifecycle:
    @pytest.mark.asyncio
    @respx.mock
    async def test_refresh_token_success(self, auth: LarkAuth) -> None:
        respx.post(f"{OPEN_URL}/auth/v3/tenant_access_token/internal").mock(
            return_value=httpx.Response(200, json={
                "code": 0, "msg": "ok",
                "tenant_access_token": "t-abc", "expire": 7200,
            }),
        )
        token = await auth.get_tenant_token()
        assert token == "t-abc"
        # Cache: 2nd call không gọi network
        token2 = await auth.get_tenant_token()
        assert token2 == "t-abc"
        await auth.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_refresh_token_failure(self, auth: LarkAuth) -> None:
        respx.post(f"{OPEN_URL}/auth/v3/tenant_access_token/internal").mock(
            return_value=httpx.Response(200, json={
                "code": 99991663, "msg": "invalid app_secret",
            }),
        )
        with pytest.raises(LarkAPIError) as ex:
            await auth.get_tenant_token()
        assert ex.value.code == 99991663
        await auth.aclose()


@pytest.mark.unit
class TestRequestRetry:
    @pytest.mark.asyncio
    @respx.mock
    async def test_get_success(self, auth: LarkAuth) -> None:
        respx.post(f"{OPEN_URL}/auth/v3/tenant_access_token/internal").mock(
            return_value=httpx.Response(200, json={
                "code": 0, "tenant_access_token": "t", "expire": 7200,
            }),
        )
        respx.get(f"{OPEN_URL}/wiki/v2/spaces/get_node").mock(
            return_value=httpx.Response(200, json={
                "code": 0, "data": {"node": {"obj_token": "doc-1"}},
            }),
        )
        r = await auth.get("/wiki/v2/spaces/get_node", params={"token": "x"})
        assert r["data"]["node"]["obj_token"] == "doc-1"
        await auth.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_nonzero_code_raises(self, auth: LarkAuth) -> None:
        respx.post(f"{OPEN_URL}/auth/v3/tenant_access_token/internal").mock(
            return_value=httpx.Response(200, json={
                "code": 0, "tenant_access_token": "t", "expire": 7200,
            }),
        )
        respx.get(f"{OPEN_URL}/wiki/v2/spaces/get_node").mock(
            return_value=httpx.Response(200, json={
                "code": 131005, "msg": "node not found",
            }),
        )
        with pytest.raises(LarkAPIError) as ex:
            await auth.get("/wiki/v2/spaces/get_node")
        assert ex.value.code == 131005
        await auth.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_retryable_5xx_then_success(self, auth: LarkAuth) -> None:
        respx.post(f"{OPEN_URL}/auth/v3/tenant_access_token/internal").mock(
            return_value=httpx.Response(200, json={
                "code": 0, "tenant_access_token": "t", "expire": 7200,
            }),
        )
        route = respx.get(f"{OPEN_URL}/foo").mock(side_effect=[
            httpx.Response(503),
            httpx.Response(200, json={"code": 0, "data": {"ok": True}}),
        ])
        r = await auth.get("/foo")
        assert r["data"]["ok"] is True
        assert route.call_count == 2
        await auth.aclose()
