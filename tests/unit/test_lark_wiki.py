"""Unit tests cho LarkWiki — move_node, list_children_tokens, walk_tree."""

from __future__ import annotations

import httpx
import pytest
import respx

from waytoagi.lark.auth import LarkAuth
from waytoagi.lark.wiki import LarkWiki

OPEN_URL = "https://open.example.com/open-apis"


@pytest.fixture
async def auth_with_token() -> LarkAuth:
    """LarkAuth với token đã refresh — tránh mock token endpoint mỗi test."""
    return LarkAuth(
        app_id="cli_test",
        app_secret="secret_test",
        open_url=OPEN_URL,
        rate_limit_rps=200,  # cao tránh test bị throttle
    )


def _mock_token() -> respx.Route:
    return respx.post(f"{OPEN_URL}/auth/v3/tenant_access_token/internal").mock(
        return_value=httpx.Response(200, json={
            "code": 0, "tenant_access_token": "t-test", "expire": 7200,
        }),
    )


@pytest.mark.unit
class TestMoveNode:
    @pytest.mark.asyncio
    @respx.mock
    async def test_move_node_basic(self, auth_with_token: LarkAuth) -> None:
        _mock_token()
        route = respx.post(
            f"{OPEN_URL}/wiki/v2/spaces/sp1/nodes/nd1/move",
        ).mock(
            return_value=httpx.Response(200, json={
                "code": 0, "data": {"node": {"node_token": "nd1"}},
            }),
        )
        wiki = LarkWiki(auth_with_token)
        r = await wiki.move_node(
            "sp1", node_token="nd1", target_parent_token="parent1",
        )
        assert r["code"] == 0
        assert route.called
        body = route.calls[0].request.content
        assert b'"target_parent_token":"parent1"' in body
        assert b'"target_space_id":"sp1"' in body  # default = same space
        await auth_with_token.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_move_node_explicit_target_space(
        self, auth_with_token: LarkAuth,
    ) -> None:
        _mock_token()
        route = respx.post(
            f"{OPEN_URL}/wiki/v2/spaces/sp1/nodes/nd1/move",
        ).mock(
            return_value=httpx.Response(200, json={"code": 0, "data": {}}),
        )
        wiki = LarkWiki(auth_with_token)
        await wiki.move_node(
            "sp1",
            node_token="nd1",
            target_parent_token="parent1",
            target_space_id="sp_other",
        )
        body = route.calls[0].request.content
        assert b'"target_space_id":"sp_other"' in body
        await auth_with_token.aclose()


@pytest.mark.unit
class TestListChildrenTokens:
    @pytest.mark.asyncio
    @respx.mock
    async def test_single_page(self, auth_with_token: LarkAuth) -> None:
        _mock_token()
        respx.get(f"{OPEN_URL}/wiki/v2/spaces/sp1/nodes").mock(
            return_value=httpx.Response(200, json={
                "code": 0, "data": {
                    "items": [
                        {"node_token": "nd1", "title": "A"},
                        {"node_token": "nd2", "title": "B"},
                    ],
                    "has_more": False,
                },
            }),
        )
        wiki = LarkWiki(auth_with_token)
        tokens = await wiki.list_children_tokens("sp1", "parent1")
        assert tokens == ["nd1", "nd2"]
        await auth_with_token.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_multi_page_pagination(
        self, auth_with_token: LarkAuth,
    ) -> None:
        _mock_token()
        respx.get(f"{OPEN_URL}/wiki/v2/spaces/sp1/nodes").mock(
            side_effect=[
                httpx.Response(200, json={
                    "code": 0, "data": {
                        "items": [{"node_token": "nd1"}],
                        "has_more": True, "page_token": "tok2",
                    },
                }),
                httpx.Response(200, json={
                    "code": 0, "data": {
                        "items": [{"node_token": "nd2"}, {"node_token": "nd3"}],
                        "has_more": False,
                    },
                }),
            ],
        )
        wiki = LarkWiki(auth_with_token)
        tokens = await wiki.list_children_tokens("sp1", "parent1")
        assert tokens == ["nd1", "nd2", "nd3"]
        await auth_with_token.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_skips_items_without_node_token(
        self, auth_with_token: LarkAuth,
    ) -> None:
        _mock_token()
        respx.get(f"{OPEN_URL}/wiki/v2/spaces/sp1/nodes").mock(
            return_value=httpx.Response(200, json={
                "code": 0, "data": {
                    "items": [
                        {"node_token": "nd1"},
                        {"title": "no token"},  # malformed item
                        {"node_token": "nd2"},
                    ],
                    "has_more": False,
                },
            }),
        )
        wiki = LarkWiki(auth_with_token)
        tokens = await wiki.list_children_tokens("sp1", "parent1")
        assert tokens == ["nd1", "nd2"]
        await auth_with_token.aclose()

    @pytest.mark.asyncio
    @respx.mock
    async def test_empty_response(self, auth_with_token: LarkAuth) -> None:
        _mock_token()
        respx.get(f"{OPEN_URL}/wiki/v2/spaces/sp1/nodes").mock(
            return_value=httpx.Response(200, json={
                "code": 0, "data": {"items": [], "has_more": False},
            }),
        )
        wiki = LarkWiki(auth_with_token)
        tokens = await wiki.list_children_tokens("sp1", "parent1")
        assert tokens == []
        await auth_with_token.aclose()


@pytest.mark.unit
class TestIterChildren:
    @pytest.mark.asyncio
    @respx.mock
    async def test_yields_all_items(self, auth_with_token: LarkAuth) -> None:
        _mock_token()
        respx.get(f"{OPEN_URL}/wiki/v2/spaces/sp1/nodes").mock(
            return_value=httpx.Response(200, json={
                "code": 0, "data": {
                    "items": [
                        {"node_token": "nd1", "title": "A"},
                        {"node_token": "nd2", "title": "B"},
                    ],
                    "has_more": False,
                },
            }),
        )
        wiki = LarkWiki(auth_with_token)
        items = [x async for x in wiki.iter_children("sp1", parent_node_token="p")]
        assert len(items) == 2
        assert items[0]["title"] == "A"
        await auth_with_token.aclose()
