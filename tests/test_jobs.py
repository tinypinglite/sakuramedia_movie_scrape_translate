import subprocess
import sys
from datetime import datetime
from types import SimpleNamespace
from typing import ClassVar

import pytest
import sakuramedia_movie_scrape_translate.jobs as jobs_module
from pydantic import ValidationError
from sakuramedia_movie_scrape_translate.dmm import DmmError
from sakuramedia_movie_scrape_translate.jobs import build_jobs
from sakuramedia_movie_scrape_translate.settings import DmmSettings
from sakuramedia_movie_scrape_translate.state import MAX_ATTEMPTS, DmmState
from sakuramedia_movie_scrape_translate.translation import TranslationError


class _Reporter:
    def __init__(self) -> None:
        self.events = []
        self.summary = {}

    def emit(self, **kwargs) -> None:
        self.events.append(kwargs)
        self.summary.update(kwargs.get("summary_patch", {}))


class _Snapshot:
    def __init__(
        self,
        movie_id: int,
        movie_number: str,
        *,
        owners=None,
        subscribed: bool = True,
        release_date=None,
        watched_count: int = 0,
        want_watch_count: int = 0,
        comment_count: int = 0,
        score_number: int = 0,
        actors=(),
    ) -> None:
        self.movie_id = movie_id
        self.revision = 0
        self.values = {
            "movie_number": movie_number,
            "title": "宿主标题",
            "summary": "宿主简介",
            "release_date": release_date,
            "watched_count": watched_count,
            "want_watch_count": want_watch_count,
            "comment_count": comment_count,
            "score_number": score_number,
            "is_subscribed": subscribed,
        }
        self.owners = dict(owners or {})
        self.actors = tuple(actors)


class _ActorSnapshot:
    def __init__(self, *, subscribed: bool, gender: int = 1) -> None:
        self.values = {"is_subscribed": subscribed, "gender": gender}


class _Movies:
    def __init__(self, snapshots) -> None:
        self.snapshots = list(snapshots)
        self.patches = []
        self.fail_patches = 0

    def find_by_numbers(self, numbers):
        requested = {number.upper() for number in numbers}
        return [
            movie for movie in self.snapshots
            if movie.values["movie_number"].upper() in requested
        ]

    def list_page(self, *, after_id, limit):
        assert limit == 500
        remaining = sorted(
            (movie for movie in self.snapshots if movie.movie_id > after_id),
            key=lambda movie: movie.movie_id,
        )
        items = tuple(remaining[:limit])
        return SimpleNamespace(
            items=items,
            next_cursor=items[-1].movie_id if len(remaining) > limit else None,
        )

    def get(self, movie_id):
        return next(
            (item for item in self.snapshots if item.movie_id == movie_id), None
        )

    def patch(self, movie_id, fields, expected_revision):
        snapshot = self.get(movie_id)
        self.patches.append((movie_id, dict(fields), expected_revision))
        if self.fail_patches:
            self.fail_patches -= 1
            snapshot.revision += 1
            return False
        if expected_revision != snapshot.revision:
            return False
        snapshot.values.update(fields)
        snapshot.owners.update(
            {field: "plugin:sakuramedia_movie_scrape_translate" for field in fields}
        )
        snapshot.revision += 1
        return True


class _Context:
    def __init__(self, tmp_path, snapshots) -> None:
        self.data_dir = tmp_path
        self.movies = _Movies(snapshots)


def _dmm_result():
    return SimpleNamespace(
        status="success",
        title="DMM 原始标题",
        desc="DMM 原始简介",
        source_url="https://www.dmm.co.jp/detail/=/cid=test001/",
        source_id="test001",
    )


class _DmmClient:
    calls: ClassVar[list[str]] = []
    result = _dmm_result()

    def __init__(self, *, timeout_seconds, request_interval_seconds) -> None:
        assert timeout_seconds == 20
        assert request_interval_seconds == 1

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def fetch(self, movie_number):
        self.calls.append(movie_number)
        return self.result


class _TranslationClient:
    calls: ClassVar[list[str]] = []

    def __init__(self, _settings) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def translate(self, *, system_prompt, source_text):
        assert system_prompt
        self.calls.append(source_text)
        return f"中文:{source_text}"


class _RateLimitedTranslationClient(_TranslationClient):
    def translate(self, *, system_prompt, source_text):
        del system_prompt, source_text
        raise TranslationError(
            "http_429", "翻译服务返回 HTTP 429", retryable=True
        )


class _UnauthorizedTranslationClient(_TranslationClient):
    def translate(self, *, system_prompt, source_text):
        del system_prompt, source_text
        raise TranslationError(
            "http_401", "翻译服务返回 HTTP 401", retryable=False, abort_batch=True
        )


@pytest.fixture(autouse=True)
def _reset_fake_clients():
    _DmmClient.calls = []
    _DmmClient.result = _dmm_result()
    _TranslationClient.calls = []


def _manual_job(context, settings):
    return build_jobs(context, settings=settings)[0]


def _daily_job(context, settings):
    return build_jobs(context, settings=settings)[1]


def _translate_cached_job(context, settings):
    return build_jobs(context, settings=settings)[2]


def test_manual_raw_writeback_uses_canonical_host_number(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    _DmmClient.calls = []
    snapshot = _Snapshot(1, "n0646")
    context = _Context(tmp_path, [snapshot])

    stats = _manual_job(context, DmmSettings()).handler(
        _Reporter(), {"movie_number": "N0646"}
    )

    assert _DmmClient.calls == ["n0646"]
    assert snapshot.values["title"] == "DMM 原始标题"
    assert snapshot.values["summary"] == "DMM 原始简介"
    assert stats["writeback_applied"] == 2
    cached = DmmState(tmp_path / "dmm_state.sqlite3").load("n0646")
    assert cached["fetch_status"] == "success"
    assert cached["raw_title"] == "DMM 原始标题"


def test_daily_filters_and_prioritizes_movies(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    subscribed_old = _Snapshot(
        1,
        "SUB-OLD",
        release_date=datetime(2018, 1, 1),
        watched_count=1,
    )
    subscribed_new = _Snapshot(
        2,
        "SUB-NEW",
        release_date=datetime(2024, 1, 1),
    )
    actress_old = _Snapshot(
        3,
        "ACTRESS-OLD",
        subscribed=False,
        release_date=datetime(2024, 1, 1),
        actors=(_ActorSnapshot(subscribed=True),),
        watched_count=2,
    )
    actress_new = _Snapshot(
        4,
        "ACTRESS-NEW",
        subscribed=False,
        release_date=datetime(2025, 1, 1),
        actors=(_ActorSnapshot(subscribed=True),),
    )
    regular = _Snapshot(
        5,
        "REGULAR",
        subscribed=False,
        release_date=datetime(2026, 1, 1),
        comment_count=10,
        actors=(_ActorSnapshot(subscribed=True, gender=2),),
    )
    null_date = _Snapshot(
        6,
        "NULL-DATE",
        subscribed=False,
        comment_count=100,
    )
    skipped_number = _Snapshot(
        7,
        "123-456",
        subscribed=False,
        release_date=datetime(2026, 1, 1),
    )
    skipped_number_underscore = _Snapshot(
        8,
        "123_456",
        subscribed=False,
        release_date=datetime(2026, 1, 1),
    )
    skipped_fc2 = _Snapshot(
        9,
        "FC2-123",
        subscribed=False,
        release_date=datetime(2026, 1, 1),
    )
    skipped_old = _Snapshot(
        10,
        "OLD",
        subscribed=False,
        release_date=datetime(2014, 12, 31),
    )
    allowed_fc = _Snapshot(
        11,
        "FC-123",
        subscribed=False,
        release_date=datetime(2026, 1, 1),
    )

    stats = _daily_job(
        _Context(
            tmp_path,
            [
                null_date,
                skipped_old,
                actress_old,
                subscribed_old,
                skipped_number,
                skipped_number_underscore,
                regular,
                allowed_fc,
                skipped_fc2,
                subscribed_new,
                actress_new,
            ],
        ),
        DmmSettings(),
    ).handler(_Reporter(), {})

    assert stats["skipped"] == 4
    assert _DmmClient.calls == [
        "SUB-NEW",
        "SUB-OLD",
        "ACTRESS-NEW",
        "ACTRESS-OLD",
        "REGULAR",
        "FC-123",
        "NULL-DATE",
    ]


def test_explicit_skipped_number_is_intercepted(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    snapshot = _Snapshot(
        1,
        "FC2-001",
        subscribed=True,
        release_date=datetime(2024, 1, 1),
    )

    stats = _manual_job(_Context(tmp_path, [snapshot]), DmmSettings()).handler(
        _Reporter(), {"movie_number": "FC2-001"}
    )

    assert _DmmClient.calls == []
    assert stats["skipped"] == 1
    assert stats["movie_not_found"] == 0
    assert DmmState(tmp_path / "dmm_state.sqlite3").load("FC2-001") is None


def test_cached_translation_retries_only_writeback(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", _TranslationClient)
    _DmmClient.calls = []
    _TranslationClient.calls = []
    snapshot = _Snapshot(1, "ABP-001")
    context = _Context(tmp_path, [snapshot])
    context.movies.fail_patches = 10
    settings = DmmSettings(
        translation_enabled=True,
        base_url="https://translate.example",
        model="test-model",
    )
    job = _manual_job(context, settings)

    reporter = _Reporter()
    first = job.handler(reporter, {"movie_number": "ABP-001"})
    assert first["failed_movies"] == 1
    assert reporter.summary["writeback_failed"] == 2
    assert _DmmClient.calls == ["ABP-001"]
    assert _TranslationClient.calls == ["DMM 原始标题", "DMM 原始简介"]

    context.movies.fail_patches = 0
    second = job.handler(_Reporter(), {"movie_number": "ABP-001"})
    assert second["fetch_cached"] == 1
    assert _DmmClient.calls == ["ABP-001"]
    assert _TranslationClient.calls == ["DMM 原始标题", "DMM 原始简介"]
    assert snapshot.values["title"] == "中文:DMM 原始标题"
    assert snapshot.values["summary"] == "中文:DMM 原始简介"


def test_manual_run_retries_exhausted_translation(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", _TranslationClient)
    snapshot = _Snapshot(1, "ABP-001")
    context = _Context(tmp_path, [snapshot])
    state = DmmState(tmp_path / "dmm_state.sqlite3")
    state.record_fetch_result("ABP-001", _dmm_result())
    state.save(
        "ABP-001",
        title_attempts=MAX_ATTEMPTS,
        desc_attempts=MAX_ATTEMPTS,
        title_error="翻译服务返回 HTTP 400",
        desc_error="翻译服务返回 HTTP 400",
    )
    settings = DmmSettings(
        translation_enabled=True,
        base_url="https://translate.example",
        model="test-model",
    )

    stats = _manual_job(context, settings).handler(
        _Reporter(), {"movie_number": "ABP-001"}
    )

    assert _TranslationClient.calls == ["DMM 原始标题", "DMM 原始简介"]
    assert stats["translation_exhausted"] == 0
    cached = state.load("ABP-001")
    assert cached["title_attempts"] == 0
    assert cached["desc_attempts"] == 0


def test_manual_or_other_owner_is_checked_before_translation(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", _TranslationClient)
    _DmmClient.calls = []
    _TranslationClient.calls = []
    snapshot = _Snapshot(
        1,
        "ABP-001",
        owners={"title": "host:manual", "summary": "plugin:other"},
    )
    context = _Context(tmp_path, [snapshot])
    settings = DmmSettings(
        translation_enabled=True,
        base_url="https://translate.example",
        model="test-model",
    )

    stats = _manual_job(context, settings).handler(
        _Reporter(), {"movie_number": "ABP-001"}
    )

    assert stats["writeback_blocked"] == 2
    assert _DmmClient.calls == []
    assert _TranslationClient.calls == []
    assert context.movies.patches == []


def test_rate_limit_continues_to_second_movie(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    monkeypatch.setattr(
        jobs_module, "OpenAITranslationClient", _RateLimitedTranslationClient
    )
    _DmmClient.calls = []
    first = _Snapshot(1, "ABP-001")
    second = _Snapshot(2, "ABP-002")
    context = _Context(tmp_path, [first, second])
    settings = DmmSettings(
        translation_enabled=True,
        base_url="https://translate.example",
        model="test-model",
    )

    reporter = _Reporter()
    result = _daily_job(context, settings).handler(reporter, {})

    assert result["aborted"] == 0
    assert reporter.summary["failed_movies"] == 2
    translation_end = [e for e in reporter.events if e["text"].startswith("阶段 3/4")][-1]
    assert translation_end["current"] == translation_end["total"] == 4
    assert reporter.events[-1]["current"] == reporter.events[-1]["total"] == 0
    assert reporter.events[-1]["text"].startswith("抓取翻译完成")
    assert _DmmClient.calls == ["ABP-001", "ABP-002"]
    assert DmmState(tmp_path / "dmm_state.sqlite3").load("ABP-002")["title_attempts"] == 1


def test_not_found_is_cached(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    _DmmClient.result = SimpleNamespace(
        status="not_found", title="", desc="", source_url="", source_id=""
    )
    first_context = _Context(tmp_path, [_Snapshot(1, "ABP-001")])
    _manual_job(first_context, DmmSettings()).handler(
        _Reporter(), {"movie_number": "ABP-001"}
    )
    assert _DmmClient.calls == ["ABP-001"]

    fresh_context = _Context(tmp_path, [_Snapshot(1, "ABP-001")])
    cached = _manual_job(fresh_context, DmmSettings()).handler(
        _Reporter(), {"movie_number": "ABP-001"}
    )
    assert cached["fetch_cached"] == 1
    assert _DmmClient.calls == ["ABP-001"]


def test_config_failure_stays_pending_and_corrected_resume_uses_cache(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    monkeypatch.setattr(
        jobs_module, "OpenAITranslationClient", _UnauthorizedTranslationClient
    )
    snapshot = _Snapshot(1, "ABP-001")
    context = _Context(tmp_path, [snapshot])
    settings = DmmSettings(
        translation_enabled=True,
        base_url="https://translate.example",
        model="test-model",
    )
    job = _manual_job(context, settings)

    reporter = _Reporter()
    with pytest.raises(RuntimeError, match="HTTP 401"):
        job.handler(reporter, {"movie_number": "ABP-001"})
    cached = DmmState(tmp_path / "dmm_state.sqlite3").load("ABP-001")
    assert reporter.summary["aborted"] == 1
    assert cached["title_zh"] is None
    assert cached["title_attempts"] == 0
    assert _DmmClient.calls == ["ABP-001"]

    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", _TranslationClient)
    second = job.handler(_Reporter(), {"movie_number": "ABP-001"})
    assert second["fetch_cached"] == 1
    assert _DmmClient.calls == ["ABP-001"]
    assert _TranslationClient.calls == ["DMM 原始标题", "DMM 原始简介"]


def test_translation_uses_cached_original_without_constructing_dmm(
    monkeypatch, tmp_path
) -> None:
    class _UnexpectedDmmClient:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("已有缓存不应创建 DMM 客户端")

    monkeypatch.setattr(jobs_module, "DmmClient", _UnexpectedDmmClient)
    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", _TranslationClient)
    snapshot = _Snapshot(1, "ABP-001")
    context = _Context(tmp_path, [snapshot])
    DmmState(tmp_path / "dmm_state.sqlite3").record_fetch_result(
        "ABP-001", _dmm_result()
    )
    settings = DmmSettings(
        translation_enabled=True,
        base_url="https://translate.example",
        model="test-model",
    )

    _manual_job(context, settings).handler(
        _Reporter(), {"movie_number": "ABP-001"}
    )

    assert _TranslationClient.calls == ["DMM 原始标题", "DMM 原始简介"]
    assert snapshot.values["title"] == "中文:DMM 原始标题"


def test_writeback_crash_recovers_without_retranslating_or_repatching_value(
    monkeypatch, tmp_path
) -> None:
    class _CrashAfterPatch(RuntimeError):
        pass

    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", _TranslationClient)
    snapshot = _Snapshot(1, "ABP-001")
    context = _Context(tmp_path, [snapshot])
    settings = DmmSettings(
        translation_enabled=True,
        base_url="https://translate.example",
        model="test-model",
    )
    job = _manual_job(context, settings)
    original_patch = context.movies.patch

    def _crash_after_patch(*args, **kwargs):
        original_patch(*args, **kwargs)
        raise _CrashAfterPatch("simulated crash")

    monkeypatch.setattr(context.movies, "patch", _crash_after_patch)
    with pytest.raises(_CrashAfterPatch, match="simulated crash"):
        job.handler(_Reporter(), {"movie_number": "ABP-001"})
    monkeypatch.setattr(context.movies, "patch", original_patch)

    assert [fields for _, fields, _ in context.movies.patches] == [
        {"title": "中文:DMM 原始标题"}
    ]
    job.handler(_Reporter(), {"movie_number": "ABP-001"})

    assert _DmmClient.calls == ["ABP-001"]
    assert _TranslationClient.calls.count("DMM 原始标题") == 1
    assert [fields for _, fields, _ in context.movies.patches] == [
        {"title": "中文:DMM 原始标题"},
        {"summary": "中文:DMM 原始简介"},
    ]


def test_pipeline_lock_blocks_another_process_and_releases_on_exit(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    job = _manual_job(_Context(tmp_path, [_Snapshot(1, "ABP-001")]), DmmSettings())
    lock_path = tmp_path / "dmm_pipeline.lock"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import portalocker, sys\n"
                "lock = portalocker.Lock(sys.argv[1], mode='a+', timeout=0)\n"
                "lock.acquire()\n"
                "print('locked', flush=True)\n"
                "sys.stdin.readline()\n"
                "lock.release()\n"
            ),
            str(lock_path),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        reporter = _Reporter()
        with pytest.raises(RuntimeError, match="未执行"):
            job.handler(reporter, {"movie_number": "ABP-001"})
        assert reporter.summary["busy"] == 1
        assert _DmmClient.calls == []
        assert holder.stdin is not None
        holder.stdin.write("\n")
        holder.stdin.flush()
        assert holder.wait(timeout=5) == 0
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=5)

    assert job.handler(_Reporter(), {"movie_number": "ABP-001"})["fetched"] == 1


def test_fetch_retries_once_per_run_and_stops_at_limit(monkeypatch, tmp_path):
    class FailedDmm(_DmmClient):
        def fetch(self, movie_number):
            self.calls.append(movie_number)
            raise DmmError("search_parse_error", "无法解析页面")

    monkeypatch.setattr(jobs_module, "DmmClient", FailedDmm)
    job = _daily_job(_Context(tmp_path, [_Snapshot(1, "ABP-001")]), DmmSettings())
    for _ in range(MAX_ATTEMPTS):
        reporter = _Reporter()
        result = job.handler(reporter, {})
        assert result["fetch_failed"] == 1
    reporter = _Reporter()
    stats = job.handler(reporter, {})
    assert stats["fetch_exhausted"] == stats["no_work"] == 1
    assert reporter.events[-1]["current"] == reporter.events[-1]["total"] == 0
    assert len(FailedDmm.calls) == MAX_ATTEMPTS
    cached = DmmState(tmp_path / "dmm_state.sqlite3").load("ABP-001")
    assert cached["fetch_status"] == "error"
    assert cached["fetch_error"] == "无法解析页面"


def test_daily_exhausted_fetch_is_skipped_without_failing(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    job = _daily_job(_Context(tmp_path, [_Snapshot(1, "ABP-001")]), DmmSettings())
    DmmState(tmp_path / "dmm_state.sqlite3").save(
        "ABP-001", fetch_status="error", fetch_attempts=MAX_ATTEMPTS,
        fetch_error="DMM 搜索结果无法精确匹配番号，未标记为不存在",
    )

    reporter = _Reporter()
    stats = job.handler(reporter, {})

    assert stats["fetch_exhausted"] == stats["no_work"] == 1
    assert stats["failed_movies"] == 0
    assert stats["pending_fetch"] == 0
    assert DmmState(tmp_path / "dmm_state.sqlite3").load("ABP-001")["fetch_attempts"] == MAX_ATTEMPTS
    assert reporter.events[-1]["text"].startswith("抓取翻译完成")


def test_manual_number_retries_exhausted_fetch(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    snapshot = _Snapshot(1, "ABP-001")
    context = _Context(tmp_path, [snapshot])
    DmmState(tmp_path / "dmm_state.sqlite3").save(
        "ABP-001", fetch_status="error", fetch_attempts=MAX_ATTEMPTS,
        fetch_error="DMM 搜索结果无法精确匹配番号，未标记为不存在",
    )

    stats = _manual_job(context, DmmSettings()).handler(
        _Reporter(), {"movie_number": "ABP-001"}
    )

    assert stats["fetch_exhausted"] == 0
    assert stats["fetched"] == 1
    assert snapshot.values["title"] == "DMM 原始标题"


def test_no_content_is_cached_without_clearing_host_text(monkeypatch, tmp_path):
    class NoContent(_TranslationClient):
        def translate(self, *, system_prompt, source_text):
            self.calls.append(source_text)
            return "无有效内容"

    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", NoContent)
    snapshot = _Snapshot(1, "ABP-001")
    job = _manual_job(
        _Context(tmp_path, [snapshot]),
        DmmSettings(
            translation_enabled=True,
            base_url="https://translate.example",
            model="test-model",
        ),
    )
    job.handler(_Reporter(), {"movie_number": "ABP-001"})
    job.handler(_Reporter(), {"movie_number": "ABP-001"})
    assert len(NoContent.calls) == 2
    assert snapshot.values["title"] == "宿主标题"
    assert snapshot.values["summary"] == "宿主简介"


@pytest.mark.parametrize("params", [
    {}, {"movie_number": ""}, {"movie_number": "   "},
    {"movie_number": "ABP-001", "scope": "all"},
    {"movie_number": "ABP-001", "mode": "refetch"},
])
def test_manual_rejects_missing_number_and_extra_params(tmp_path, params) -> None:
    job = _manual_job(_Context(tmp_path, []), DmmSettings())
    with pytest.raises(ValidationError):
        job.handler(_Reporter(), params)


def test_manual_only_processes_requested_movie(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    context = _Context(tmp_path, [_Snapshot(1, "ABP-001"), _Snapshot(2, "ABP-002")])
    job = _manual_job(context, DmmSettings())
    schema = job.params_schema.model_json_schema()
    assert list(schema["properties"]) == ["movie_number"]
    assert schema["required"] == ["movie_number"]
    stats = job.handler(_Reporter(), {"movie_number": " ABP-002 "})
    assert stats["movies"] == 1
    assert _DmmClient.calls == ["ABP-002"]


def test_progress_precedes_requests_and_counts_cached_skipped_movies(
    monkeypatch, tmp_path
):
    reporter = _Reporter()
    messages = []

    class Dmm(_DmmClient):
        def fetch(self, number):
            assert "正在抓取 DMM" in reporter.events[-1]["text"]
            assert number in reporter.events[-1]["text"]
            assert reporter.events[-1]["total"] == 1
            return super().fetch(number)

    class Translator(_TranslationClient):
        def translate(self, **kwargs):
            assert "正在翻译" in reporter.events[-1]["text"]
            assert reporter.events[-1]["current"] == len(self.calls)
            assert reporter.events[-1]["total"] == 2
            return super().translate(**kwargs)

    monkeypatch.setattr(jobs_module, "DmmClient", Dmm)
    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", Translator)
    state = DmmState(tmp_path / "dmm_state.sqlite3")
    state.save("ABP-002", fetch_status="not_found")
    state.record_fetch_result("ABP-003", _dmm_result())
    snapshots = [
        _Snapshot(1, "ABP-001"),
        _Snapshot(2, "ABP-002"),
        _Snapshot(
            3, "ABP-003", owners={"title": "host:manual", "summary": "host:manual"}
        ),
        _Snapshot(4, "FC2-001"),
    ]
    sink = jobs_module.logger.add(lambda msg: messages.append(str(msg)))
    try:
        result = _daily_job(
            _Context(tmp_path, snapshots),
            DmmSettings(
                translation_enabled=True,
                base_url="https://translate.example",
                model="test",
            ),
        ).handler(reporter, {})
    finally:
        jobs_module.logger.remove(sink)
    assert result["movies"] == 3
    assert result["skipped"] == result["not_found"] == 1
    assert result["writeback_blocked"] == 2
    assert reporter.events[-1]["current"] == reporter.events[-1]["total"] == 2
    assert "翻译 2 个字段" in reporter.events[-1]["text"]
    assert "写回 2 个字段" in reporter.events[-1]["text"]
    assert any("DMM 未找到" in msg for msg in messages)
    assert any("受保护 2 个字段" in msg for msg in messages)
    assert result["no_work"] == 2
    for stage, total in ((1, 4), (2, 1), (3, 2), (4, 2)):
        events = [e for e in reporter.events if e["text"].startswith(f"阶段 {stage}/4")]
        assert events[0]["current"] == 0
        assert events[0]["total"] == (0 if stage == 1 else total)
        assert events[-1]["current"] == events[-1]["total"] == total
        assert [e["current"] for e in events] == sorted(e["current"] for e in events)
        assert all(len(e["text"]) <= 255 for e in events)


def test_local_failure_continues_and_preserves_success(monkeypatch, tmp_path):
    class Dmm(_DmmClient):
        def fetch(self, number):
            if number == "ABP-001":
                raise DmmError("search_parse_error", "无法解析页面")
            return super().fetch(number)

    monkeypatch.setattr(jobs_module, "DmmClient", Dmm)
    reporter = _Reporter()
    context = _Context(tmp_path, [_Snapshot(1, "ABP-001"), _Snapshot(2, "ABP-002")])
    result = _daily_job(context, DmmSettings()).handler(reporter, {})
    assert result["failed_movies"] == 1
    assert result["movies"] == 2
    assert result["writeback_applied"] == 2
    assert reporter.events[-1]["current"] == reporter.events[-1]["total"] == 2
    assert "失败 1 部" in reporter.events[-1]["text"]


def test_progress_throttles_and_forces_final_update(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(jobs_module.time, "monotonic", lambda: now[0])
    reporter = _Reporter()
    progress = jobs_module._Progress(reporter, {})
    progress.emit("正在读取影片")
    for value in range(1, 4):
        now[0] = value / 10
        progress.emit("正在读取影片", detail=str(value))
    assert len(reporter.events) == 1
    now[0] = 2
    progress.emit("正在读取影片", detail="第 5 页")
    assert len(reporter.events) == 2
    progress.total = progress.current = 10
    progress.emit("完成", force=True)
    assert reporter.events[-1]["current"] == reporter.events[-1]["total"] == 10


def test_dmm_http_failure_continues_and_releases_lock(monkeypatch, tmp_path):
    class Dmm(_DmmClient):
        def fetch(self, number):
            if number == "ABP-001":
                raise DmmError("http_403", "DMM 返回 HTTP 403")
            return super().fetch(number)

    monkeypatch.setattr(jobs_module, "DmmClient", Dmm)
    reporter = _Reporter()
    job = _daily_job(_Context(tmp_path, [_Snapshot(1, "ABP-001"), _Snapshot(2, "ABP-002")]), DmmSettings())
    result = job.handler(reporter, {})
    assert result["aborted"] == 0
    assert result["movies"] == 2
    assert result["writeback_applied"] == 2
    assert result["fetch_failed"] == 1
    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    assert job.handler(_Reporter(), {})["writeback_applied"] == 2


def test_three_429_stop_batch_and_next_run_starts_fresh(monkeypatch, tmp_path):
    import httpx
    from sakuramedia_movie_scrape_translate.translation import OpenAITranslationClient

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(429)

    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    monkeypatch.setattr(
        jobs_module, "OpenAITranslationClient",
        lambda settings: OpenAITranslationClient(
            settings, transport=httpx.MockTransport(respond)
        ),
    )
    _DmmClient.calls = []
    context = _Context(tmp_path, [
        _Snapshot(1, "ABP-001"), _Snapshot(2, "ABP-002"), _Snapshot(3, "ABP-003")
    ])
    settings = DmmSettings(
        translation_enabled=True, base_url="https://translate.example", model="test-model"
    )
    for run in range(2):
        reporter = _Reporter()
        with pytest.raises(RuntimeError, match="连续 3 次返回 HTTP 429"):
            _daily_job(context, settings).handler(reporter, {})
        assert len(requests) == (run + 1) * 3
        assert reporter.summary["aborted"] == 1
        assert reporter.summary["movies"] == 3
        assert reporter.summary["failed_movies"] == 2
        assert reporter.events[-1]["current"] == 3
        assert reporter.events[-1]["total"] == 6
        assert reporter.summary["pending_translation"] == 3
        state = DmmState(tmp_path / "dmm_state.sqlite3")
        assert state.load("ABP-002")["desc_attempts"] == 0
        assert _DmmClient.calls == ["ABP-001", "ABP-002", "ABP-003"]
        assert context.movies.patches == []


def test_cached_movies_are_loaded_once_without_individual_reads(monkeypatch, tmp_path):
    snapshots = [_Snapshot(index, f"ABP-{index:04}") for index in range(1, 1002)]
    context = _Context(tmp_path, snapshots)
    state = DmmState(tmp_path / "dmm_state.sqlite3")
    rows = []
    for index, snapshot in enumerate(snapshots):
        snapshot.values["title"] = "中文标题"
        rows.append((
            snapshot.values["movie_number"],
            "not_found" if index % 4 == 0 else "success",
            "原文标题", "中文标题",
            "原文简介" if index % 2 else "",
            "" if index % 2 else None,
        ))
    with state.connect() as db:
        db.executemany(
            "INSERT INTO movie_cache "
            "(movie_number, fetch_status, raw_title, title_zh, raw_desc, desc_zh) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            rows,
        )

    def unexpected(*_args, **_kwargs):
        pytest.fail("全量扫描不应按页/逐片查库或创建网络客户端")

    load_all_calls = []
    original_load_all = DmmState.load_all

    def load_all(self):
        load_all_calls.append(1)
        return original_load_all(self)

    monkeypatch.setattr(DmmState, "load_all", load_all)
    monkeypatch.setattr(DmmState, "load_many", unexpected)
    monkeypatch.setattr(DmmState, "load", unexpected)
    monkeypatch.setattr(context.movies, "get", unexpected)
    monkeypatch.setattr(jobs_module, "DmmClient", unexpected)
    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", unexpected)
    reporter = _Reporter()
    messages = []
    sink = jobs_module.logger.add(lambda message: messages.append(str(message)))
    try:
        stats = _daily_job(context, DmmSettings(
            translation_enabled=True, base_url="https://translate.example", model="test",
        )).handler(reporter, {})
    finally:
        jobs_module.logger.remove(sink)

    assert load_all_calls == [1]
    assert stats["movies"] == stats["no_work"] == stats["fetch_cached"] == 1001
    assert stats["not_found"] == 251
    assert stats["translated"] == stats["writeback_applied"] == 0
    assert context.movies.patches == []
    assert not any("使用 DMM 抓取缓存" in message or "跳过" in message for message in messages)
    assert len(messages) < 20
    screening = [e for e in reporter.events if e["text"].startswith("阶段 1/4")]
    assert screening[-1]["current"] == screening[-1]["total"] == 1001
    assert [e["current"] for e in screening] == sorted(e["current"] for e in screening)


@pytest.mark.parametrize("translation_enabled", [False, True])
def test_second_run_has_no_field_work(monkeypatch, tmp_path, translation_enabled):
    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", _TranslationClient)
    context = _Context(tmp_path, [_Snapshot(1, "ABP-001"), _Snapshot(2, "ABP-002")])
    job = _daily_job(context, DmmSettings(
        translation_enabled=translation_enabled,
        base_url="https://translate.example", model="test",
    ))
    job.handler(_Reporter(), {})

    def unexpected(*_args, **_kwargs):
        pytest.fail("第二轮无需逐片读取或请求")

    monkeypatch.setattr(context.movies, "get", unexpected)
    monkeypatch.setattr(DmmState, "load", unexpected)
    monkeypatch.setattr(jobs_module, "DmmClient", unexpected)
    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", unexpected)
    result = job.handler(_Reporter(), {})
    assert result["no_work"] == 2
    assert result["fetched"] == result["translated"] == result["writeback_applied"] == 0


def test_mixed_work_runs_in_stages_and_preserves_priority(monkeypatch, tmp_path):
    operations = []

    class Dmm(_DmmClient):
        def fetch(self, number):
            operations.append(("fetch", number))
            return super().fetch(number)

    class Translator(_TranslationClient):
        def translate(self, **kwargs):
            operations.append(("translate", kwargs["source_text"]))
            return super().translate(**kwargs)

    monkeypatch.setattr(jobs_module, "DmmClient", Dmm)
    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", Translator)
    snapshots = [_Snapshot(index, f"ABP-{index:03}") for index in range(1, 4)]
    context = _Context(tmp_path, snapshots)
    original_patch = context.movies.patch

    def patch(movie_id, fields, expected_revision):
        operations.append(("write", movie_id))
        return original_patch(movie_id, fields, expected_revision)

    monkeypatch.setattr(context.movies, "patch", patch)
    state = DmmState(tmp_path / "dmm_state.sqlite3")
    state.record_fetch_result("ABP-002", _dmm_result())
    state.save("ABP-002", title_zh="宿主标题", raw_desc="待翻译简介")
    state.record_fetch_result("ABP-003", _dmm_result())
    state.save("ABP-003", title_zh="缓存中文标题", desc_zh="")
    reporter = _Reporter()
    result = _daily_job(context, DmmSettings(
        translation_enabled=True, base_url="https://translate.example", model="test",
    )).handler(reporter, {})

    assert [operation for operation, _ in operations] == [
        "fetch", "translate", "translate", "translate", "write", "write", "write", "write",
    ]
    assert _TranslationClient.calls == ["DMM 原始标题", "DMM 原始简介", "待翻译简介"]
    assert [movie_id for operation, movie_id in operations if operation == "write"] == [1, 1, 2, 3]
    assert not any(movie_id == 2 and "title" in fields for movie_id, fields, _ in context.movies.patches)
    planned = [e for e in reporter.events if "筛选完成" in e["text"]][0]["summary_patch"]
    assert planned["pending_fetch"] == planned["pending_translation"] == planned["pending_writeback"] == 1
    assert result["translated"] == 3
    assert result["writeback_applied"] == 4
    assert result["pending_fetch"] == result["pending_translation"] == result["pending_writeback"] == 0


@pytest.mark.parametrize("change", ["protected", "same_value", "deleted"])
def test_cached_write_rechecks_host_after_fetch_stage(monkeypatch, tmp_path, change):
    first, second = _Snapshot(1, "ABP-001"), _Snapshot(2, "ABP-002")
    context = _Context(tmp_path, [first, second])
    state = DmmState(tmp_path / "dmm_state.sqlite3")
    state.record_fetch_result("ABP-002", _dmm_result())
    state.save("ABP-002", raw_desc="")

    class Dmm(_DmmClient):
        def fetch(self, number):
            if change == "protected":
                second.values["title"] = "人工标题"
                second.owners["title"] = "host:manual"
            elif change == "same_value":
                second.values["title"] = "DMM 原始标题"
            else:
                context.movies.snapshots.remove(second)
            second.revision += 1
            return super().fetch(number)

    monkeypatch.setattr(jobs_module, "DmmClient", Dmm)
    result = _daily_job(context, DmmSettings()).handler(_Reporter(), {})
    assert all(movie_id == 1 for movie_id, _, _ in context.movies.patches)
    assert result["writeback_blocked"] == (0 if change == "same_value" else 1)


def test_translation_does_not_overwrite_field_taken_over_during_request(monkeypatch, tmp_path):
    snapshot = _Snapshot(1, "ABP-001")
    context = _Context(tmp_path, [snapshot])

    class Translator(_TranslationClient):
        def translate(self, **kwargs):
            snapshot.values["title"] = "人工标题"
            snapshot.owners["title"] = "host:manual"
            return super().translate(**kwargs)

    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", Translator)
    result = _daily_job(context, DmmSettings(
        translation_enabled=True, base_url="https://translate.example", model="test",
    )).handler(_Reporter(), {})
    assert result["writeback_blocked"] == 1
    assert snapshot.values["title"] == "人工标题"
    assert [fields for _, fields, _ in context.movies.patches] == [{"summary": "中文:DMM 原始简介"}]


def test_daily_retry_only_processes_failed_field(monkeypatch, tmp_path):
    class Translator(_TranslationClient):
        def translate(self, **kwargs):
            if kwargs["source_text"] == "DMM 原始标题" and not self.calls:
                self.calls.append(kwargs["source_text"])
                raise TranslationError("http_429", "限流", retryable=True)
            return super().translate(**kwargs)

    monkeypatch.setattr(jobs_module, "DmmClient", _DmmClient)
    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", Translator)
    context = _Context(tmp_path, [_Snapshot(1, "ABP-001")])
    job = _daily_job(context, DmmSettings(
        translation_enabled=True, base_url="https://translate.example", model="test",
    ))
    first = job.handler(_Reporter(), {})
    assert first["failed_movies"] == 1
    assert [fields for _, fields, _ in context.movies.patches] == [{"summary": "中文:DMM 原始简介"}]
    result = job.handler(_Reporter(), {})
    assert _DmmClient.calls == ["ABP-001"]
    assert _TranslationClient.calls == ["DMM 原始标题", "DMM 原始简介", "DMM 原始标题"]
    assert result["translated"] == result["writeback_applied"] == 1


def test_translate_cached_uses_cache_without_constructing_dmm(monkeypatch, tmp_path):
    class _UnexpectedDmmClient:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("仅翻译任务不应创建 DMM 客户端")

    monkeypatch.setattr(jobs_module, "DmmClient", _UnexpectedDmmClient)
    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", _TranslationClient)
    snapshot = _Snapshot(1, "ABP-001")
    context = _Context(tmp_path, [snapshot])
    DmmState(tmp_path / "dmm_state.sqlite3").record_fetch_result(
        "ABP-001", _dmm_result()
    )
    settings = DmmSettings(
        translation_enabled=True,
        base_url="https://translate.example",
        model="test-model",
    )
    reporter = _Reporter()

    stats = _translate_cached_job(context, settings).handler(reporter, {})

    assert _TranslationClient.calls == ["DMM 原始标题", "DMM 原始简介"]
    assert snapshot.values["title"] == "中文:DMM 原始标题"
    assert snapshot.values["summary"] == "中文:DMM 原始简介"
    assert stats["translated"] == stats["writeback_applied"] == 2
    assert any(e["text"].startswith("阶段 1/3") for e in reporter.events)
    assert any(e["text"].startswith("阶段 2/3 · 翻译文案") for e in reporter.events)
    assert any(e["text"].startswith("阶段 3/3 · 写回影片") for e in reporter.events)
    assert not any("抓取 DMM 文案" in e["text"] for e in reporter.events)


def test_translate_cached_skips_movies_without_cache(monkeypatch, tmp_path):
    class _UnexpectedDmmClient:
        def __init__(self, **_kwargs) -> None:
            raise AssertionError("仅翻译任务不应创建 DMM 客户端")

    monkeypatch.setattr(jobs_module, "DmmClient", _UnexpectedDmmClient)
    monkeypatch.setattr(jobs_module, "OpenAITranslationClient", _TranslationClient)
    uncached = _Snapshot(1, "ABP-001")
    cached = _Snapshot(2, "ABP-002")
    context = _Context(tmp_path, [uncached, cached])
    DmmState(tmp_path / "dmm_state.sqlite3").record_fetch_result(
        "ABP-002", _dmm_result()
    )
    settings = DmmSettings(
        translation_enabled=True,
        base_url="https://translate.example",
        model="test-model",
    )

    stats = _translate_cached_job(context, settings).handler(_Reporter(), {})

    assert stats["movies"] == 2
    assert stats["no_work"] == 1
    assert stats["translated"] == 2
    assert uncached.values["title"] == "宿主标题"
    assert cached.values["title"] == "中文:DMM 原始标题"
    assert context.movies.patches == [
        (2, {"title": "中文:DMM 原始标题"}, 0),
        (2, {"summary": "中文:DMM 原始简介"}, 1),
    ]


def test_translate_cached_requires_translation_enabled(tmp_path):
    job = _translate_cached_job(_Context(tmp_path, []), DmmSettings())

    with pytest.raises(RuntimeError, match="需要启用翻译"):
        job.handler(_Reporter(), {})
