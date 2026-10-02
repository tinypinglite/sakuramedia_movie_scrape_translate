import pytest
from pydantic import ValidationError
from sakuramedia_movie_scrape_translate.settings import DmmSettings


def test_translation_is_disabled_without_configuration() -> None:
    settings = DmmSettings.model_validate({})

    assert settings.translation_enabled is False
    assert settings.request_interval_seconds == 1.0
    assert settings.model == ""
    assert settings.api_type == "chat_completions"


def test_unknown_api_type_is_rejected() -> None:
    with pytest.raises(ValidationError, match="api_type"):
        DmmSettings(api_type="unknown")


def test_enabled_translation_requires_valid_root_url_and_model() -> None:
    with pytest.raises(ValidationError, match="base_url"):
        DmmSettings(translation_enabled=True, model="gpt")
    with pytest.raises(ValidationError, match="http"):
        DmmSettings(translation_enabled=True, base_url="localhost:1234", model="gpt")

    settings = DmmSettings(
        translation_enabled=True,
        base_url="https://example.test/v1/",
        model="gpt",
    )
    assert settings.base_url == "https://example.test"
