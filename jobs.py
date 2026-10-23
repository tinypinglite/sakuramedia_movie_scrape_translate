"""读取缓存、抓取、翻译，再通过宿主接口写回。"""

import re
import time
from contextlib import ExitStack
from dataclasses import dataclass

import portalocker
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field
from src.scheduler.contracts import JobDefinition

from .dmm import DmmClient, DmmError
from .state import MAX_ATTEMPTS, DmmState
from .translation import (
    OpenAITranslationClient,
    TranslationError,
    load_prompt,
    normalize_translation,
)

PLUGIN_OWNER = "plugin:sakuramedia_movie_scrape_translate"
_FAILURE_COUNTS = (
    "fetch_failed",
    "fetch_exhausted",
    "translation_failed",
    "translation_exhausted",
    "writeback_failed",
)
MIN_RELEASE_YEAR = 2015
FEMALE_GENDER = 1
INTERACTION_FIELDS = (
    "watched_count",
    "want_watch_count",
    "comment_count",
    "score_number",
)
SKIP_NUMBER_PATTERNS = (
    re.compile(r"^\d+[-_]\d+$"),
    re.compile(r"^FC2-", re.IGNORECASE),
)


@dataclass(frozen=True, slots=True)
class _MovieRef:
    movie_id: int
    movie_number: str
    values: dict
    owners: dict


class DmmSyncParams(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    movie_number: str = Field(min_length=1, max_length=128)


class _Progress:
    def __init__(self, reporter, stats):
        self.reporter, self.stats = reporter, stats
        self.current = self.total = 0
        self.number = self.stage = ""
        self.last_report = self.last_log = float("-inf")
        self.started = time.monotonic()

    def start(self, stage, total):
        self.current, self.total = 0, total
        self.number = ""
        self.emit(stage, force=True)

    def emit(self, stage, *, detail="", force=False, report=False):
        force = force or stage != self.stage
        self.stage = stage
        text = stage + " · "
        text += (
            f"已完成 {self.current}/{self.total} 项 · "
            if self.total else f"已检查 {self.current} 项 · "
        )
        text += f"{self.number} · " if self.number else ""
        text += detail
        now = time.monotonic()
        if force or now - self.last_log >= 10:
            self.log(text)
            self.last_log = now
        if force or report or now - self.last_report >= 2:
            self.reporter.emit(
                current=self.current,
                total=self.total,
                text=text[:255],
                summary_patch=dict(self.stats),
            )
            self.last_report = now

    def log(self, message, *, warning=False):
        log = logger.warning if warning else logger.info
        log(
            "影片抓取翻译 task_run_id={} movie_number={} elapsed_seconds={:.2f} {}",
            getattr(self.reporter, "task_run_id", None),
            self.number,
            time.monotonic() - self.started,
            message,
        )


def _result_text(stats):
    return (
        f"检查 {stats['movies']} 部，无需执行 {stats['no_work']} 部，失败 {stats['failed_movies']} 部，"
        f"筛选排除 {stats['skipped']} 部，DMM 未找到 {stats['not_found']} 部，"
        f"翻译 {stats['translated']} 个字段，写回 {stats['writeback_applied']} 个字段，"
        f"受保护 {stats['writeback_blocked']} 个字段，"
        f"重试耗尽 {stats['fetch_exhausted']} 部/{stats['translation_exhausted']} 个字段，"
        f"待抓取 {stats['pending_fetch']} 部，待翻译 {stats['pending_translation']} 个字段，"
        f"待写回 {stats['pending_writeback']} 个字段"
    )


def _release_year(movie):
    release_date = movie.values.get("release_date")
    return release_date.year if release_date is not None else None


def _should_skip(movie):
    number = str(movie.values["movie_number"]).strip()
    if any(pattern.match(number) for pattern in SKIP_NUMBER_PATTERNS):
        return True
    year = _release_year(movie)
    return year is not None and year < MIN_RELEASE_YEAR


def _has_subscribed_actress(movie):
    return any(
        actor.values.get("gender") == FEMALE_GENDER
        and actor.values.get("is_subscribed")
        for actor in getattr(movie, "actors", ())
    )


def _interaction_heat(movie):
    return sum(int(movie.values.get(field) or 0) for field in INTERACTION_FIELDS)


def _priority_key(movie):
    if movie.values.get("is_subscribed"):
        tier = 0
    elif _has_subscribed_actress(movie):
        tier = 1
    else:
        tier = 2
    year = _release_year(movie)
    return (
        tier,
        -(year if year is not None else 0),
        -_interaction_heat(movie),
        movie.movie_id,
    )


def _movie_ref(movie):
    return _MovieRef(
        movie_id=movie.movie_id,
        movie_number=movie.values["movie_number"],
        values={field: movie.values[field] for field in ("title", "summary")},
        owners=dict(movie.owners),
    )


def _movie_pages(context, movie_number):
    if movie_number:
        yield context.movies.find_by_numbers([movie_number])
        return
    cursor = 0
    while cursor is not None:
        page = context.movies.list_page(after_id=cursor, limit=500)
        yield page.items
        cursor = page.next_cursor


def _writable(context, movie_id, field):
    movie = context.movies.get(movie_id)
    if movie is None or movie.owners.get(field) not in (None, PLUGIN_OWNER):
        return None
    return movie


def _fetch(state, item, dmm, stats, progress):
    movie, attempts = item
    number = movie.movie_number
    progress.emit(progress.stage, detail="正在抓取 DMM 文案，等待响应", report=True)
    started = time.monotonic()
    try:
        state.record_fetch_result(number, dmm.fetch(number))
    except DmmError as exc:
        attempts = attempts + 1 if exc.retryable else MAX_ATTEMPTS
        state.save(
            number,
            fetch_status="error",
            fetch_attempts=attempts,
            fetch_error=str(exc),
        )
        stats["fetch_failed"] += 1
        progress.log(
            f"DMM 抓取失败 code={exc.code} attempts={attempts} reason={exc}",
            warning=True,
        )
        return None
    stats["fetched"] += 1
    progress.log(f"DMM 抓取完成 request_seconds={time.monotonic() - started:.2f}")
    return state.load(number)


def _translate(context, state, item, translator, prompts, stats, progress):
    movie, field, source, attempts = item
    number = movie.movie_number
    host_field = "title" if field == "title" else "summary"
    label = "标题" if field == "title" else "简介"
    latest = _writable(context, movie.movie_id, host_field)
    if latest is None:
        stats["writeback_blocked"] += 1
        return None
    progress.emit(progress.stage, detail=f"正在翻译{label}，等待响应", report=True)
    started = time.monotonic()
    try:
        value = normalize_translation(
            translator.translate(
                system_prompt=prompts[host_field],
                source_text=source,
            )
        )
    except TranslationError as exc:
        if exc.retryable:
            attempts += 1
        elif not exc.abort_batch:
            attempts = MAX_ATTEMPTS
        state.save(
            number,
            **{f"{field}_attempts": attempts, f"{field}_error": str(exc)},
        )
        stats["translation_failed"] += 1
        progress.log(
            f"{label}翻译失败 code={exc.code} attempts={attempts} reason={exc}",
            warning=True,
        )
        if exc.abort_batch:
            raise
        return None
    state.save(
        number,
        **{f"{field}_zh": value, f"{field}_error": "", f"{field}_attempts": 0},
    )
    stats["translated"] += 1
    progress.log(f"{label}翻译完成 request_seconds={time.monotonic() - started:.2f}")
    return value


def _write(context, item, stats, progress):
    movie, host_field, value = item
    latest = _writable(context, movie.movie_id, host_field)
    if latest is None:
        stats["writeback_blocked"] += 1
        return
    if latest.values[host_field] == value:
        return
    label = "标题" if host_field == "title" else "简介"
    progress.emit(progress.stage, detail=f"正在写回{label}")
    applied = context.movies.patch(
        movie.movie_id,
        {host_field: value},
        expected_revision=latest.revision,
    )
    stats["writeback_applied" if applied else "writeback_failed"] += 1
    progress.log(
        f"{label}写回" + ("成功" if applied else "失败：版本冲突或字段已被接管"),
        warning=not applied,
    )


def run_pipeline(context, *, settings, reporter, movie_number=None, translate_only=False):
    if translate_only and not settings.translation_enabled:
        raise RuntimeError("仅翻译任务需要启用翻译")
    stats = dict.fromkeys(
        (
            "movies",
            "no_work",
            "pending_fetch",
            "pending_translation",
            "pending_writeback",
            "failed_movies",
            "translation_exhausted",
            "skipped",
            "movie_not_found",
            "fetched",
            "fetch_cached",
            "not_found",
            "fetch_failed",
            "fetch_exhausted",
            "translated",
            "translation_failed",
            "writeback_applied",
            "writeback_blocked",
            "writeback_failed",
            "aborted",
            "busy",
        ),
        0,
    )
    progress = _Progress(reporter, stats)
    lock = portalocker.Lock(
        str(context.data_dir / "dmm_pipeline.lock"), mode="a", timeout=0
    )
    try:
        lock.acquire()
    except portalocker.exceptions.LockException:
        stats["busy"] = 1
        progress.emit("未执行：已有抓取翻译任务在运行", force=True)
        raise RuntimeError("未执行：已有抓取翻译任务在运行") from None
    try:
        state = DmmState(context.data_dir / "dmm_state.sqlite3")
        prompts = (
            {field: load_prompt(field) for field in ("title", "summary")}
            if settings.translation_enabled
            else {}
        )
        fetches, translations, writebacks = [], [], []
        failed_movies = set()
        stage_specs = (
            [] if translate_only else [("抓取 DMM 文案", fetches, "pending_fetch")]
        ) + [
            ("翻译文案", translations, "pending_translation"),
            ("写回影片", writebacks, "pending_writeback"),
        ]
        total_stages = len(stage_specs) + 1

        def mark_failed(movie):
            failed_movies.add(movie.movie_id)
            stats["failed_movies"] = len(failed_movies)

        def queue_fields(movie, cache):
            if cache["fetch_status"] == "not_found":
                stats["not_found"] += 1
                return
            for field, host_field in (("title", "title"), ("desc", "summary")):
                source = cache[f"raw_{field}"]
                if not source:
                    continue
                if movie.owners.get(host_field) not in (None, PLUGIN_OWNER):
                    stats["writeback_blocked"] += 1
                    continue
                value = cache[f"{field}_zh"] if settings.translation_enabled else source
                if value is None:
                    attempts = cache[f"{field}_attempts"]
                    if attempts >= MAX_ATTEMPTS and movie_number is None:
                        stats["translation_exhausted"] += 1
                    else:
                        translations.append((movie, field, source, attempts))
                        stats["pending_translation"] += 1
                elif value and value != movie.values[host_field]:
                    writebacks.append((movie, host_field, value))
                    stats["pending_writeback"] += 1

        progress.start(f"阶段 1/{total_stages} · 读取和筛选", 0)
        priority = {}
        # 全量扫描时一次性顺序读取缓存；按页随机查库在慢存储上会成为瓶颈。
        all_caches = state.load_all() if movie_number is None else None
        for page in _movie_pages(context, movie_number):
            selected = [snapshot for snapshot in page if not _should_skip(snapshot)]
            excluded = len(page) - len(selected)
            stats["skipped"] += excluded
            progress.current += excluded
            caches = all_caches
            if caches is None:
                caches = state.load_many(
                    [snapshot.values["movie_number"] for snapshot in selected]
                )
            for snapshot in selected:
                movie = _movie_ref(snapshot)
                stats["movies"] += 1
                before = len(fetches) + len(translations) + len(writebacks)
                cache = caches.get(movie.movie_number)
                if cache is None or cache["fetch_status"] in ("pending", "error"):
                    if not translate_only:
                        attempts = cache["fetch_attempts"] if cache else 0
                        if attempts >= MAX_ATTEMPTS and movie_number is None:
                            stats["fetch_exhausted"] += 1
                        elif all(
                            movie.owners.get(field) not in (None, PLUGIN_OWNER)
                            for field in ("title", "summary")
                        ):
                            stats["writeback_blocked"] += 2
                        else:
                            fetches.append((movie, attempts))
                            stats["pending_fetch"] += 1
                else:
                    stats["fetch_cached"] += 1
                    queue_fields(movie, cache)
                if len(fetches) + len(translations) + len(writebacks) == before:
                    stats["no_work"] += 1
                else:
                    priority[movie.movie_id] = _priority_key(snapshot)
                progress.current += 1
                progress.emit(progress.stage, detail=_result_text(stats))
            progress.emit(progress.stage, detail="正在读取影片 · " + _result_text(stats))
        progress.total = progress.current
        progress.emit(
            progress.stage, detail="筛选完成 · " + _result_text(stats), force=True
        )
        with ExitStack() as clients:
            dmm = translator = None
            for index, (name, items, pending_key) in enumerate(stage_specs, start=2):
                stage = f"阶段 {index}/{total_stages} · {name}"
                items.sort(key=lambda item: priority[item[0].movie_id])
                progress.start(stage, len(items))
                for item in items:
                    movie = item[0]
                    number = movie.movie_number
                    progress.number = number
                    failures_before = sum(stats[key] for key in _FAILURE_COUNTS)
                    try:
                        if pending_key == "pending_fetch":
                            if dmm is None:
                                dmm = clients.enter_context(
                                    DmmClient(
                                        timeout_seconds=settings.request_timeout_seconds,
                                        request_interval_seconds=settings.request_interval_seconds,
                                    )
                                )
                            cache = _fetch(state, item, dmm, stats, progress)
                            if cache is not None:
                                queue_fields(movie, cache)
                        elif pending_key == "pending_translation":
                            if translator is None:
                                translator = clients.enter_context(
                                    OpenAITranslationClient(settings)
                                )
                            value = _translate(
                                context, state, item, translator, prompts, stats, progress
                            )
                            host_field = "title" if item[1] == "title" else "summary"
                            if value and value != movie.values[host_field]:
                                writebacks.append((movie, host_field, value))
                                stats["pending_writeback"] += 1
                        else:
                            _write(context, item, stats, progress)
                    except Exception:
                        mark_failed(movie)
                        raise
                    finally:
                        if sum(stats[key] for key in _FAILURE_COUNTS) > failures_before:
                            mark_failed(movie)
                        progress.current += 1
                        stats[pending_key] -= 1
                        progress.emit(
                            stage, detail=_result_text(stats),
                            report=pending_key != "pending_writeback",
                        )
                progress.number = ""
                progress.emit(stage, detail="阶段完成 · " + _result_text(stats), force=True)
        if movie_number and stats["movies"] == 0 and stats["skipped"] == 0:
            stats["movie_not_found"] = 1
        progress.number = ""
        summary = _result_text(stats)
        if stats["movie_not_found"]:
            summary = "未找到指定影片 · " + summary
        progress.emit("抓取翻译完成", detail=summary, force=True)
    except Exception as exc:
        if isinstance(exc, TranslationError) and exc.abort_batch:
            stats["aborted"] = 1
            message = f"{progress.number} · {progress.stage} · {exc}"
            progress.emit(
                "抓取翻译中断", detail=message + " · " + _result_text(stats), force=True
            )
            raise RuntimeError(message) from exc
        progress.emit(
            "抓取翻译失败", detail=f"{exc} · {_result_text(stats)}", force=True
        )
        raise
    finally:
        lock.release()
    return stats


def build_jobs(context, *, settings):
    def run_manual(reporter, params):
        return run_pipeline(
            context,
            settings=settings,
            reporter=reporter,
            movie_number=DmmSyncParams.model_validate(params).movie_number,
        )

    def run_daily(reporter, params):
        return run_pipeline(context, settings=settings, reporter=reporter)

    def run_translate_cached(reporter, params):
        return run_pipeline(
            context, settings=settings, reporter=reporter, translate_only=True
        )

    return (
        JobDefinition(
            task_key="sakuramedia_movie_scrape_translate_sync",
            log_name="movie-scrape-translate",
            cli_name="scrape-translate-movies",
            cli_help="抓取并翻译影片标题和简介",
            manual_only=True,
            params_schema=DmmSyncParams,
            handler=run_manual,
        ),
        JobDefinition(
            task_key="sakuramedia_movie_scrape_translate_sync_subscribed",
            log_name="movie-scrape-translate-subscribed",
            cli_name="scrape-translate-subscribed-movies",
            cli_help="每天按优先级抓取并翻译影片文案",
            default_cron="10 4 * * *",
            handler=run_daily,
        ),
        JobDefinition(
            task_key="sakuramedia_movie_scrape_translate_translate_cached",
            log_name="movie-translate-cached",
            cli_name="translate-cached-movies",
            cli_help="仅翻译已有 DMM 缓存原文的影片文案，不请求 DMM",
            manual_only=True,
            handler=run_translate_cached,
        ),
    )
