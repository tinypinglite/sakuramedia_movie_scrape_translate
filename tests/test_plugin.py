from pathlib import Path

from sakuramedia_movie_scrape_translate.plugin import PLUGIN_ID, register
from src.plugins import HOST_API_VERSION, PluginContext


def test_register_accepts_empty_settings_without_translation_client() -> None:
    registration = register(
        PluginContext(
            plugin_id=PLUGIN_ID,
            settings={},
            data_dir=Path("/tmp/sakuramedia-movie-scrape-translate-test"),
        )
    )

    assert registration.host_api_version == HOST_API_VERSION
    assert [job.task_key for job in registration.jobs] == [
        "sakuramedia_movie_scrape_translate_sync",
        "sakuramedia_movie_scrape_translate_sync_subscribed",
        "sakuramedia_movie_scrape_translate_translate_cached",
    ]
    assert registration.jobs[1].default_cron == "10 4 * * *"
    assert registration.jobs[2].manual_only is True
    assert registration.jobs[2].default_cron is None
    assert registration.jobs[2].params_schema is None
