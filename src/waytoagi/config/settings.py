"""Pydantic Settings — load configuration từ .env + env vars.

Single source of truth cho tất cả config trong dự án. KHÔNG hardcode constants
trong code khác — luôn import từ here.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Self

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMEndpoint(BaseModel):
    """Single LLM endpoint configuration."""

    endpoint: str = Field(..., description="OpenAI-compatible base URL")
    api_key: str = Field(..., description="API key")
    model: str = Field(..., description="Model name")
    name: str = Field(..., description="Human-readable name (vd 'GPT-5.4-local')")


class Settings(BaseSettings):
    """Application settings — loaded từ .env + env vars."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ============================================================
    # Lark configuration (DST = Larksuite, target tenant)
    # ============================================================
    # Default values là PLACEHOLDER. Set qua .env hoặc env vars.
    # Xem .env.example để biết format.
    lark_app_id: str = Field(default="")
    lark_app_secret: str = Field(default="")
    lark_open_url: str = Field(default="https://open.larksuite.com/open-apis")
    lark_domain: str = Field(default="")
    lark_cli_path: Path = Field(default=Path("lark-cli"))

    # Lark Base
    lark_base_token: str = Field(default="")
    lark_table_id: str = Field(default="")

    # Lark Spaces
    lark_src_space: str = Field(default="")
    lark_working_space: str = Field(default="")
    lark_dst_space: str = Field(default="")
    lark_cn_parent: str = Field(default="")
    lark_vi_parent: str = Field(default="")

    # Source tenant — cross-tenant block read
    # Nếu source ở CÙNG TENANT với DST → để rỗng, code dùng LARK_APP_* thay thế.
    # Nếu source ở Feishu CN → set FEISHU_APP_*  + SOURCE_OPEN_URL=https://open.feishu.cn/open-apis
    # Nếu source ở Larksuite tenant khác → set FEISHU_APP_* (đăng ký ở source tenant)
    #     + SOURCE_OPEN_URL=https://open.larksuite.com/open-apis
    source_open_url: str = Field(
        default="https://open.larksuite.com/open-apis",
        description=(
            "Source tenant API base. Default Larksuite. "
            "Set 'https://open.feishu.cn/open-apis' nếu source ở Feishu CN."
        ),
    )
    feishu_app_id: str = Field(
        default="",
        description="Source app ID. Để rỗng nếu source CÙNG TENANT với DST.",
    )
    feishu_app_secret: str = Field(default="")
    feishu_open_url: str = Field(
        default="https://open.larksuite.com/open-apis",
        description="DEPRECATED — alias cho source_open_url. Giữ để backward compat.",
    )

    # ============================================================
    # LLM POOL (loaded từ file)
    # ============================================================
    llm_keys_file: Path = Field(default=Path("llm_keys.json"))
    llm_endpoints: list[LLMEndpoint] = Field(default_factory=list)

    # ============================================================
    # Rate limiting
    # ============================================================
    lark_rate_limit_rps: int = Field(default=5, ge=1, le=50)
    llm_rate_limit_rps: int = Field(default=10, ge=1, le=100)

    # ============================================================
    # Cache
    # ============================================================
    translation_cache_db: Path = Field(default=Path(".cache/translations.sqlite"))

    # ============================================================
    # Logging
    # ============================================================
    log_level: str = Field(default="INFO")
    log_format: str = Field(default="json", pattern="^(json|console)$")

    # ============================================================
    # Validators
    # ============================================================
    @model_validator(mode="after")
    def load_llm_endpoints(self) -> Self:
        """Load LLM endpoints từ JSON file nếu có."""
        if self.llm_endpoints:
            return self
        if self.llm_keys_file.is_file():
            data = json.loads(self.llm_keys_file.read_text(encoding="utf-8"))
            self.llm_endpoints = [LLMEndpoint(**entry) for entry in data]
        return self

    # ============================================================
    # Computed properties
    # ============================================================
    @property
    def source_same_tenant_as_dst(self) -> bool:
        """Source CÙNG cloud + tenant với DST?

        True nếu cả 2 đều ở Larksuite (open.larksuite.com) — KHÔNG cần Feishu app.
        False nếu source ở Feishu CN hoặc tenant Larksuite khác.
        """
        return (
            "larksuite.com" in self.source_open_url
            and "larksuite.com" in self.lark_open_url
            and not self.feishu_app_id  # nếu user explicit đặt FEISHU_APP_ID → khác tenant
        )

    @property
    def effective_source_app_id(self) -> str:
        """App ID cho source tenant.

        - Nếu source CÙNG TENANT với DST → trả lark_app_id (Larksuite App của user).
        - Nếu source khác tenant (Feishu CN hoặc Larksuite khác) → trả feishu_app_id.
        """
        if self.source_same_tenant_as_dst:
            return self.lark_app_id
        return self.feishu_app_id

    @property
    def effective_source_app_secret(self) -> str:
        """App Secret cho source tenant — same logic như effective_source_app_id."""
        if self.source_same_tenant_as_dst:
            return self.lark_app_secret
        return self.feishu_app_secret


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Singleton Settings instance — cached."""
    return Settings()
