"""一部影片一条记录：抓取结果、译文缓存和失败次数。"""

import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

MAX_ATTEMPTS = 5


class DmmState:
    def __init__(self, database_path: Path):
        self.database_path = database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS movie_cache (
                    movie_number TEXT PRIMARY KEY,
                    fetch_status TEXT NOT NULL DEFAULT 'pending',
                    raw_title TEXT NOT NULL DEFAULT '',
                    raw_desc TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    source_id TEXT NOT NULL DEFAULT '',
                    fetch_attempts INTEGER NOT NULL DEFAULT 0,
                    fetch_error TEXT NOT NULL DEFAULT '',
                    title_zh TEXT,
                    desc_zh TEXT,
                    title_attempts INTEGER NOT NULL DEFAULT 0,
                    desc_attempts INTEGER NOT NULL DEFAULT 0,
                    title_error TEXT NOT NULL DEFAULT '',
                    desc_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
            """)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.database_path)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def load(self, movie_number: str) -> dict | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM movie_cache WHERE movie_number = ?", (movie_number,)
            ).fetchone()
            return dict(row) if row is not None else None

    def load_many(self, movie_numbers: list[str]) -> dict[str, dict]:
        cached = {}
        with self.connect() as db:
            for offset in range(0, len(movie_numbers), 500):
                numbers = movie_numbers[offset:offset + 500]
                rows = db.execute(
                    "SELECT * FROM movie_cache WHERE movie_number IN ("
                    + ",".join("?" for _ in numbers) + ")",
                    numbers,
                )
                cached.update((row["movie_number"], dict(row)) for row in rows)
        return cached

    def load_all(self) -> dict[str, dict]:
        # 全量扫描时一次性顺序读取，避免按页随机查库；只取后续用到的列。
        # 缩略列名：本插件内部固定，外部输入仅作参数值。
        columns = (
            "movie_number",
            "fetch_status",
            "raw_title",
            "raw_desc",
            "title_zh",
            "desc_zh",
            "fetch_attempts",
            "title_attempts",
            "desc_attempts",
        )
        with self.connect() as db:
            rows = db.execute("SELECT " + ", ".join(columns) + " FROM movie_cache")
            return {
                row["movie_number"]: {
                    column: row[column] for column in columns if column != "movie_number"
                }
                for row in rows
            }

    def save(self, movie_number: str, **fields):
        # 列名全部由本插件代码传入；外部输入只作为参数化的列值。
        with self.connect() as db:
            db.execute(
                f"INSERT INTO movie_cache (movie_number, {', '.join(fields)}) "
                f"VALUES ({', '.join('?' for _ in range(len(fields) + 1))}) "
                "ON CONFLICT(movie_number) DO UPDATE SET "
                + ", ".join(f"{name} = excluded.{name}" for name in fields)
                + ", updated_at = CURRENT_TIMESTAMP",
                (movie_number, *fields.values()),
            )

    def record_fetch_result(self, movie_number, result):
        previous = self.load(movie_number)
        fields = dict(
            fetch_status=result.status,
            raw_title=result.title,
            raw_desc=result.desc,
            source_url=result.source_url,
            source_id=result.source_id,
            fetch_attempts=0,
            fetch_error="",
        )
        for field, source in (("title", result.title), ("desc", result.desc)):
            if previous is None or source != previous[f"raw_{field}"]:
                fields.update(
                    {
                        f"{field}_zh": None if source else "",
                        f"{field}_attempts": 0,
                        f"{field}_error": "",
                    }
                )
        self.save(movie_number, **fields)
