"""OpenAI 兼容翻译客户端和旧版文案规范化规则。"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

import httpx
from typing_extensions import Self

from .settings import DmmSettings

PROMPTS_DIR = Path(__file__).with_name("prompts")


class TranslationError(RuntimeError):
    """翻译服务没有产生可安全写回的结果。"""

    def __init__(
        self, code: str, message: str, *, retryable: bool, abort_batch: bool = False
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.abort_batch = abort_batch


def _is_model_error(response: httpx.Response) -> bool:
    if response.status_code not in {400, 404, 422}:
        return False
    try:
        data = response.json()
    except ValueError:
        return False
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        if error.get("code") in {"model_not_found", "invalid_model", "unsupported_model"}:
            return True
        if error.get("param") == "model":
            return True
        error = error.get("message")
    return isinstance(error, str) and bool(re.search(
        r"\bmodel\b.*(?:not found|does not exist|not supported|not available)"
        r"|\b(?:unknown|invalid|unsupported) model\b",
        error, re.IGNORECASE,
    ))


def load_prompt(field: str) -> str:
    filename = {
        "title": "movie_title_translation.md",
        "summary": "movie_desc_translation.md",
    }[field]
    prompt = (PROMPTS_DIR / filename).read_text(encoding="utf-8").strip()
    if not prompt:
        raise RuntimeError(f"翻译提示词为空: {filename}")
    return prompt


def normalize_translation(value: str) -> str:
    """保留旧翻译任务对思维块和“无有效内容”的语义。"""

    normalized = re.sub(r"<think>.*?</think>", "", value, flags=re.DOTALL).strip()
    return "" if "无有效内容" in normalized else normalized


class OpenAITranslationClient:
    """支持 Chat Completions 和 Responses 的翻译客户端。"""

    def __init__(
        self,
        settings: DmmSettings,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        headers = {
            "Content-Type": "application/json",
            "x-opencode-session": f"sakuramedia-translation-{uuid.uuid4().hex}",
        }
        if settings.api_key:
            headers["Authorization"] = f"Bearer {settings.api_key}"
        base_url = settings.base_url.rstrip("/")
        if not base_url.endswith("/v1"):
            base_url += "/v1"
        self._client = httpx.Client(
            base_url=base_url,
            timeout=settings.translation_timeout_seconds,
            headers=headers,
            transport=transport,
            trust_env=False,
        )
        self._consecutive_429 = 0
        self._model = settings.model
        self._api_type = settings.api_type

    def __enter__(self) -> Self:
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self._client.close()

    def _response_content(self, response: httpx.Response) -> str:
        try:
            data = response.json()
            if self._api_type == "responses":
                if data["status"] != "completed":
                    raise TranslationError(
                        "incomplete_response", "翻译服务未完成响应", retryable=True,
                    )
                parts = []
                if not isinstance(data["output"], list):
                    raise ValueError("output must be a list")
                for item in data["output"]:
                    if item["type"] != "message":
                        continue
                    for part in item["content"]:
                        if part["type"] == "refusal":
                            raise TranslationError(
                                "refusal", "翻译服务拒绝生成译文", retryable=False
                            )
                        if part["type"] == "output_text":
                            parts.append(part["text"])
                content = "".join(parts)
            else:
                content = data["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise TranslationError(
                "invalid_response",
                "翻译服务返回了非法响应结构",
                retryable=True,
            ) from exc
        if not isinstance(content, str):
            raise TranslationError(
                "invalid_response",
                "翻译服务返回了非法译文结构",
                retryable=True,
            )
        content = content.strip()
        if not content:
            raise TranslationError(
                "empty_result", "翻译服务返回了空译文", retryable=False
            )
        return content

    def translate(self, *, system_prompt: str, source_text: str) -> str:
        if self._api_type == "responses":
            endpoint = "responses"
            payload = {
                "model": self._model,
                "instructions": system_prompt,
                "input": source_text,
            }
        else:
            endpoint = "chat/completions"
            payload = {
                "model": self._model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": source_text},
                ],
            }
        try:
            response = self._client.post(
                endpoint,
                json=payload,
            )
        except httpx.HTTPError as exc:
            self._consecutive_429 = 0
            raise TranslationError(
                "request_failed", "翻译服务请求失败", retryable=True
            ) from exc

        self._consecutive_429 = (
            self._consecutive_429 + 1 if response.status_code == 429 else 0
        )
        if self._consecutive_429 >= 3:
            raise TranslationError(
                "http_429", "翻译服务连续 3 次返回 HTTP 429，已停止本轮任务",
                retryable=True, abort_batch=True,
            )
        if response.status_code >= 400:
            abort = response.status_code in {401, 403} or _is_model_error(response)
            raise TranslationError(
                f"http_{response.status_code}",
                f"翻译服务返回 HTTP {response.status_code}" + ("：认证或模型配置错误" if abort else ""),
                retryable=not abort and (response.status_code in {408, 429} or response.status_code >= 500),
                abort_batch=abort,
            )
        return self._response_content(response)
