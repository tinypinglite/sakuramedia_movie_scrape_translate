import json

import httpx
import pytest
from sakuramedia_movie_scrape_translate.settings import DmmSettings
from sakuramedia_movie_scrape_translate.translation import (
    OpenAITranslationClient,
    TranslationError,
    load_prompt,
    normalize_translation,
)


def _settings() -> DmmSettings:
    return DmmSettings(
        translation_enabled=True,
        base_url="https://translate.example",
        api_key="test-key",
        model="test-model",
    )


def test_client_keeps_legacy_messages_and_temperature() -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "译文"}}]})

    with OpenAITranslationClient(
        _settings(), transport=httpx.MockTransport(handler)
    ) as client:
        assert client.translate(system_prompt=load_prompt("title"), source_text="原始标题") == "译文"

    assert captured["url"] == "https://translate.example/v1/chat/completions"
    assert captured["payload"] == {
        "model": "test-model",
        "temperature": 0,
        "messages": [
            {"role": "system", "content": load_prompt("title")},
            {"role": "user", "content": "原始标题"},
        ],
    }


def test_normalization_and_structured_content_are_not_written() -> None:
    assert normalize_translation("<think>reasoning</think> 可展示") == "可展示"
    assert normalize_translation("可展示\n无有效内容") == ""

    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200, json={"choices": [{"message": {"content": ["not text"]}}]}
        )
    )
    with (
        OpenAITranslationClient(_settings(), transport=transport) as client,
        pytest.raises(TranslationError, match="非法译文结构") as error,
    ):
        client.translate(system_prompt="system", source_text="source")
    assert error.value.retryable is True
    assert error.value.abort_batch is False


@pytest.mark.parametrize("base_url", [
    "https://opencode.ai/zen/go", "https://opencode.ai/zen/go/v1/",
])
def test_responses_request_and_text_extraction(base_url: str) -> None:
    settings = _settings().model_copy(update={"api_type": "responses", "base_url": base_url})

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://opencode.ai/zen/go/v1/responses"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.headers["x-opencode-session"].startswith(
            "sakuramedia-translation-"
        )
        assert json.loads(request.content) == {
            "model": "test-model", "instructions": "system", "input": "source",
        }
        return httpx.Response(200, json={
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": []},
                {"type": "message", "content": [
                    {"type": "output_text", "text": " 中文"},
                    {"type": "output_text", "text": "译文 "},
                ]},
            ],
        })

    with OpenAITranslationClient(settings, transport=httpx.MockTransport(handler)) as client:
        assert client.translate(system_prompt="system", source_text="source") == "中文译文"


@pytest.mark.parametrize("body,code", [
    ({"status": "incomplete", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": "截断译文"},
    ]}]}, "incomplete_response"),
    ({"status": "completed", "output": []}, "empty_result"),
    ({"status": "completed", "output": [{"type": "message", "content": [
        {"type": "refusal", "refusal": "refused"},
    ]}]}, "refusal"),
    ({"status": "completed", "output": [{"type": "message", "content": [
        {"type": "output_text", "text": ["invalid"]},
    ]}]}, "invalid_response"),
    ({"status": "completed"}, "invalid_response"),
])
def test_responses_rejects_unusable_output(body: dict, code: str) -> None:
    settings = _settings().model_copy(update={"api_type": "responses"})
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
    with (
        OpenAITranslationClient(settings, transport=transport) as client,
        pytest.raises(TranslationError) as error,
    ):
        client.translate(system_prompt="system", source_text="source")
    assert error.value.code == code


def test_http_429_keeps_the_current_batch() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(429))

    with (
        OpenAITranslationClient(_settings(), transport=transport) as client,
        pytest.raises(TranslationError) as error,
    ):
        client.translate(system_prompt="system", source_text="source")

    assert error.value.code == "http_429"
    assert error.value.retryable is True
    assert error.value.abort_batch is False


@pytest.mark.parametrize("status,body,abort,retryable", [
    (401, {}, True, False), (403, {}, True, False),
    (404, {"error": {"code": "model_not_found"}}, True, False),
    (400, {"error": {"param": "model", "message": "invalid"}}, True, False),
    (404, {"error": "model 'missing' not found"}, True, False),
    (400, {"error": {"message": "The model `missing` does not exist"}}, True, False),
    (400, {"error": "invalid request"}, False, False),
    (404, {}, False, False), (408, {}, False, True),
    (429, {}, False, True), (500, {}, False, True), (503, {}, False, True),
])
@pytest.mark.parametrize("api_type", ["chat_completions", "responses"])
def test_http_error_policy(status, body, abort, retryable, api_type):
    settings = _settings().model_copy(update={"api_type": api_type})
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    with OpenAITranslationClient(settings, transport=transport) as client:
        with pytest.raises(TranslationError) as error:
            client.translate(system_prompt="system", source_text="source")
    assert error.value.abort_batch is abort
    assert error.value.retryable is retryable


def test_read_timeout_is_retryable_without_aborting():
    def timeout(request):
        raise httpx.ReadTimeout("timeout", request=request)
    with OpenAITranslationClient(_settings(), transport=httpx.MockTransport(timeout)) as client:
        with pytest.raises(TranslationError) as error:
            client.translate(system_prompt="system", source_text="source")
    assert error.value.retryable
    assert not error.value.abort_batch


@pytest.mark.parametrize("api_type", ["chat_completions", "responses"])
@pytest.mark.parametrize("interruption", [None, 200, 500, "timeout"])
def test_three_consecutive_429_abort_batch(api_type, interruption):
    statuses = [429, 429]
    if interruption is not None:
        statuses += [interruption, 429, 429]
    statuses += [429]
    responses = iter(statuses)

    def respond(request):
        status = next(responses)
        if status == "timeout":
            raise httpx.ReadTimeout("timeout", request=request)
        body = (
            {"status": "completed", "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "译文"}
            ]}]}
            if api_type == "responses"
            else {"choices": [{"message": {"content": "译文"}}]}
        )
        return httpx.Response(status, json=body)

    settings = _settings().model_copy(update={"api_type": api_type})
    with OpenAITranslationClient(settings, transport=httpx.MockTransport(respond)) as client:
        for index, status in enumerate(statuses):
            if status == 200:
                assert client.translate(system_prompt="system", source_text="source") == "译文"
                continue
            with pytest.raises(TranslationError) as error:
                client.translate(system_prompt="system", source_text="source")
            assert error.value.abort_batch is (index == len(statuses) - 1)
        assert "连续 3 次" in str(error.value)
