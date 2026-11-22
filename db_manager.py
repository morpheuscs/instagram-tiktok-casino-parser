import csv
import asyncio
import time
import aiosqlite
from typing import Dict, Any, List
from loguru import logger
from config import DB_PATH


class DatabaseManager:
    def __init__(self, db_path: str = str(DB_PATH)):
        self.db_path = db_path
        self._db = None
        self._queue = asyncio.Queue(maxsize=1000)
        self._worker_task = None

        self._row_defaults = {
            "video_url": "", "video_id": "", "platform": "", "caption": "", "hashtags": "",
            "likes_count": 0, "comments_count": 0, "shares_count": 0, "views_count": 0,
            "video_duration": 0.0, "publish_date": "", "author_username": "", "author_id": "",
            "author_display_name": "", "followers_count": 0, "following_count": 0, "total_posts": 0,
            "account_creation_date": "", "bio": "", "is_verified": 0, "external_url": "",
            "audio_id": "", "audio_title": "", "audio_artist": "", "audio_usage_count": 0,
            "audio_text": "", "ocr_text": "", "status": "parsed", "match_percentage": 0.0,
        }

    async def initialize_db(self) -> None:
        if self._db is None:
            self._db = await aiosqlite.connect(self.db_path)

        await self._db.execute("PRAGMA journal_mode=WAL;")
        await self._db.execute("PRAGMA cache_size=-64000;")
        await self._db.execute("PRAGMA synchronous=NORMAL;")

        await self._db.execute("""
            CREATE TABLE IF NOT EXISTS media_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                video_url TEXT, video_id TEXT, platform TEXT, caption TEXT, hashtags TEXT,
                likes_count INTEGER, comments_count INTEGER, shares_count INTEGER, views_count INTEGER,
                video_duration REAL, publish_date TEXT, author_username TEXT, author_id TEXT,
                author_display_name TEXT, followers_count INTEGER, following_count INTEGER, total_posts INTEGER,
                account_creation_date TEXT, bio TEXT, is_verified INTEGER DEFAULT 0, external_url TEXT,
                audio_id TEXT, audio_title TEXT, audio_artist TEXT, audio_usage_count INTEGER,
                audio_text TEXT, ocr_text TEXT, status TEXT DEFAULT 'parsed', match_percentage REAL DEFAULT 0.0,
                UNIQUE(platform, video_id)
            )
        """)
        await self._db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_platform_video ON media_analytics(platform, video_id);"
        )
        await self._db.execute(
            "CREATE INDEX IF NOT EXISTS idx_likes_views ON media_analytics(likes_count, views_count);"
        )
        await self._db.commit()

        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._batch_writer_worker())

    async def save_media_record(self, data: Dict[str, Any]) -> bool:
        if self._db is None:
            await self.initialize_db()
        await self._queue.put(data)
        return True

    def _prepare_row(self, data: Dict[str, Any]) -> Dict[str, Any]:
        row = self._row_defaults.copy()
        row.update({k: v for k, v in data.items() if k in row})
        return row

    async def _batch_writer_worker(self):
        logger.info("[DB Worker] Ingestion system запущена.")

        batch_size = 100
        timeout = 0.5

        query = """
            INSERT INTO media_analytics (
                video_url, video_id, platform, caption, hashtags, likes_count, comments_count,
                shares_count, views_count, video_duration, publish_date, author_username,
                author_id, author_display_name, followers_count, following_count, total_posts,
                account_creation_date, bio, is_verified, external_url, audio_id, audio_title,
                audio_artist, audio_usage_count, audio_text, ocr_text, status, match_percentage
            ) VALUES (
                :video_url, :video_id, :platform, :caption, :hashtags, :likes_count, :comments_count,
                :shares_count, :views_count, :video_duration, :publish_date, :author_username,
                :author_id, :author_display_name, :followers_count, :following_count, :total_posts,
                :account_creation_date, :bio, :is_verified, :external_url, :audio_id, :audio_title,
                :audio_artist, :audio_usage_count, :audio_text, :ocr_text, :status, :match_percentage
            ) ON CONFLICT(platform, video_id) DO UPDATE SET
                caption=excluded.caption, hashtags=excluded.hashtags,
                likes_count=excluded.likes_count, comments_count=excluded.comments_count,
                shares_count=excluded.shares_count, views_count=excluded.views_count,
                followers_count=excluded.followers_count, following_count=excluded.following_count,
                audio_text=excluded.audio_text, ocr_text=excluded.ocr_text,
                status=excluded.status, match_percentage=excluded.match_percentage
        """

        while True:
            batch = []
            loop = asyncio.get_running_loop()
            start_time = loop.time()
            shutdown_triggered = False

            while len(batch) < batch_size:
                time_left = timeout - (loop.time() - start_time)
                if time_left <= 0:
                    break
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=max(time_left, 0.05))
                    if item is None:
                        shutdown_triggered = True
                        break
                    batch.append(self._prepare_row(item))
                except asyncio.TimeoutError:
                    break

            if batch:
                start_insert = time.perf_counter()
                success = False

                for attempt in range(1, 4):
                    try:
                        await self._db.executemany(query, batch)
                        await self._db.commit()
                        success = True
                        latency = (time.perf_counter() - start_insert) * 1000
                        logger.info(
                            f"[DB Worker] Батч {len(batch)} строк за {latency:.2f}ms. "
                            f"Очередь: {self._queue.qsize()}"
                        )
                        break
                    except Exception as e:
                        logger.warning(f"[DB Worker] Ошибка батча (попытка {attempt}/3): {e}")
                        await self._db.rollback()
                        await asyncio.sleep(0.1 * attempt)

                if not success:
                    logger.error("[DB Worker] Батч упал. Поштучная изоляция...")
                    failed_rows = 0
                    for single_item in batch:
                        try:
                            await self._db.execute(query, single_item)
                        except Exception as single_err:
                            failed_rows += 1
                            logger.error(
                                f"[DB Worker] Битая строка [{single_item.get('video_id')}]: {single_err}"
                            )
                    await self._db.commit()
                    logger.info(
                        f"[DB Worker] Fallback завершён. "
                        f"Спасено: {len(batch) - failed_rows}, потеряно: {failed_rows}"
                    )

                for _ in batch:
                    self._queue.task_done()

            if shutdown_triggered:
                self._queue.task_done()
                break

        logger.info("[DB Worker] Воркер завершил работу.")

    async def export_to_csv(self, target_csv_path: str) -> None:
        """
        Экспорт в CSV.
        ИСПРАВЛЕНИЕ: открывает собственное соединение с БД,
        поэтому работает корректно даже после вызова close().
        """
        try:
            async with aiosqlite.connect(self.db_path) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute("SELECT * FROM media_analytics") as cursor:
                    rows: List[aiosqlite.Row] = await cursor.fetchall()

            if not rows:
                logger.warning("[Export] БД пуста — CSV не создан.")
                return

            headers = rows[0].keys()
            with open(target_csv_path, mode="w", encoding="utf-8", newline="") as f:
                writer = csv.writer(f, delimiter=";", quotechar='"', quoting=csv.QUOTE_MINIMAL)
                writer.writerow(headers)
                for row in rows:
                    writer.writerow(list(row))

            logger.success(f"[Export] CSV сохранён: {target_csv_path} ({len(rows)} строк)")

        except Exception as e:
            logger.error(f"[Export] Ошибка экспорта CSV: {e}")

    async def close(self):
        """Мягкая остановка через Sentinel-маркер."""
        logger.info("[DB] Остановка. Сброс буферов...")
        if self._worker_task:
            await self._queue.put(None)
            await self._queue.join()
            await self._worker_task
        if self._db:
            await self._db.close()
            self._db = None   # ← сброс ссылки, чтобы повторный вызов initialize_db был безопасен
        logger.success("[DB] База данных закрыта.")