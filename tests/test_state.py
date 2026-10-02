from types import SimpleNamespace

from sakuramedia_movie_scrape_translate.state import DmmState


def result(title="原始标题", desc="原始简介"):
    return SimpleNamespace(
        status="success" if desc else "partial",
        title=title,
        desc=desc,
        source_url="https://www.dmm.co.jp/mono/dvd/-/detail/=/cid=test001/",
        source_id="test001",
    )


def test_partial_result_and_canonical_number_survive_reopening(tmp_path):
    path = tmp_path / "dmm_state.sqlite3"
    DmmState(path).record_fetch_result("n0646", result(desc=""))

    cached = DmmState(path).load("n0646")
    assert cached["movie_number"] == "n0646"
    assert cached["fetch_status"] == "partial"
    assert cached["raw_title"] == "原始标题"
    assert cached["title_zh"] is None
    assert cached["raw_desc"] == ""
    assert cached["desc_zh"] == ""


def test_refetch_only_invalidates_translation_for_changed_source(tmp_path):
    state = DmmState(tmp_path / "dmm_state.sqlite3")
    state.record_fetch_result("ABP-001", result())
    state.save("ABP-001", title_zh="中文标题", desc_zh="中文简介")

    state.record_fetch_result("ABP-001", result())
    assert state.load("ABP-001")["title_zh"] == "中文标题"
    state.record_fetch_result("ABP-001", result(title="新的原始标题"))
    cached = state.load("ABP-001")
    assert cached["title_zh"] is None
    assert cached["title_attempts"] == 0
    assert cached["desc_zh"] == "中文简介"


def test_load_many_handles_missing_numbers_quotes_and_multiple_batches(tmp_path):
    state = DmmState(tmp_path / "dmm_state.sqlite3")
    numbers = [f"ABP-{index}" for index in range(1001)] + ["ABP-'quoted"]
    with state.connect() as db:
        db.executemany(
            "INSERT INTO movie_cache (movie_number, title_zh) VALUES (?, ?)",
            [(number, "" if index % 2 else None) for index, number in enumerate(numbers)],
        )
    assert state.load_many([]) == {}
    cached = state.load_many([*numbers, "missing"])
    assert set(cached) == set(numbers)
    assert cached[numbers[0]]["title_zh"] is None
    assert cached[numbers[1]]["title_zh"] == ""


def test_load_all_projects_only_columns_used_by_pipeline(tmp_path):
    state = DmmState(tmp_path / "dmm_state.sqlite3")
    state.record_fetch_result("ABP-001", result())
    state.save("ABP-001", title_zh="中文标题")
    state.save("ABP-002", fetch_status="not_found")

    cached = state.load_all()

    assert set(cached) == {"ABP-001", "ABP-002"}
    assert cached["ABP-001"]["fetch_status"] == "success"
    assert cached["ABP-001"]["raw_title"] == "原始标题"
    assert cached["ABP-001"]["title_zh"] == "中文标题"
    assert cached["ABP-002"]["fetch_status"] == "not_found"
    assert "source_url" not in cached["ABP-001"]
