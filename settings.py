"""DMM 插件配置。"""

from __future__ import annotations

from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DmmSettings(BaseModel):
    """DMM 请求和可选 OpenAI 兼容翻译服务的私有配置。"""

    model_config = ConfigDict(extra="forbid")

    request_timeout_seconds: float = Field(default=20.0, gt=0, le=120, title="请求超时（秒）")
    request_interval_seconds: float = Field(default=1.0, ge=0, le=60, title="请求间隔（秒）")
    translation_enabled: bool = Field(default=False, title="启用翻译")
    base_url: str = Field(default="", title="翻译服务地址")
    api_key: str = Field(default="", title="API 密钥", json_schema_extra={"format": "password"})
    model: str = Field(default="", title="翻译模型")
    api_type: Literal["chat_completions", "responses"] = Field(default="chat_completions", title="API 类型")
    translation_timeout_seconds: float = Field(default=60.0, gt=0, le=300, title="翻译超时（秒）")

    @field_validator("base_url", "api_key", "model")
    @classmethod
    def _strip_text(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def _require_translation_target(self):
        if self.translation_enabled and not self.base_url:
            raise ValueError("启用翻译时 base_url 不能为空")
        if self.translation_enabled and not self.model:
            raise ValueError("启用翻译时 model 不能为空")
        if self.translation_enabled:
            parsed = urlsplit(self.base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ValueError("base_url 必须是 http(s) 服务根地址")
            if parsed.username or parsed.password:
                raise ValueError("base_url 不能包含凭据")
            if parsed.query or parsed.fragment:
                raise ValueError("base_url 不能包含查询参数或片段")
            # 保留既有配置将根路径 /v1 规范为服务根地址的行为。
            if parsed.path.rstrip("/") == "/v1":
                self.base_url = self.base_url.rstrip("/")[:-3]
        return self
