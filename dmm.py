"""DMM 搜索及商品文案；只有明确的空结果才记为不存在。"""

from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

import httpx


class DmmError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = True,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class DmmFetchResult:
    status: str
    title: str = ""
    desc: str = ""
    source_url: str = ""
    source_id: str = ""


def _text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


class _Page(HTMLParser):
    """只读取可见文案和 Product JSON，忽略页面脚本里的错误页模板。"""

    def __init__(self, content: str):
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.visible: list[str] = []
        self.page_title: list[str] = []
        self.heading: list[str] = []
        self.description: list[str] = []
        self.products: list[dict] = []
        self._stack: list[tuple[str, set[str]]] = []
        self._script: list[str] = []
        self.feed(content)
        self.close()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        flags = set()
        if tag in {"script", "style"}:
            flags.add("hidden")
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            flags.add("json")
            self._script = []
        if tag == "title":
            flags.add("title")
        if tag == "h1" and (
            attrs.get("id") == "title"
            or {"item", "fn"} <= set(attrs.get("class", "").split())
        ):
            flags.add("heading")
        if tag == "div" and {"mg-b20", "lh4"} <= set(attrs.get("class", "").split()):
            flags.add("description")
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag in {"br", "p"}:
            self.handle_data(" ")
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self._stack.append((tag, flags))

    def handle_endtag(self, tag):
        for i in range(len(self._stack) - 1, -1, -1):
            if self._stack[i][0] == tag:
                if "json" in self._stack[i][1]:
                    try:
                        self._read_products(json.loads("".join(self._script)))
                    except (ValueError, TypeError):
                        pass
                del self._stack[i:]
                break

    def handle_data(self, data):
        flags = set().union(*(flags for _, flags in self._stack))
        if "json" in flags:
            self._script.append(data)
        if "hidden" in flags:
            return
        self.visible.append(data)
        for flag, dest in (
            ("title", self.page_title),
            ("heading", self.heading),
            ("description", self.description),
        ):
            if flag in flags:
                dest.append(data)

    def _read_products(self, value):
        if isinstance(value, list):
            for item in value:
                self._read_products(item)
        elif isinstance(value, dict):
            kind = value.get("@type", [])
            if kind == "Product" or isinstance(kind, list) and "Product" in kind:
                self.products.append(value)
            if "@graph" in value:
                self._read_products(value["@graph"])


def _cid(url: str) -> str:
    match = re.search(r"cid=([^/?&#]+)", url)
    return match.group(1) if match else ""


def _matches(cid: str, number: str) -> bool:
    # 仅转换有字母前缀的 DMM 品番，不折叠纯数字番号的有效分隔符。
    match = re.fullmatch(r"([a-z]{2,10})[-_ ]?(\d{2,8})", number, re.IGNORECASE)
    if not match:
        return cid.casefold() == number.casefold()
    product = re.fullmatch(r"\d*([a-z]{2,10})(\d+)", cid, re.IGNORECASE)
    return bool(
        product
        and product[1].casefold() == match[1].casefold()
        and int(product[2]) == int(match[2])
    )


def _detail_url(value: str) -> str:
    url = urljoin("https://www.dmm.co.jp/", html.unescape(value))
    parts = urlsplit(url)
    if (
        parts.scheme == "https"
        and parts.netloc == "www.dmm.co.jp"
        and "/detail/=/cid=" in parts.path
    ):
        return url.split("?", 1)[0]
    return ""


class DmmClient:
    def __init__(
        self,
        timeout_seconds: float = 20,
        *,
        transport=None,
        request_interval_seconds: float = 1,
    ):
        self.request_interval_seconds = request_interval_seconds
        self._last_request_at = 0.0
        try:
            self.client = httpx.Client(
                timeout=timeout_seconds,
                follow_redirects=True,
                cookies={"age_check_done": "1"},
                transport=transport,
                trust_env=transport is None,
                headers={"User-Agent": "SakuraMedia-DMM/0.1"},
            )
        except (ImportError, ValueError) as exc:
            raise DmmError(
                "proxy_configuration",
                "DMM 代理配置不可用，请检查代理协议及依赖",
                retryable=False,
            ) from exc

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.client.close()

    def _page(self, url: str) -> _Page:
        delay = self.request_interval_seconds - (
            time.monotonic() - self._last_request_at
        )
        if delay > 0:
            time.sleep(delay)
        try:
            response = self.client.get(url)
        except httpx.HTTPError as exc:
            raise DmmError(
                "request_failed", "DMM 请求失败或超时"
            ) from exc
        finally:
            self._last_request_at = time.monotonic()
        if response.status_code != 200:
            raise DmmError(
                f"http_{response.status_code}",
                f"DMM 返回 HTTP {response.status_code}",
            )
        if response.url.host != "www.dmm.co.jp" or "age_check" in str(response.url):
            raise DmmError(
                "unexpected_redirect", "DMM 返回验证页或未知跳转"
            )
        if _cid(url) and _cid(str(response.url)) != _cid(url):
            raise DmmError("product_redirect", "DMM 商品跳转后品番不一致")
        page = _Page(response.text)
        title = _text("".join(page.page_title)).lower()
        if any(
            marker in title
            for marker in (
                "年齢認証",
                "年齢確認",
                "access denied",
                "just a moment",
                "captcha",
            )
        ):
            raise DmmError("challenge", "DMM 返回验证页")
        return page

    def fetch(self, movie_number: str) -> DmmFetchResult:
        number = movie_number.strip()
        if not number:
            raise DmmError("invalid_number", "番号不能为空", retryable=False)
        search = self._page(
            f"https://www.dmm.co.jp/search/=/searchstr={quote(number, safe='')}/limit=30/sort=date/"
        )
        page_title = _text("".join(search.page_title))
        if (
            "検索結果" not in page_title
            or number.casefold() not in page_title.casefold()
        ):
            raise DmmError(
                "search_parse_error", "无法确认 DMM 搜索页面，未标记为不存在"
            )
        urls = list(
            dict.fromkeys(url for link in search.links if (url := _detail_url(link)))
        )
        matches = [url for url in urls if _matches(_cid(url), number)]
        if not matches:
            visible = _text("".join(search.visible))
            if not urls and "に一致する商品は見つかりませんでした" in visible:
                return DmmFetchResult("not_found")
            raise DmmError(
                "search_unmatched", "DMM 搜索结果无法精确匹配番号，未标记为不存在"
            )
        # 原版实体 DVD 优先，避免选择搜索结果前列的廉价版或蓝光版标题。
        matches.sort(
            key=lambda url: ("/mono/" not in url, bool(re.match(r"\d", _cid(url))))
        )
        best = None
        for url in matches:
            page = self._page(url)
            title = _text("".join(page.heading))
            desc = _text("".join(page.description))
            for product in page.products:
                title = title or _text(
                    product.get("name") if isinstance(product.get("name"), str) else ""
                )
                desc = desc or _text(
                    product.get("description")
                    if isinstance(product.get("description"), str)
                    else ""
                )
            if not title and not desc:
                raise DmmError("detail_parse_error", "DMM 商品详情结构无法识别")
            result = DmmFetchResult(
                "success" if title and desc else "partial", title, desc, url, _cid(url)
            )
            if title and desc:
                return result
            best = best or result
        return best
