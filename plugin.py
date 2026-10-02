"""SakuraMedia 影片文案抓取与翻译插件入口。"""

from __future__ import annotations

import json
from pathlib import Path

from src.plugins import HOST_API_VERSION, PluginContext, PluginRegistration

from .jobs import build_jobs
from .settings import DmmSettings

__version__ = json.loads(
    Path(__file__).with_name("manifest.json").read_text(encoding="utf-8")
)["version"]
PLUGIN_ID = "sakuramedia_movie_scrape_translate"
DISPLAY_NAME = "SakuraMedia 影片文案抓取与翻译"


def register(context: PluginContext) -> PluginRegistration:
    """声明任务；加载阶段不请求 DMM 或翻译服务。"""

    settings = DmmSettings.model_validate(context.settings)
    return PluginRegistration(
        plugin_id=PLUGIN_ID,
        display_name=DISPLAY_NAME,
        version=__version__,
        host_api_version=HOST_API_VERSION,
        jobs=build_jobs(context, settings=settings),
    )
