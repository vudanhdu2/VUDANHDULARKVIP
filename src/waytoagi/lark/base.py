"""LarkBase — async wrapper cho Bitable (Base) CRUD."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping, Sequence

    from waytoagi.lark.auth import LarkAuth

logger = structlog.get_logger(__name__)


class LarkBase:
    """Bitable CRUD operations.

    Một instance bind với 1 LarkAuth. Để truy vấn cross-tenant, dùng instance khác.
    """

    def __init__(self, auth: LarkAuth) -> None:
        self.auth = auth
        self._log = logger.bind(component="LarkBase")

    # ============================================================
    # Records — query
    # ============================================================

    async def search_records(
        self,
        app_token: str,
        table_id: str,
        *,
        filter_body: Mapping[str, Any] | None = None,
        sort: Sequence[Mapping[str, Any]] | None = None,
        field_names: Sequence[str] | None = None,
        page_size: int = 500,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        """POST /search — paginated query với filter."""
        params: dict[str, Any] = {"page_size": page_size}
        if page_token:
            params["page_token"] = page_token
        body: dict[str, Any] = {}
        if filter_body:
            body["filter"] = dict(filter_body)
        if sort:
            body["sort"] = list(sort)
        if field_names:
            body["field_names"] = list(field_names)
        return await self.auth.request(
            "POST",
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/search",
            params=params,
            json_body=body or None,
        )

    async def iter_records(
        self,
        app_token: str,
        table_id: str,
        *,
        filter_body: Mapping[str, Any] | None = None,
        field_names: Sequence[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Async iterator over ALL records (auto-paginate)."""
        page_token: str | None = None
        while True:
            r = await self.search_records(
                app_token, table_id,
                filter_body=filter_body,
                field_names=field_names,
                page_token=page_token,
            )
            data = r.get("data", {})
            for item in data.get("items", []):
                yield item
            if not data.get("has_more"):
                return
            page_token = data.get("page_token")

    async def get_record(
        self, app_token: str, table_id: str, record_id: str,
    ) -> dict[str, Any]:
        return await self.auth.get(
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
        )

    # ============================================================
    # Records — mutate
    # ============================================================

    async def create_record(
        self, app_token: str, table_id: str, fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self.auth.post(
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records",
            json_body={"fields": dict(fields)},
        )

    async def batch_create(
        self,
        app_token: str,
        table_id: str,
        records: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """records: [{"fields": {...}}, ...] — max 500."""
        return await self.auth.post(
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_create",
            json_body={"records": [dict(r) for r in records]},
        )

    async def update_record(
        self,
        app_token: str,
        table_id: str,
        record_id: str,
        fields: Mapping[str, Any],
    ) -> dict[str, Any]:
        return await self.auth.put(
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
            json_body={"fields": dict(fields)},
        )

    async def batch_update(
        self,
        app_token: str,
        table_id: str,
        records: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        """records: [{"record_id": "...", "fields": {...}}, ...] — max 500."""
        return await self.auth.post(
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/batch_update",
            json_body={"records": [dict(r) for r in records]},
        )

    async def delete_record(
        self, app_token: str, table_id: str, record_id: str,
    ) -> dict[str, Any]:
        return await self.auth.delete(
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/records/{record_id}",
        )

    # ============================================================
    # Tables
    # ============================================================

    async def list_tables(self, app_token: str) -> dict[str, Any]:
        return await self.auth.get(f"/bitable/v1/apps/{app_token}/tables")

    async def list_fields(self, app_token: str, table_id: str) -> dict[str, Any]:
        return await self.auth.get(
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
        )

    async def create_field(
        self,
        app_token: str,
        table_id: str,
        *,
        field_name: str,
        field_type: int,
        property_: Mapping[str, Any] | None = None,
        description: str = "",
    ) -> dict[str, Any]:
        """Tạo field mới trong Bitable table.

        Lark API: POST /bitable/v1/apps/{app_token}/tables/{table_id}/fields
        Body: {field_name, type, property, description}
        """
        body: dict[str, Any] = {
            "field_name": field_name,
            "type": field_type,
        }
        if property_:
            body["property"] = dict(property_)
        if description:
            body["description"] = {"text": description}
        return await self.auth.post(
            f"/bitable/v1/apps/{app_token}/tables/{table_id}/fields",
            json_body=body,
        )
