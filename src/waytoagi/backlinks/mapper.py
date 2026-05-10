"""URL mapping CN source → DST mirror.

Build từ Lark Base records: mỗi BaseRecord có (Node Token, Mirror Wiki Node Token)
→ map (source_token → dst_url).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

if TYPE_CHECKING:
    from collections.abc import Iterable

    from waytoagi.models.base import BaseRecord


class UrlMapper(BaseModel):
    """Source URL → DST URL mapper.

    Builds mapping từ Base records:
        source_token (Node Token) → DST URL (https://dst-domain/wiki/MirrorToken)

    Lookup logic:
        1. Parse URL → extract source_token
        2. Lookup token trong mapping
        3. Build new URL với DST domain + DST token + preserve query/anchor

    Usage:
        mapper = UrlMapper.from_records(records, source_domain="waytoagi.feishu.cn",
                                         dst_domain="vudanhdu.sg.larksuite.com")
        new_url, replaced = mapper.replace("https://waytoagi.feishu.cn/wiki/X?p=1#sec")
        # → new_url = "https://vudanhdu.sg.larksuite.com/wiki/Y?p=1#sec", replaced = True
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    source_domain: str = Field(..., description="Source domain (vd waytoagi.feishu.cn)")
    dst_domain: str = Field(..., description="DST domain (vd vudanhdu.sg.larksuite.com)")
    mapping: dict[str, str] = Field(
        default_factory=dict,
        description="source_token → dst_token",
    )

    _src_pattern: re.Pattern[str] = PrivateAttr()

    def model_post_init(self, __context: Any) -> None:
        """Build regex pattern cho source URL — sau khi domain được set."""
        # Match cả wiki/docx/base/sheets paths
        self._src_pattern = re.compile(
            rf"https?://{re.escape(self.source_domain)}/(wiki|docx|base|sheets)/([A-Za-z0-9]+)",
            re.IGNORECASE,
        )

    @classmethod
    def from_records(
        cls,
        records: Iterable[BaseRecord],
        source_domain: str,
        dst_domain: str,
    ) -> UrlMapper:
        """Build mapper từ Base records.

        Args:
            records: BaseRecord iterable
            source_domain: source tenant domain
            dst_domain: DST tenant domain

        Returns:
            UrlMapper với mapping populated
        """
        mapping: dict[str, str] = {}
        for rec in records:
            if rec.node_token and rec.mirror_wiki_node_token:
                mapping[rec.node_token] = rec.mirror_wiki_node_token
        return cls(source_domain=source_domain, dst_domain=dst_domain, mapping=mapping)

    def replace(self, url: str) -> tuple[str, bool]:
        """Replace 1 URL nếu match source pattern + có trong mapping.

        Args:
            url: original URL (có thể URL-encoded)

        Returns:
            (new_url, replaced):
              - replaced=True nếu URL được thay (DST URL trả về)
              - replaced=False nếu không match hoặc không có mapping (URL gốc trả về)

        Preserve anchor (#xxx) + query (?yyy). Idempotent: nếu URL đã DST → không touch.
        """
        if not url:
            return url, False

        # Skip nếu URL đã trỏ DST (idempotent)
        if self.dst_domain in url:
            return url, False

        # Decode (URL có thể bị encode khi qua Lark API)
        try:
            decoded = unquote(url)
        except Exception:
            decoded = url

        match = self._src_pattern.search(decoded)
        if not match:
            return url, False

        src_token = match.group(2)
        if src_token not in self.mapping:
            return url, False

        dst_token = self.mapping[src_token]
        # Preserve anything after the matched token (query, anchor, etc.)
        suffix = decoded[match.end():]
        new_url = f"https://{self.dst_domain}/wiki/{dst_token}{suffix}"
        return new_url, True

    def __len__(self) -> int:
        """Number of mappings loaded."""
        return len(self.mapping)


def replace_url(url: str, mapper: UrlMapper) -> tuple[str, bool]:
    """Convenience wrapper for UrlMapper.replace."""
    return mapper.replace(url)
