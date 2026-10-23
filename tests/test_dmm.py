import httpx
import pytest
from sakuramedia_movie_scrape_translate.dmm import DmmClient, DmmError

SEARCH = "<title>ABP-123の検索結果 - FANZA</title>"
URL = "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=abp00123/"
DETAIL = '<h1 id="title">Original title</h1><div class="mg-b20 lh4"><p class="mg-b20">Original<br>description &amp; text</p></div>'


def client_for(search, detail=DETAIL, status=200):
    calls = []

    def handle(request):
        calls.append(str(request.url))
        body = search if "/search/" in request.url.path else detail
        return httpx.Response(status, text=body)

    return DmmClient(
        transport=httpx.MockTransport(handle), request_interval_seconds=0
    ), calls


def test_current_search_and_mono_extract_both_fields():
    client, calls = client_for(SEARCH + f'<a href="{URL}">Product</a>')
    with client:
        result = client.fetch("ABP-123")
    assert result.status == "success"
    assert result.title == "Original title"
    assert result.desc == "Original description & text"
    assert result.source_id == "abp00123"
    assert len(calls) == 2


def test_explicit_empty_results_are_terminal_and_ignore_script_templates():
    body = SEARCH + "<p>キーワードに一致する商品は見つかりませんでした。</p>"
    client, calls = client_for(body)
    with client:
        assert client.fetch("ABP-123").status == "not_found"
    assert len(calls) == 1
    body = SEARCH + '<script>"に一致する商品は見つかりませんでした"</script>'
    client, _ = client_for(body)
    with client, pytest.raises(DmmError, match="精确匹配"):
        client.fetch("ABP-123")


@pytest.mark.parametrize(
    "body",
    [
        "<title>年齢確認</title>",
        "<title>Just a moment...</title>",
        SEARCH + "<p>Unknown new search layout</p>",
        SEARCH
        + '<a href="https://www.dmm.co.jp.evil.test/mono/dvd/-/detail/=/cid=abp00123/">fake</a>',
        SEARCH
        + '<a href="https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=xabp00123/">different prefix</a>',
    ],
)
def test_unrecognized_pages_are_errors_never_not_found(body):
    client, calls = client_for(body)
    with client, pytest.raises(DmmError):
        client.fetch("ABP-123")
    assert len(calls) == 1


@pytest.mark.parametrize("status", [403, 429, 503])
def test_upstream_failure_is_retryable(status):
    client, _ = client_for("", status=status)
    with client, pytest.raises(DmmError) as error:
        client.fetch("ABP-123")
    assert error.value.retryable
    assert error.value.code == f"http_{status}"


def test_known_product_without_description_is_partial():
    client, _ = client_for(
        SEARCH + f'<a href="{URL}">Product</a>', '<h1 id="title">Title only</h1>'
    )
    with client:
        result = client.fetch("ABP-123")
    assert result.status == "partial"
    assert result.title == "Title only"
    assert result.desc == ""


def test_broken_detail_is_not_treated_as_missing_description():
    client, _ = client_for(
        SEARCH + f'<a href="{URL}">Product</a>', "<h1>Unknown template</h1>"
    )
    with client, pytest.raises(DmmError) as error:
        client.fetch("ABP-123")
    assert error.value.code == "detail_parse_error"


def test_rental_product_json_extracts_title_and_description():
    body = '<script type="application/ld+json">{"@graph":[{"@type":"Product","name":"Title","description":"Description"}]}</script>'
    rental_url = "https://www.dmm.co.jp/rental/ppr/-/detail/=/cid=4abp123/"
    client, _ = client_for(SEARCH + f'<a href="{rental_url}">Product</a>', body)
    with client:
        result = client.fetch("ABP-123")
    assert (result.status, result.title, result.desc) == (
        "success",
        "Title",
        "Description",
    )


def test_current_rental_html_survives_invalid_json_ld():
    body = (
        '<h1 class="item fn bold">Rental title</h1>'
        '<div class="mg-b20 lh4">Rental description</div>'
        '<script type="application/ld+json">{"description":"bad\njson"}</script>'
    )
    rental_url = "https://www.dmm.co.jp/rental/ppr/-/detail/=/cid=4abp123/"
    client, _ = client_for(SEARCH + f'<a href="{rental_url}">Product</a>', body)
    with client:
        result = client.fetch("ABP-123")
    assert (result.status, result.title, result.desc) == (
        "success",
        "Rental title",
        "Rental description",
    )


def test_original_product_preferred_over_discount_and_rental():
    body = SEARCH + "".join(
        f'<a href="{u}">Product</a>'
        for u in [
            URL.replace("abp00123", "77abp123"),
            "https://www.dmm.co.jp/rental/ppr/-/detail/=/cid=4abp123/",
            URL,
        ]
    )
    client, calls = client_for(body)
    with client:
        assert client.fetch("ABP-123").source_url == URL
    assert len(calls) == 2


def test_wrong_product_redirect_is_rejected():
    def handle(request):
        if "/search/" in request.url.path:
            return httpx.Response(200, text=SEARCH + f'<a href="{URL}">Product</a>')
        if "abp00123" in request.url.path:
            return httpx.Response(
                302, headers={"location": URL.replace("abp00123", "abp00124")}
            )
        return httpx.Response(200, text=DETAIL)

    with (
        DmmClient(
            transport=httpx.MockTransport(handle), request_interval_seconds=0
        ) as client,
        pytest.raises(DmmError) as error,
    ):
        client.fetch("ABP-123")
    assert error.value.code == "product_redirect"


def test_h_prefixed_cids_match_and_original_mono_is_preferred():
    search = "<title>SAN-089の検索結果 - FANZA</title>"
    rental = "https://www.dmm.co.jp/rental/ppr/-/detail/=/cid=h_796san089r/"
    mono = "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=h_796san089/"
    client, calls = client_for(
        search + f'<a href="{rental}">Rental</a><a href="{mono}">Mono</a>'
    )
    with client:
        result = client.fetch("SAN-089")
    assert result.status == "success"
    assert result.source_id == "h_796san089"
    assert len(calls) == 2


def test_single_letter_number_matches_digit_prefixed_cid():
    search = "<title>T-28632の検索結果 - FANZA</title>"
    url = "https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=55t28632/"
    client, _ = client_for(search + f'<a href="{url}">Product</a>')
    with client:
        result = client.fetch("T-28632")
    assert result.status == "success"
    assert result.source_id == "55t28632"


def test_monthly_detail_page_extracts_title_and_description():
    search = "<title>SIMM-350の検索結果 - FANZA</title>"
    url = "https://www.dmm.co.jp/monthly/premium/-/detail/=/cid=simm350/"
    body = (
        '<div class="title-area"><div class="title-block__title"><h1>もなみさん</h1></div></div>'
        '<!-- 商品説明 -->'
        '<div class="description-area"><div class="product-description-block">'
        '<div class="ignore-new-line">一行目<br>二行目</div>'
        '<div class="hidden-paragraph">重复内容</div>'
        "</div></div>"
    )
    client, _ = client_for(search + f'<a href="{url}">Product</a>', body)
    with client:
        result = client.fetch("SIMM-350")
    assert (result.status, result.title, result.desc) == (
        "success",
        "もなみさん",
        "一行目 二行目",
    )


def test_unparseable_candidate_falls_back_to_next_match():
    def handle(request):
        if "/search/" in request.url.path:
            return httpx.Response(
                200,
                text=SEARCH
                + f'<a href="{URL}">Broken</a>'
                + '<a href="https://www.dmm.co.jp/rental/ppr/-/detail/=/cid=4abp123/">Ok</a>',
            )
        if "abp00123" in request.url.path:
            return httpx.Response(200, text="<h1>Unknown template</h1>")
        return httpx.Response(200, text=DETAIL)

    with DmmClient(
        transport=httpx.MockTransport(handle), request_interval_seconds=0
    ) as client:
        result = client.fetch("ABP-123")
    assert result.status == "success"
    assert result.source_id == "4abp123"
