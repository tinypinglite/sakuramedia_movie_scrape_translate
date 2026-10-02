"""SakuraMedia 影片文案抓取与翻译插件。"""

from .plugin import register
from .settings import DmmSettings

__all__ = ["DmmSettings", "register"]
