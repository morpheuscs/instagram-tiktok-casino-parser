import os
import re
import asyncio
import random
import json
import math
from typing import List, Dict, Any, Optional, Tuple
from urllib.parse import quote
from playwright.async_api import async_playwright, Page, BrowserContext, Response
from loguru import logger
from config import (
    USER_AGENTS,
    DOWNLOAD_DIR, MAX_VIDEOS_PER_KEYWORD,
    INSTAGRAM_USERNAME, INSTAGRAM_PASSWORD,
    SESSION_FILE, PARALLEL_WORKERS
)
from vpn_rotator import VPNRotator

SESSION_FILE = str(SESSION_FILE)

# Ключевые слова для скоринга мошенничества — используются в match_percentage
FRAUD_KEYWORDS = [
    "казино", "casino", "слоты", "slots", "ставки", "бет", "bet", "букмекер",
    "выигрыш", "jackpot", "джекпот", "выплата", "payout",
    "пирамида", "pyramid", "млм", "mlm", "реферал", "referral",
    "гарантированный доход", "guaranteed income", "пассивный доход", "passive income",
    "инвестиции", "invest", "криптовалют", "crypto", "биткоин", "bitcoin",
    "быстрые деньги", "easy money", "заработок", "earn", "заработай",
    "сигналы", "signals", "трейдинг", "trading", "форекс", "forex",
    "бонус", "bonus", "промокод", "promo", "депозит", "deposit",
    "вывод средств", "вывести", "withdraw", "умножить", "multiply",
    "схема", "scheme", "100%", "без риска", "no risk",
]


def _compute_match_percentage(text_blob: str) -> float:
    """
    Лёгкий TF-подобный скоринг: считаем сколько fraud-ключей встречается
    в суммарном тексте поста (caption + hashtags + audio_text + ocr_text).
    Возвращает float 0.0–100.0.
    """
    if not text_blob:
        return 0.0
    blob = text_blob.lower()
    hits = sum(1 for kw in FRAUD_KEYWORDS if kw.lower() in blob)
    # Нелинейное масштабирование: 1 совпадение ~ 20%, 5+ ~ 90%+
    score = min(100.0, round((1 - math.exp(-hits * 0.45)) * 100, 1))
    return score


def _build_text_blob(record: Dict[str, Any]) -> str:
    """Собирает текстовый blob для match_percentage из полей записи."""
    return " ".join([
        record.get("caption") or "",
        record.get("hashtags") or "",
        record.get("audio_text") or "",
        record.get("ocr_text") or "",
        record.get("audio_title") or "",
        record.get("audio_artist") or "",
        record.get("bio") or "",
    ])


# JS: парсинг чисел вида 1.2M / 540K / 7,5к из строки
_JS_PARSE_ABBREV = r"""
function parseAbbrev(raw) {
    if (!raw) return 0;
    const s = String(raw).trim().replace(/\u00a0/g, '').replace(/,/g, '.').toLowerCase()
        .replace(/к/g, 'k').replace(/м/g, 'm').replace(/тыс/g, 'k').replace(/млн/g, 'm');
    const m = s.match(/([\d.]+)\s*([km]?)/);
    if (!m) return 0;
    let n = parseFloat(m[1]);
    if (isNaN(n)) return 0;
    if (m[2] === 'k') n *= 1000;
    if (m[2] === 'm') n *= 1000000;
    return Math.floor(n);
}
"""


class InstagramParser:
    def __init__(self, vpn_rotator: VPNRotator):
        self.vpn_rotator = vpn_rotator
        self._semaphore = asyncio.Semaphore(PARALLEL_WORKERS)

    # ------------------------------------------------------------------
    # Человекоподобное движение мыши по кривой Безье
    # ------------------------------------------------------------------
    async def _simulate_human_mouse(self, page: Page, from_x: int, from_y: int, to_x: int, to_y: int):
        steps = random.randint(15, 30)
        cx1 = from_x + (to_x - from_x) * random.uniform(0.1, 0.4)
        cy1 = from_y + (to_y - from_y) * random.uniform(0.1, 0.4)
        cx2 = from_x + (to_x - from_x) * random.uniform(0.6, 0.9)
        cy2 = from_y + (to_y - from_y) * random.uniform(0.6, 0.9)
        for i in range(steps + 1):
            t = i / steps
            x = (1-t)**3*from_x + 3*(1-t)**2*t*cx1 + 3*(1-t)*t**2*cx2 + t**3*to_x
            y = (1-t)**3*from_y + 3*(1-t)**2*t*cy1 + 3*(1-t)*t**2*cy2 + t**3*to_y
            await page.mouse.move(int(x), int(y))
            await asyncio.sleep(random.uniform(0.01, 0.03))

    # ------------------------------------------------------------------
    # Реалистичный ввод текста
    # ------------------------------------------------------------------
    async def _human_type(self, page: Page, element, text: str):
        await element.click()
        await asyncio.sleep(random.uniform(0.3, 0.7))
        for char in text:
            await page.keyboard.type(char, delay=random.randint(50, 200))

    # ------------------------------------------------------------------
    # Хаотичный скролл
    # ------------------------------------------------------------------
    async def _chaotic_scroll(self, page: Page):
        for _ in range(random.randint(2, 4)):
            scroll_amount = random.randint(300, 700)
            await page.evaluate(f"window.scrollBy(0, {scroll_amount})")
            await asyncio.sleep(random.uniform(0.5, 1.2))
            if random.random() > 0.7:
                await page.evaluate(f"window.scrollBy(0, -{int(scroll_amount * 0.3)})")
                await asyncio.sleep(random.uniform(0.3, 0.8))

    # ------------------------------------------------------------------
    # Принять куки
    # ------------------------------------------------------------------
    async def _accept_cookies(self, page: Page):
        for selector in [
            "button:has-text('Accept All')",
            "button:has-text('Accept all')",
            "button:has-text('Allow all cookies')",
            "button:has-text('Allow All')",
            "button:has-text('Принять все')",
            "[data-testid='cookie-policy-manage-dialog-accept-button']",
        ]:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    logger.info("[Instagram] Куки приняты.")
                    await asyncio.sleep(1)
                    return
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Ручной режим — ждём пока юзер пройдёт капчу/2FA
    # ------------------------------------------------------------------
    async def _wait_for_manual_login(self, page: Page) -> bool:
        logger.warning("=" * 60)
        logger.warning("[Instagram] РУЧНОЙ РЕЖИМ! Действия в окне браузера:")
        logger.warning("  1. Прими куки если есть баннер")
        logger.warning("  2. Введи логин и пароль")
        logger.warning("  3. Пройди 2FA / капчу если появится")
        logger.warning("  4. Дойди до главной страницы Instagram")
        logger.warning("  У тебя 5 минут.")
        logger.warning("=" * 60)

        blocked = ("login", "challenge", "checkpoint", "codeentry", "auth_platform", "two_factor")
        for _ in range(150):
            await asyncio.sleep(2)
            try:
                url = page.url.lower()
            except Exception:
                logger.error("[Instagram] Окно браузера закрыто.")
                return False
            if all(m not in url for m in blocked) and "instagram.com" in url:
                logger.success("[Instagram] Ручной вход прошёл успешно!")
                await page.wait_for_timeout(3000)
                return True

        logger.error("[Instagram] Таймаут ручного входа.")
        return False

    # ------------------------------------------------------------------
    # Авторизация
    # ------------------------------------------------------------------
    async def _login(self, page: Page) -> bool:
        if not INSTAGRAM_USERNAME or not INSTAGRAM_PASSWORD:
            logger.error("[Instagram] Не заданы INSTAGRAM_USERNAME или INSTAGRAM_PASSWORD in config.py!")
            return False
        try:
            logger.info("[Instagram] Открываем страницу входа...")
            await page.goto("https://www.instagram.com/accounts/login/", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(3000)

            await self._accept_cookies(page)
            await page.wait_for_timeout(1500)

            url = page.url.lower()
            if any(x in url for x in ("challenge", "checkpoint", "two_factor")):
                ok = await self._wait_for_manual_login(page)
                if ok:
                    await page.context.storage_state(path=SESSION_FILE)
                return ok

            username_input = None
            for sel in ["input[name='username']", "input[aria-label*='username' i]",
                        "input[placeholder*='username' i]", "input[placeholder*='email' i]"]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=3000):
                        username_input = el
                        break
                except Exception:
                    continue

            password_input = None
            for sel in ["input[name='password']", "input[type='password']", "input[aria-label*='password' i]"]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=3000):
                        password_input = el
                        break
                except Exception:
                    continue

            if not username_input or not password_input:
                logger.warning("[Instagram] Поля ввода не найдены — переход в ручной режим...")
                ok = await self._wait_for_manual_login(page)
                if ok:
                    await page.context.storage_state(path=SESSION_FILE)
                return ok

            await self._human_type(page, username_input, INSTAGRAM_USERNAME)
            await asyncio.sleep(random.uniform(0.8, 1.5))
            await self._human_type(page, password_input, INSTAGRAM_PASSWORD)
            await asyncio.sleep(random.uniform(0.8, 1.5))

            login_btn = None
            for sel in ["button[type='submit']", "button:has-text('Log in')", "div[role='button']:has-text('Log in')"]:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=2000):
                        login_btn = btn
                        break
                except Exception:
                    continue

            if login_btn:
                box = await login_btn.bounding_box()
                if box:
                    await self._simulate_human_mouse(page, 640, 400,
                        int(box["x"] + box["width"] / 2), int(box["y"] + box["height"] / 2))
                await login_btn.click()
            else:
                await password_input.press("Enter")

            await page.wait_for_timeout(5000)

            url = page.url.lower()
            if any(x in url for x in ("challenge", "checkpoint", "codeentry", "auth_platform", "two_factor")):
                logger.warning("[Instagram] Требуется 2FA / дополнительная проверка.")
                ok = await self._wait_for_manual_login(page)
                if ok:
                    await page.context.storage_state(path=SESSION_FILE)
                    logger.success(f"[Instagram] Сессия сохранена: {SESSION_FILE}")
                return ok

            if "login" not in url and "instagram.com" in url:
                logger.success("[Instagram] Авторизация успешна!")
                await page.context.storage_state(path=SESSION_FILE)
                logger.success(f"[Instagram] Сессия сохранена: {SESSION_FILE}")
                return True

            logger.warning("[Instagram] Неизвестное состояние — ручной режим...")
            ok = await self._wait_for_manual_login(page)
            if ok:
                await page.context.storage_state(path=SESSION_FILE)
            return ok

        except Exception as e:
            logger.error(f"[Instagram] Критическая ошибка при логине: {e}")
            try:
                ok = await self._wait_for_manual_login(page)
                if ok:
                    await page.context.storage_state(path=SESSION_FILE)
                return ok
            except Exception:
                return False

    # ------------------------------------------------------------------
    # FIX: Перехват видео через network responses
    # ------------------------------------------------------------------
    async def _intercept_and_download_video(self, page: Page, video_id: str) -> str:
        """
        Слушаем сетевые ответы страницы и ловим mp4-файл напрямую.

        Instagram иногда отдаёт видео в DASH fMP4: несколько чанков
        одинакового размера (~376KB). Первый чанк содержит moov/init box
        (заголовки), остальные — moof+mdat (данные).  Сохранение только
        одного чанка даёт битый файл («trun track id unknown, no tfhd was
        found»).  Решение: собираем ВСЕ чанки в порядке поступления и
        конкатенируем — это даёт валидный fragmented MP4.
        """
        # Список чанков в порядке поступления + самый большой одиночный
        # файл (для случая обычного, не-DASH MP4).
        segments: list[bytes] = []
        best_single: bytes = b""

        async def handle_response(response: Response):
            nonlocal best_single
            url = response.url
            ct = response.headers.get("content-type", "")
            if ".mp4" in url or "video/mp4" in ct or "video/webm" in ct:
                try:
                    body = await response.body()
                    if len(body) < 1024:          # меньше 1KB — точно мусор
                        return
                    segments.append(body)
                    # Дополнительно трекаем самый большой одиночный ответ
                    # на случай, если это цельный MP4 (не DASH).
                    if len(body) > len(best_single):
                        best_single = body
                except Exception:
                    pass

        page.on("response", handle_response)

        # Кликаем на видео чтобы оно начало грузиться
        try:
            video_el = page.locator("video").first
            if await video_el.count() > 0:
                await video_el.click(timeout=3000)
                await asyncio.sleep(2)
        except Exception:
            pass

        # Скроллим немного — иногда это триггерит загрузку
        await page.evaluate("window.scrollBy(0, 100)")
        await asyncio.sleep(1.5)

        page.remove_listener("response", handle_response)

        if not segments:
            return ""

        # Если пришёл один большой чанк (≥ 500KB) — это цельный MP4.
        # Если несколько чанков одного размера — это DASH fMP4, конкатенируем.
        if len(segments) == 1 and len(segments[0]) >= 512 * 1024:
            video_data = segments[0]
        elif best_single and len(best_single) > sum(len(s) for s in segments) * 0.8:
            # Один чанк сильно доминирует по размеру — берём его
            video_data = best_single
        else:
            # DASH: склеиваем все сегменты в порядке поступления
            video_data = b"".join(segments)

        if len(video_data) < 10240:
            return ""

        local_filename = f"ig_{video_id}.mp4"
        local_path = os.path.join(str(DOWNLOAD_DIR), local_filename)
        try:
            with open(local_path, "wb") as f:
                f.write(video_data)
            logger.info(
                f"[Instagram] Видео перехвачено: {local_filename} "
                f"({len(video_data)//1024}KB, {len(segments)} сегм.)"
            )
            return local_path
        except Exception as e:
            logger.warning(f"[Instagram] Не удалось сохранить видео: {e}")
        return ""

    # ------------------------------------------------------------------
    # Загрузка видео для корректного video.duration
    # ------------------------------------------------------------------
    async def _load_video_duration(self, page: Page) -> float:
        try:
            await page.wait_for_selector("video", timeout=8000)
            await page.evaluate(
                """async () => {
                    const v = document.querySelector('video');
                    if (!v) return;
                    v.muted = true;
                    try { await v.play(); } catch (e) {}
                    if (v.readyState < 1) {
                        await new Promise(resolve => {
                            v.addEventListener('loadedmetadata', resolve, { once: true });
                            setTimeout(resolve, 3500);
                        });
                    }
                }"""
            )
            await page.wait_for_timeout(1200)
            dur = await page.evaluate(
                """() => {
                    const v = document.querySelector('video');
                    if (v && v.duration && isFinite(v.duration) && v.duration > 0) {
                        return Math.round(v.duration * 10) / 10;
                    }
                    return 0;
                }"""
            )
            return float(dur or 0)
        except Exception as e:
            logger.debug(f"[Instagram] video duration load: {e}")
            return 0.0

    # ------------------------------------------------------------------
    # Просмотры: embedded JSON, DOM, meta-теги
    # ------------------------------------------------------------------
    async def _extract_views_from_page(self, page: Page) -> int:
        try:
            views = await page.evaluate(
                r"""() => {"""
                + _JS_PARSE_ABBREV
                + r"""
                    const patterns = [
                        /"video_view_count"\s*:\s*(\d+)/,
                        /"play_count"\s*:\s*(\d+)/,
                        /"view_count"\s*:\s*(\d+)/,
                        /"video_play_count"\s*:\s*(\d+)/,
                    ];
                    for (const s of document.querySelectorAll('script:not([src])')) {
                        const t = s.textContent || '';
                        for (const p of patterns) {
                            const m = t.match(p);
                            if (m && parseInt(m[1], 10) > 0) return parseInt(m[1], 10);
                        }
                    }

                    for (const sel of [
                        'span[class*="x1lliihq"]', 'section span', 'article span',
                        '[aria-label*="views" i]', '[aria-label*="просмотр" i]',
                    ]) {
                        for (const el of document.querySelectorAll(sel)) {
                            const txt = (el.innerText || el.getAttribute('aria-label') || '').trim();
                            if (/views|просмотр/i.test(txt)) {
                                const n = parseAbbrev(txt.replace(/[^0-9.,KkMmкКм]/g, ''));
                                if (n > 0) return n;
                            }
                        }
                    }

                    const og = document.querySelector('meta[property="og:description"]');
                    if (og && og.content) {
                        const m = og.content.match(/([\d.,]+[KkMm]?)\s+(views|просмотр)/i);
                        if (m) {
                            const n = parseAbbrev(m[1]);
                            if (n > 0) return n;
                        }
                    }

                    const ldScripts = document.querySelectorAll('script[type="application/ld+json"]');
                    for (const s of ldScripts) {
                        try {
                            const data = JSON.parse(s.textContent || '');
                            const nodes = Array.isArray(data) ? data : [data];
                            for (const node of nodes) {
                                const stats = node.interactionStatistic || [];
                                const list = Array.isArray(stats) ? stats : [stats];
                                for (const st of list) {
                                    const it = st.interactionType || '';
                                    if (/Watch|Play/i.test(it)) {
                                        const c = parseInt(st.userInteractionCount || 0, 10);
                                        if (c > 0) return c;
                                    }
                                }
                            }
                        } catch (e) {}
                    }
                    return 0;
                }"""
            )
            return int(views or 0)
        except Exception as e:
            logger.debug(f"[Instagram] views extract: {e}")
            return 0

    # ------------------------------------------------------------------
    # FIX: Парсинг метаданных поста
    # ------------------------------------------------------------------
    async def _extract_post_metadata(self, page: Page, keyword: str) -> Dict[str, Any]:
        result = {
            "caption": "",
            "author_username": "",
            "author_display_name": "",
            "likes_count": 0,
            "comments_count": 0,
            "views_count": 0,
            "publish_date": "",
            "video_duration": 0.0,
            "audio_id": "",
            "audio_title": "",
            "audio_artist": "",
            "audio_usage_count": 0,
        }

        # ── 1. JSON-LD ────────────────────────────────────────────────
        try:
            json_ld_text = await page.evaluate('''() => {
                const scripts = Array.from(document.querySelectorAll('script[type="application/ld+json"]'));
                for (let s of scripts) {
                    const t = s.innerText || s.textContent || "";
                    if (t.includes("interactionCount") || t.includes("author") || t.includes("datePublished")) return t;
                }
                return "";
            }''')
            if json_ld_text:
                data = json.loads(json_ld_text)
                main = data[0] if isinstance(data, list) else data

                result["caption"] = (
                    main.get("headline") or main.get("articleBody") or main.get("description") or ""
                )

                result["publish_date"] = (
                    main.get("datePublished") or main.get("uploadDate") or ""
                )

                author = main.get("author", {})
                if isinstance(author, dict):
                    result["author_username"] = (
                        author.get("alternateName") or author.get("identifier") or ""
                    ).lstrip("@")
                    result["author_display_name"] = author.get("name") or result["author_username"]

                for stat in main.get("interactionStatistic", []):
                    itype = stat.get("interactionType", "")
                    count = int(stat.get("userInteractionCount", 0) or 0)
                    if "LikeAction" in itype:
                        result["likes_count"] = count
                    elif "CommentAction" in itype:
                        result["comments_count"] = count
                    elif "WatchAction" in itype:
                        result["views_count"] = count
        except Exception:
            pass

        # ── 2. DOM — username, display_name, publish_date, duration ──
        try:
            dom_data = await page.evaluate(r'''() => {
                const out = {
                    username: "", display_name: "", publish_date: "",
                    video_duration: 0,
                    audio_id: "", audio_title: "", audio_artist: "", audio_usage_count: 0
                };

                const urlMatch = location.pathname.match(/^\/([\w\.]+)\/(p|reel|tv)\//);
                if (urlMatch) out.username = urlMatch[1];

                if (!out.username) {
                    const canonical = document.querySelector('link[rel="canonical"]');
                    if (canonical) {
                        const m = (canonical.href || "").match(/instagram\.com\/([\w\.]+)\/(p|reel|tv)\//);
                        if (m) out.username = m[1];
                    }
                }

                if (!out.username) {
                    const ogUrl = document.querySelector('meta[property="og:url"]');
                    if (ogUrl) {
                        const m = (ogUrl.content || "").match(/instagram\.com\/([\w\.]+)\/(p|reel|tv)\//);
                        if (m) out.username = m[1];
                    }
                }

                if (!out.username) {
                    const SKIP = new Set(["explore","reel","p","tv","stories","direct","accounts","about","legal","_u"]);
                    const links = document.querySelectorAll('header a[href], article a[href]');
                    for (const a of links) {
                        const href = a.getAttribute("href") || "";
                        const m = href.match(/^\/([\w][\w\.]{1,29})\/$/);
                        if (m && !SKIP.has(m[1])) { out.username = m[1]; break; }
                    }
                }

                const nameSelectors = [
                    'header h2', 'h1[dir]',
                    'header span[class*="x1lliihq"]',
                    'article h2', 'header div[class*="Username"]'
                ];
                for (const sel of nameSelectors) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText && el.innerText.trim().length > 0) {
                        out.display_name = el.innerText.trim();
                        break;
                    }
                }

                const timeEl = document.querySelector('time[datetime]');
                if (timeEl) out.publish_date = timeEl.getAttribute("datetime") || "";

                const videoEl = document.querySelector('video');
                if (videoEl && videoEl.duration && isFinite(videoEl.duration)) {
                    out.video_duration = Math.round(videoEl.duration * 10) / 10;
                }

                const audioLink = document.querySelector('a[href*="/reels/audio/"]');
                if (audioLink) {
                    const href = audioLink.getAttribute("href") || "";
                    const idMatch = href.match(/\/reels\/audio\/(\d+)/);
                    if (idMatch) out.audio_id = idMatch[1];
                    const audioText = audioLink.innerText || audioLink.textContent || "";
                    const parts = audioText.split("·");
                    if (parts.length >= 2) {
                        out.audio_title = parts[0].trim();
                        out.audio_artist = parts[1].trim();
                    } else if (parts.length === 1 && parts[0].trim()) {
                        out.audio_title = parts[0].trim();
                    }
                }

                if (!out.audio_title) {
                    const musicSpans = document.querySelectorAll('span[aria-label*="Audio"], div[class*="audio"] span');
                    for (const sp of musicSpans) {
                        const t = sp.innerText.trim();
                        if (t && t.length > 2) { out.audio_title = t; break; }
                    }
                }

                const audioUsageEl = document.querySelector('a[href*="/reels/audio/"] ~ span, [class*="audioUseCount"]');
                if (audioUsageEl) {
                    const numMatch = (audioUsageEl.innerText || "").replace(/[,\.]/g,"").match(/\d+/);
                    if (numMatch) out.audio_usage_count = parseInt(numMatch[0]) || 0;
                }

                return out;
            }''')

            if dom_data.get("username") and not result["author_username"]:
                result["author_username"] = dom_data["username"]
            if dom_data.get("display_name") and not result["author_display_name"]:
                result["author_display_name"] = dom_data["display_name"]
            if dom_data.get("publish_date") and not result["publish_date"]:
                result["publish_date"] = dom_data["publish_date"]
            if dom_data.get("video_duration", 0) > 0 and result["video_duration"] == 0.0:
                result["video_duration"] = dom_data["video_duration"]
            if dom_data.get("audio_id"):
                result["audio_id"] = dom_data["audio_id"]
            if dom_data.get("audio_title"):
                result["audio_title"] = dom_data["audio_title"]
            if dom_data.get("audio_artist"):
                result["audio_artist"] = dom_data["audio_artist"]
            if dom_data.get("audio_usage_count", 0) > 0:
                result["audio_usage_count"] = dom_data["audio_usage_count"]

        except Exception as e:
            logger.debug(f"[Instagram] DOM metadata error: {e}")

        # ── 3. og:description fallback ────────────────────────────────
        try:
            meta_desc = await page.evaluate(
                "() => document.querySelector('meta[property=\"og:description\"]')?.content || ''"
            )
            if meta_desc:
                if not result["author_username"]:
                    m = re.search(r'@([\w\.]+)', meta_desc)
                    if m:
                        result["author_username"] = m.group(1)
                if result["likes_count"] == 0:
                    m = re.search(r'([\d,]+)\s+[Ll]ikes?', meta_desc)
                    if m:
                        result["likes_count"] = int(m.group(1).replace(',', ''))
                if result["comments_count"] == 0:
                    m = re.search(r'([\d,]+)\s+[Cc]omments?', meta_desc)
                    if m:
                        result["comments_count"] = int(m.group(1).replace(',', ''))
                if result["views_count"] == 0:
                    m = re.search(r'([\d,\.]+[KkMm]?)\s+(views|Views|просмотр)', meta_desc)
                    if m:
                        result["views_count"] = InstagramParser._parse_abbreviated_number(m.group(1))
        except Exception:
            pass

        # ── 5. Embedded JSON + DOM просмотры ─────────────────────────
        if result["views_count"] == 0:
            result["views_count"] = await self._extract_views_from_page(page)

        # ── 6. Длительность — принудительная загрузка video ──────────
        if result["video_duration"] == 0.0:
            loaded_dur = await self._load_video_duration(page)
            if loaded_dur > 0:
                result["video_duration"] = loaded_dur

        # ── 4. og:title как последний fallback caption ────────────────
        if not result["caption"]:
            try:
                result["caption"] = await page.evaluate(
                    "() => document.querySelector('meta[property=\"og:title\"]')?.content || document.title || ''"
                )
            except Exception:
                result["caption"] = f"Instagram контент #{keyword}"

        if not result["author_display_name"] and result["author_username"]:
            result["author_display_name"] = result["author_username"]

        return result

    # ------------------------------------------------------------------
    # FIX: Парсинг профиля — добавлен сборщик просмотров по шорткодам постов
    # ------------------------------------------------------------------
    async def _extract_profile_data(self, page: Page, username: str, target_shortcodes: List[str] = None) -> Tuple[int, int, int, str, int, Dict[str, int]]:
        followers_count = 0
        following_count = 0
        total_posts = 0
        bio = ""
        is_verified = 0
        detected_views = {} # Мапа shortcode -> views

        if target_shortcodes is None:
            target_shortcodes = []

        try:
            try:
                await page.goto(f"https://www.instagram.com/{username}/", timeout=50000, wait_until="domcontentloaded")
            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "err_http_response_code_failure" in error_str:
                    logger.warning(f"[Instagram] Блокировка при открытии профиля @{username}")
                    await self.vpn_rotator.rotate_ip()
                    try:
                        await page.goto(f"https://www.instagram.com/{username}/", timeout=50000, wait_until="domcontentloaded")
                    except Exception:
                        logger.warning(f"[Instagram] Не удалось загрузить профиль @{username} даже после ротации")
                        return followers_count, following_count, total_posts, bio, is_verified, detected_views
                else:
                    logger.warning(f"[Instagram] Ошибка при открытии профиля @{username}: {error_str[:80]}")
                    return followers_count, following_count, total_posts, bio, is_verified, detected_views

            await page.wait_for_timeout(random.randint(2500, 4000))

            # Скроллим слегка профиль, чтобы подгрузить плитку с Reels (актуально, если постов много)
            await page.evaluate("window.scrollBy(0, 400)")
            await page.wait_for_timeout(1000)

            # Передаем массив шорткодов в контекст браузера для сопоставления
            profile_data = await page.evaluate(
                r'''([shortcodes]) => {'''
                + _JS_PARSE_ABBREV
                + r'''
                const result = { followers: 0, following: 0, posts: 0, bio: "", verified: false, views_map: {} };

                const followersSpan =
                    document.querySelector('a[href$="/followers/"] span[title]') ||
                    document.querySelector('a[href*="followers"] span[title]') ||
                    document.querySelector('a[href$="/followers/"] span');
                if (followersSpan) {
                    const val = followersSpan.getAttribute("title") || followersSpan.innerText || "0";
                    result.followers = parseInt(val.replace(/[^0-9]/g, "")) || 0;
                }

                const followingSpan =
                    document.querySelector('a[href$="/following/"] span[title]') ||
                    document.querySelector('a[href*="following"] span[title]') ||
                    document.querySelector('a[href$="/following/"] span');
                if (followingSpan) {
                    const val = followingSpan.getAttribute("title") || followingSpan.innerText || "0";
                    result.following = parseInt(val.replace(/[^0-9]/g, "")) || 0;
                }

                const statItems = document.querySelectorAll('ul > li');
                if (statItems.length >= 3) {
                    const getText = el => {
                        const sp = el.querySelector('span[title]') || el.querySelector('span');
                        if (!sp) return 0;
                        const v = sp.getAttribute("title") || sp.innerText || "0";
                        return parseInt(v.replace(/[^0-9]/g, "")) || 0;
                    };
                    if (result.followers === 0) result.followers = getText(statItems[1]);
                    if (result.following === 0) result.following = getText(statItems[2]);
                    if (result.posts === 0) result.posts = getText(statItems[0]);
                }

                const bioSelectors = [
                    'header section div span[class]',
                    'div[data-testid="user-description"] span',
                    'span._ap3a._aaco._aacu._aacx._aad7._aade',
                    'header section > div:nth-child(3) span',
                    'header section > div:last-child span',
                    'h1 ~ div span', 'section h1 + div span',
                ];
                for (const sel of bioSelectors) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText && el.innerText.trim().length > 2) {
                        result.bio = el.innerText.trim();
                        break;
                    }
                }

                result.verified = !!(
                    document.querySelector('[aria-label="Verified"]') ||
                    document.querySelector('[title="Verified"]') ||
                    document.querySelector('svg[aria-label*="Verified"]') ||
                    document.querySelector('img[alt*="Verified"]') ||
                    document.querySelector('[class*="verified" i]')
                );

                // --- Сборщик просмотров через overlay-градиент плитки ---
                // Instagram показывает просмотры в div[style*="linear-gradient"]
                // поверх превью. Стабильный селектор — не зависит от CSS-классов.
                shortcodes.forEach(code => {
                    const postLink = document.querySelector(`a[href*="${code}"]`);
                    if (postLink) {
                        const aria = postLink.getAttribute('aria-label') || '';
                        if (/views|просмотр/i.test(aria)) {
                            const n = parseAbbrev(aria.replace(/[^0-9.,KkMmкКм]/g, ''));
                            if (n > 0) { result.views_map[code] = n; return; }
                        }
                        const overlay = postLink.querySelector('div[style*="linear-gradient"]');
                        if (overlay) {
                            const raw = (overlay.innerText || '').trim();
                            const n = parseAbbrev(raw.replace(/[^0-9.,KkMmкКм]/g, ''));
                            if (n > 0) result.views_map[code] = n;
                        }
                    }
                });

                return result;
            }''', [target_shortcodes])

            followers_count = profile_data.get("followers", 0)
            following_count = profile_data.get("following", 0)
            total_posts = profile_data.get("posts", 0)
            bio = profile_data.get("bio", "")
            is_verified = 1 if profile_data.get("verified", False) else 0
            detected_views = profile_data.get("views_map", {})

            # Fallback: embedded JSON Instagram (edge_followed_by и т.д.)
            if followers_count == 0 or following_count == 0 or total_posts == 0:
                try:
                    embedded = await page.evaluate(
                        r"""() => {
                            const out = { followers: 0, following: 0, posts: 0 };
                            for (const s of document.querySelectorAll('script:not([src])')) {
                                const t = s.textContent || '';
                                if (!t.includes('edge_followed_by') && !t.includes('edge_follow')) continue;
                                const fm = t.match(/"edge_followed_by"\s*:\s*\{\s*"count"\s*:\s*(\d+)/);
                                const fg = t.match(/"edge_follow"\s*:\s*\{\s*"count"\s*:\s*(\d+)/);
                                const pm = t.match(/"edge_owner_to_timeline_media"\s*:\s*\{\s*"count"\s*:\s*(\d+)/);
                                if (fm) out.followers = parseInt(fm[1], 10);
                                if (fg) out.following = parseInt(fg[1], 10);
                                if (pm) out.posts = parseInt(pm[1], 10);
                                if (out.followers || out.following || out.posts) break;
                            }
                            return out;
                        }"""
                    )
                    if embedded.get("followers", 0) > 0 and followers_count == 0:
                        followers_count = embedded["followers"]
                    if embedded.get("following", 0) > 0 and following_count == 0:
                        following_count = embedded["following"]
                    if embedded.get("posts", 0) > 0 and total_posts == 0:
                        total_posts = embedded["posts"]
                except Exception:
                    pass

            if followers_count == 0:
                try:
                    meta = await page.evaluate(
                        "() => document.querySelector('meta[name=\"description\"]')?.content || ''"
                    )
                    if meta:
                        def parse_ig_num(s: str) -> int:
                            s = s.replace(',', '').replace('.', '').strip()
                            if s.upper().endswith('K'):
                                return int(float(s[:-1]) * 1000)
                            if s.upper().endswith('M'):
                                return int(float(s[:-1]) * 1_000_000)
                            try:
                                return int(s)
                            except Exception:
                                return 0

                        m = re.search(r'([\d,\.]+[KkMm]?)\s+[Ff]ollowers?', meta)
                        if m:
                            followers_count = parse_ig_num(m.group(1))
                        m = re.search(r'([\d,\.]+[KkMm]?)\s+[Ff]ollowing', meta)
                        if m:
                            following_count = parse_ig_num(m.group(1))
                        m = re.search(r'([\d,\.]+[KkMm]?)\s+[Pp]osts?', meta)
                        if m:
                            total_posts = parse_ig_num(m.group(1))
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f"[Instagram] Ошибка парсинга профиля @{username}: {e}")

        return followers_count, following_count, total_posts, bio, is_verified, detected_views

    # ------------------------------------------------------------------
    # Обработка одного поста — запускается параллельно
    # ------------------------------------------------------------------
    async def _process_single_post(
        self,
        context: BrowserContext,
        v_url: str,
        keyword: str,
        worker_idx: int,
        ml_processor,
        vpn_rotator=None,
        views_count: int = 0  # Добавлено для передачи просмотров, собранных на этапе скроллинга сетки
    ) -> Optional[Dict]:
        async with self._semaphore:
            await asyncio.sleep(worker_idx * random.uniform(0.5, 1.5))
            page = await context.new_page()
            local_path = ""
            try:
                cleaned_tag = re.sub(r"[#\W]+", "", keyword).lower().strip()

                try:
                    await page.goto(f"https://www.instagram.com/explore/tags/{cleaned_tag}/", 
                                    timeout=30000, wait_until="domcontentloaded")
                    await page.wait_for_timeout(500)
                except Exception:
                    pass

                try:
                    response = await page.goto(
                        v_url, timeout=60000, wait_until="domcontentloaded"
                    )
                except Exception as goto_error:
                    error_str = str(goto_error).lower()
                    if any(x in error_str for x in ["429", "403", "err_http_response_code_failure", "net::"]):
                        logger.warning(f"[Instagram Worker {worker_idx}] Блокировка на {v_url}: {error_str[:100]}")
                        if vpn_rotator:
                            logger.info(f"[Instagram Worker {worker_idx}] Попытка ротации VPN...")
                            await vpn_rotator.rotate_ip()
                            try:
                                response = await page.goto(
                                    v_url, timeout=60000, wait_until="domcontentloaded"
                                )
                            except Exception as retry_error:
                                logger.warning(f"[Instagram Worker {worker_idx}] Повтор не помог: {str(retry_error)[:80]}")
                                return None
                        else:
                            return None
                    else:
                        logger.warning(f"[Instagram Worker {worker_idx}] Ошибка {v_url}: {error_str[:80]}")
                        return None

                if response and response.status in (404, 410, 403):
                    logger.debug(f"[Instagram] Пост недоступен ({response.status}): {v_url}")
                    return None
                if "login" in page.url or "error" in page.url:
                    return None

                await page.wait_for_timeout(random.randint(2000, 4000))

                try:
                    content_loaded = await page.evaluate("document.body.innerText.length > 50")
                    if not content_loaded:
                        logger.debug(f"[Instagram] Пустая страница: {v_url}")
                        return None
                except Exception:
                    pass

                parts = v_url.rstrip("/").split("/")
                video_id = parts[-1] if parts else f"id_{random.randint(10000, 99999)}"

                meta = await self._extract_post_metadata(page, keyword)

                author_username = meta["author_username"]
                author_display_name = meta["author_display_name"]
                likes_count = meta["likes_count"]
                comments_count = meta["comments_count"]

                # Если из метаданных страницы поста почему-то пришло 0, используем переданное значение из сетки тегов
                final_views = meta["views_count"] if meta["views_count"] > 0 else views_count

                publish_date = meta["publish_date"]
                video_duration = meta["video_duration"]
                audio_id = meta["audio_id"]
                audio_title = meta["audio_title"]
                audio_artist = meta["audio_artist"]
                audio_usage_count = meta["audio_usage_count"]

                # Данные профиля собираются изолированно на Этапе 2
                followers_count = 0
                following_count = 0
                total_posts = 0
                bio = ""
                is_verified = 0

                # FIX: Перехват видео через network responses (страница стабильна, событие ловится на 100%)
                local_path = await self._intercept_and_download_video(page, video_id)

                if not local_path:
                    try:
                        video_el = page.locator("video").first
                        if await video_el.count() > 0:
                            video_src = await video_el.get_attribute("src") or ""
                            if video_src and not video_src.startswith("blob:"):
                                local_filename = f"ig_{video_id}.mp4"
                                local_path = os.path.join(str(DOWNLOAD_DIR), local_filename)
                                video_resp = await page.context.request.get(video_src, timeout=20000)
                                content = await video_resp.body()
                                if len(content) > 10240:
                                    with open(local_path, "wb") as f:
                                        f.write(content)
                                else:
                                    local_path = ""
                    except Exception:
                        local_path = ""

                # РАСШИРЕНИЕ: Безопасная валидация файла перед OpenCV во избежание сегфолтов FFmpeg
                if local_path and os.path.exists(local_path) and os.path.getsize(local_path) > 50 * 1024 and video_duration == 0.0:
                    try:
                        import cv2
                        cap = cv2.VideoCapture(local_path)
                        if cap.isOpened():
                            fps = cap.get(cv2.CAP_PROP_FPS) or 30
                            frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
                            if fps > 0 and frames > 0:
                                video_duration = round(frames / fps, 1)
                        cap.release()
                    except Exception:
                        pass

                # ML обработка
                audio_text = ""
                ocr_text = ""
                # РАСШИРЕНИЕ: Защитный блок и проверка размера (>50KB), чтобы битые чанки не ломали Whisper
                if local_path and os.path.exists(local_path) and os.path.getsize(local_path) > 50 * 1024:
                    if ml_processor:
                        try:
                            audio_text = await asyncio.to_thread(ml_processor.extract_audio_text, local_path)
                        except Exception as ml_ae:
                            logger.warning(f"[ML Guard] Ошибка Whisper: {ml_ae}")

                        try:
                            ocr_text = await asyncio.to_thread(ml_processor.extract_ocr_text, local_path)
                        except Exception as ml_oe:
                            logger.warning(f"[ML Guard] Ошибка Tesseract: {ml_oe}")
                    try:
                        os.remove(local_path)
                    except Exception:
                        pass
                else:
                    if local_path and os.path.exists(local_path):
                        try: os.remove(local_path)
                        except Exception: pass

                hashtags = " ".join(w for w in meta["caption"].split() if w.startswith("#")) or f"#{keyword}"

                text_blob = " ".join([
                    meta["caption"], hashtags, audio_text, ocr_text,
                    audio_title, audio_artist, bio
                ])
                match_percentage = _compute_match_percentage(text_blob)

                return {
                    "video_url": v_url,
                    "video_id": video_id,
                    "platform": "instagram",
                    "caption": meta["caption"] or f"Instagram #{keyword}",
                    "hashtags": hashtags,
                    "likes_count": likes_count,
                    "comments_count": comments_count,
                    "shares_count": 0,
                    "views_count": final_views,
                    "video_duration": video_duration,
                    "publish_date": publish_date,
                    "author_username": author_username or "unknown",
                    "author_id": f"ig_{author_username}" if author_username else f"ig_unknown_{video_id}",
                    "author_display_name": author_display_name or author_username or "Unknown",
                    "followers_count": followers_count,
                    "following_count": following_count,
                    "total_posts": total_posts,
                    "account_creation_date": "",
                    "bio": bio,
                    "is_verified": is_verified,
                    "external_url": f"https://www.instagram.com/{author_username}/" if author_username else "",
                    "audio_id": audio_id,
                    "audio_title": audio_title,
                    "audio_artist": audio_artist,
                    "audio_usage_count": audio_usage_count,
                    "audio_text": audio_text,
                    "ocr_text": ocr_text,
                    "status": "parsed",
                    "match_percentage": match_percentage,
                    "local_path": ""
                }

            except Exception as e:
                logger.warning(f"[Instagram Worker {worker_idx}] Ошибка поста {v_url}: {e}")
                return None
            finally:
                try:
                    await page.close()
                except Exception:
                    pass


    # ------------------------------------------------------------------
    # Вспомогательные утилиты для get_reel_views_by_shortcode
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_abbreviated_number(raw: str) -> int:
        """Разбирает '1.2M', '540K', '7.5к' и т.п. -> int."""
        if not raw:
            return 0
        s = raw.strip().replace('\u00a0', '').replace(',', '.').lower()
        s = s.replace('к', 'k').replace('м', 'm').replace('тыс', 'k').replace('млн', 'm')
        m = re.search(r'([\d.]+)\s*([km]?)', s)
        if not m:
            return 0
        try:
            num = float(m.group(1))
            suffix = m.group(2)
            if suffix == 'k':
                num *= 1_000
            elif suffix == 'm':
                num *= 1_000_000
            return int(num)
        except ValueError:
            return 0

    @staticmethod
    def _extract_views_from_json_ld(json_ld_text: str) -> int:
        """Парсит application/ld+json, ищет interactionStatistic WatchAction."""
        try:
            data = json.loads(json_ld_text)
        except (json.JSONDecodeError, TypeError):
            return 0
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            stats = node.get("interactionStatistic", [])
            if isinstance(stats, dict):
                stats = [stats]
            for stat in stats:
                itype = stat.get("interactionType", "")
                if any(t in itype for t in ("WatchAction", "Watch", "PlayAction")):
                    count = stat.get("userInteractionCount", 0)
                    if isinstance(count, (int, float)) and count > 0:
                        return int(count)
                    if isinstance(count, str):
                        v = InstagramParser._parse_abbreviated_number(count)
                        if v > 0:
                            return v
        return 0

    # ------------------------------------------------------------------
    # Получение просмотров конкретного Reels по shortcode (SEO/JSON-LD)
    # ------------------------------------------------------------------
    async def get_reel_views_by_shortcode(
        self,
        page: Page,
        username: str,
        shortcode: str,
    ) -> int:
        """
        Получает количество просмотров конкретного Reel по его shortcode.

        Алгоритм:
          1. /{username}/reels/  — скролл, ищем плитку по shortcode в href.
          2. aria-label плитки  — парсим "N просмотров / views" (fallback).
          3. /reel/{shortcode}/ — читаем application/ld+json → WatchAction.
          4. og:description / meta[name=description] — последний fallback.
        """
        views: int = 0
        profile_reels_url = f"https://www.instagram.com/{username}/reels/"

        # -- Шаг 1: открываем вкладку Reels профиля ----------------------
        try:
            logger.info(f"[ReelsViews] Открываем {profile_reels_url}")
            await page.goto(profile_reels_url, timeout=50_000, wait_until="domcontentloaded")
            await page.wait_for_timeout(random.randint(3_000, 5_000))
        except Exception as e:
            logger.warning(f"[ReelsViews] Не удалось загрузить /reels/ @{username}: {e}")
            return 0

        # JS для поиска плитки по shortcode
        js_has_tile = """(sc) => {
            const links = document.querySelectorAll('a[href]');
            for (const a of links) { 
                if ((a.getAttribute('href')||'').includes(sc)) return true; 
            }
            return false;
        }"""

        js_tile = r"""(sc) => {
            function parseAbbrev(raw) {
                if (!raw) return 0;
                const s = String(raw).trim().replace(/\u00a0/g, '').replace(/,/g, '.').toLowerCase()
                    .replace(/к/g, 'k').replace(/м/g, 'm').replace(/тыс/g, 'k').replace(/млн/g, 'm');
                const m = s.match(/([\d.]+)\s*([km]?)/);
                if (!m) return 0;
                let n = parseFloat(m[1]);
                if (isNaN(n)) return 0;
                if (m[2] === 'k') n *= 1000;
                if (m[2] === 'm') n *= 1000000;
                return Math.floor(n);
            }
            const links = document.querySelectorAll('a[href]');
            let anchor = null;
            for (const a of links) {
                if ((a.getAttribute('href')||'').includes(sc)) { anchor = a; break; }
            }
            if (!anchor) return 0;

            // Сначала смотрим aria-label плитки
            const aria = anchor.getAttribute('aria-label') || '';
            if (/views|просмотр/i.test(aria)) {
                const n = parseAbbrev(aria.replace(/[^0-9.,KkMmкКм]/g, ''));
                if (n > 0) return n;
            }

            // Текст в левом нижнем углу превью (span/div с числом под плиткой)
            // Instagram рисует счётчик просмотров прямо на превью — ищем любой
            // дочерний элемент с числовым текстом внутри ссылки-плитки.
            const allChildren = anchor.querySelectorAll('span, div');
            for (const el of allChildren) {
                const txt = (el.innerText || el.textContent || '').trim();
                // Текст вида "1 234", "12K", "1.2M" — только цифры/буквы K/M
                if (/^[\d\s.,]+[KkMmкКм]?$/.test(txt) && txt.length > 0 && txt.length < 12) {
                    const n = parseAbbrev(txt.replace(/\s/g, ''));
                    if (n > 100) return n;  // Отсекаем случайный шум (1-2 цифры)
                }
            }

            // Fallback: overlay с linear-gradient (старый способ)
            const overlays = anchor.querySelectorAll('div[style*="linear-gradient"], div[style]');
            for (const ov of overlays) {
                const style = ov.getAttribute('style') || '';
                if (!style.includes('linear-gradient') && !ov.innerText) continue;
                const raw = (ov.innerText || '').trim();
                if (!raw) continue;
                const n = parseAbbrev(raw.replace(/[^0-9.,KkMmкКм]/g, ''));
                if (n > 0) return n;
            }
            return 0;
        }"""

        js_last_href = """() => {
            const links = document.querySelectorAll('a[href*="/reel/"]');
            return links.length > 0 ? links[links.length-1].getAttribute('href') : '';
        }"""
        js_count_links = """() => document.querySelectorAll('a[href*="/reel/"]').length"""

        tile_views: int = 0
        max_scrolls: int = 30
        prev_last_href: str = ""
        same_state_count: int = 0  # Сколько раз подряд DOM не обновился

        for scroll_idx in range(max_scrolls):
            # Проверяем — появилась ли нужная плитка
            try:
                found = await page.evaluate(js_has_tile, shortcode)
            except Exception:
                found = False

            if found:
                try:
                    tile_views = await page.evaluate(js_tile, shortcode)
                except Exception as e:
                    logger.debug(f"[ReelsViews] js_tile error: {e}")
                    tile_views = 0

                if tile_views > 0:
                    logger.info(
                        f"[ReelsViews] Плитка найдена (скролл {scroll_idx}): "
                        f"{tile_views:,} просмотров"
                    )
                    break

                # Плитка в DOM, но overlay ещё не отрисован — ждём и повторяем
                logger.debug(
                    f"[ReelsViews] Плитка есть, overlay views=0, "
                    f"ждём отрисовки (скролл {scroll_idx})"
                )
                await page.wait_for_timeout(random.randint(800, 1_200))
                try:
                    tile_views = await page.evaluate(js_tile, shortcode)
                    if tile_views > 0:
                        logger.info(
                            f"[ReelsViews] Overlay появился после ожидания: "
                            f"{tile_views:,} просмотров"
                        )
                        break
                except Exception:
                    pass

            # Скроллим вниз — небольшими шагами для виртуального скролла
            await page.evaluate("window.scrollBy(0, 600)")
            await page.wait_for_timeout(random.randint(1_500, 2_500))

            # Дополнительный скролл если страница большая
            await page.evaluate("window.scrollBy(0, 600)")
            await page.wait_for_timeout(random.randint(800, 1_200))

            # Проверяем прогресс по маркеру — href последней ссылки
            try:
                curr_last_href = await page.evaluate(js_last_href)
            except Exception:
                curr_last_href = ""

            if curr_last_href == prev_last_href:
                same_state_count += 1
                logger.debug(
                    f"[ReelsViews] DOM не обновился (скролл {scroll_idx}, "
                    f"повтор #{same_state_count})"
                )
                if same_state_count >= 4:
                    # DOM не обновляется 4 раза подряд — достигли конца вкладки
                    logger.debug(
                        f"[ReelsViews] Конец вкладки /reels/ на скролле {scroll_idx}, "
                        f"shortcode {shortcode} не найден"
                    )
                    break
            else:
                same_state_count = 0  # Новые элементы появились — сбрасываем счётчик

            prev_last_href = curr_last_href

        if tile_views > 0:
            logger.info(f"[ReelsViews] overlay /reels/: {tile_views:,}")
            views = tile_views

        if views == 0:
            logger.warning(f"[ReelsViews] Просмотры не найдены: {shortcode} (@{username})")
        else:
            logger.success(f"[ReelsViews] @{username}/{shortcode} -> {views:,} просмотров")

        return views

    # ------------------------------------------------------------------
    # Главная точка входа
    # ------------------------------------------------------------------
    async def parse(self, keyword: str, ml_processor=None) -> List[Dict[str, Any]]:
        parsed_results = []
        attempt = 0
        max_attempts = 3

        while attempt < max_attempts:
            async with async_playwright() as p:
                storage_state = SESSION_FILE if os.path.exists(SESSION_FILE) else None

                browser = await p.chromium.launch(
                    headless=False,
                    args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--disable-blink-features=AutomationControlled",
                        "--disable-web-security",
                        "--disable-features=IsolateOrigins,site-per-process",
                    ],
                )
                context = await browser.new_context(
                    user_agent=random.choice(USER_AGENTS),
                    viewport={"width": 1280, "height": 800},
                    storage_state=storage_state,
                    locale="en-US",
                    timezone_id="Europe/Amsterdam",
                    extra_http_headers={
                        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                        "Accept-Encoding": "gzip, deflate, br",
                        "Accept-Language": "en-US,en;q=0.9",
                        "DNT": "1",
                        "Referer": "https://www.instagram.com/",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "none",
                        "Sec-Fetch-User": "?1",
                        "Upgrade-Insecure-Requests": "1",
                    }
                )
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                    window.chrome = { runtime: {} };
                """)
                page = await context.new_page()

                try:
                    if not storage_state:
                        logger.info("[Instagram] Сессия не найдена — логинимся...")
                        login_ok = await self._login(page)
                        if not login_ok:
                            await browser.close()
                            attempt += 1
                            continue
                    else:
                        logger.info(f"[Instagram] Загружаем сессию из {SESSION_FILE}")
                        try:
                            await page.goto("https://www.instagram.com/", timeout=50000, wait_until="domcontentloaded")
                        except Exception as e:
                            error_str = str(e).lower()
                            if "429" in error_str or "err_http_response_code_failure" in error_str:
                                logger.warning("[Instagram] Блокировка при открытии главной")
                                await self.vpn_rotator.rotate_ip()
                                try:
                                    await page.goto("https://www.instagram.com/", timeout=50000, wait_until="domcontentloaded")
                                except Exception:
                                    await browser.close()
                                    attempt += 1
                                    continue

                        await page.wait_for_timeout(random.randint(2500, 3500))
                        if "login" in page.url or await page.locator("input[name='username']").count() > 0:
                            logger.warning("[Instagram] Сессия протухла — логинимся заново...")
                            try:
                                os.remove(SESSION_FILE)
                            except Exception:
                                pass
                            await browser.close()
                            continue

                    raw = keyword.strip()
                    cleaned = re.sub(r"[#\W]+", " ", raw).strip()
                    seen = set()
                    variants = []
                    for c in [cleaned.replace(" ", ""), cleaned.replace(" ", "_"),
                               cleaned.lower(), raw.replace(" ", "").lower()]:
                        n = c.lower().strip()
                        if n and n not in seen:
                            seen.add(n)
                            variants.append(n)
                    if not variants:
                        variants = [raw]

                    urls = []
                    # Мапа для хранения связки url -> количество просмотров из плитки тегов
                    url_views_map = {}
                    got_429 = False

                    for tag in variants:
                        tag_url = f"https://www.instagram.com/explore/tags/{quote(tag, safe='')}/"
                        logger.info(f"[Instagram] Открываю тег: #{tag}")

                        try:
                            response = await page.goto(tag_url, timeout=60000, wait_until="domcontentloaded")
                        except Exception as e:
                            error_str = str(e).lower()
                            if "429" in error_str or "err_http_response_code_failure" in error_str:
                                logger.warning(f"[Instagram] 429/блокировка при открытии тега #{tag}")
                                await self.vpn_rotator.rotate_ip()
                                try:
                                    response = await page.goto(tag_url, timeout=60000, wait_until="domcontentloaded")
                                except Exception:
                                    got_429 = True
                                    break
                            else:
                                logger.warning(f"[Instagram] Ошибка при открытии тега #{tag}: {error_str[:100]}")
                                break

                        await page.wait_for_timeout(random.randint(2000, 3500))

                        if response and response.status == 429:
                            got_429 = True
                            break

                        # Ждём появления постов — пробуем все возможные селекторы
                        for sel in [
                            'a[href*="/reel/"]',
                            'a[href*="/p/"]',
                            'article a',
                            'main a[href*="/"]',
                        ]:
                            try:
                                await page.wait_for_selector(sel, timeout=8000)
                                break
                            except Exception:
                                continue

                        # Дополнительная пауза для JS-рендеринга плитки
                        await page.wait_for_timeout(random.randint(2000, 3000))
                        await self._chaotic_scroll(page)
                        await page.wait_for_timeout(2500)

                        # Извлекаем ссылки вместе со счетчиком просмотров из элементов плитки
                        posts_data = await page.evaluate(
                            r"""() => {"""
                            + _JS_PARSE_ABBREV
                            + r"""
                                const results = [];
                                const seen = new Set();
                                const links = document.querySelectorAll(
                                    'a[href*="/reel/"], a[href*="/p/"], a[href*="/tv/"]'
                                );
                                links.forEach(a => {
                                    const href = a.href;
                                    if (!href || seen.has(href)) return;
                                    if (!/\/(reel|p|tv)\/[A-Za-z0-9_-]{5,}/.test(href)) return;
                                    seen.add(href);
                                    let views = 0;
                                    const aria = a.getAttribute('aria-label') || '';
                                    if (/views|просмотр/i.test(aria)) {
                                        views = parseAbbrev(aria.replace(/[^0-9.,KkMmкКм]/g, ''));
                                    }
                                    if (!views) {
                                        const overlay = a.querySelector('div[style*="linear-gradient"]');
                                        if (overlay) {
                                            const raw = (overlay.innerText || '').trim();
                                            views = parseAbbrev(raw.replace(/[^0-9.,KkMmкКм]/g, ''));
                                        }
                                    }
                                    if (!views) {
                                        const spans = a.querySelectorAll('span');
                                        for (const sp of spans) {
                                            const t = (sp.innerText || '').trim();
                                            if (/^[\d.,]+[KkMmкКм]?$/.test(t)) {
                                                views = parseAbbrev(t);
                                                if (views > 0) break;
                                            }
                                        }
                                    }
                                    results.push({ href: href, views_count: views });
                                });
                                return results;
                            }"""
                        )

                        for item in posts_data:
                            href = item["href"]
                            if href and href not in urls:
                                urls.append(href)
                                url_views_map[href] = item["views_count"]

                        if urls:
                            logger.info(f"[Instagram] #{tag} → {len(urls)} постов.")
                            break
                        logger.warning(f"[Instagram] #{tag} — постов нет, пробую следующий вариант.")

                    if got_429:
                        await browser.close()
                        await self.vpn_rotator.rotate_ip()
                        attempt += 1
                        continue

                    urls = list(dict.fromkeys(urls))[:MAX_VIDEOS_PER_KEYWORD]

                    if not urls:
                        logger.warning("[Instagram] Постов не найдено.")
                        await browser.close()
                        return parsed_results

                    logger.info(f"[Instagram] Запускаю {PARALLEL_WORKERS} воркеров для {len(urls)} постов...")

                    # Выполнение параллельного сбора метаданных постов и скачивания роликов
                    tasks = [
                        self._process_single_post(
                            context, v_url, keyword, idx, ml_processor, self.vpn_rotator,
                            views_count=url_views_map.get(v_url, 0)
                        )
                        for idx, v_url in enumerate(urls)
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    for r in results:
                        if isinstance(r, dict):
                            parsed_results.append(r)

                    # ── ЭТАП 2: параллельный сбор профилей и просмотров ────────────
                    # Каждый профиль и каждый поиск просмотров открывается
                    # в отдельной вкладке — столько вкладок сколько PARALLEL_WORKERS.
                    if parsed_results:
                        logger.info(
                            f"[Instagram] Посты собраны. Запускаю {PARALLEL_WORKERS} "
                            f"воркеров для профилей и просмотров..."
                        )

                        # Группируем посты по авторам
                        author_posts_map: dict = {}
                        for r in parsed_results:
                            uname = r.get("author_username")
                            vid   = r.get("video_id")
                            if uname and uname != "unknown" and vid:
                                author_posts_map.setdefault(uname, []).append(vid)

                        profiles_cache: dict = {}
                        profile_lock = asyncio.Lock()

                        # ── Воркер профиля: открывает свою вкладку ──────────────────
                        async def _profile_worker(username: str, shortcodes: list):
                            pg = await context.new_page()
                            try:
                                logger.info(f"[Instagram] Парсим профиль: @{username}")
                                followers, following, posts, bio, verified, views_map =                                     await self._extract_profile_data(pg, username, target_shortcodes=shortcodes)
                                async with profile_lock:
                                    profiles_cache[username] = {
                                        "followers": followers, "following": following,
                                        "posts": posts, "bio": bio, "verified": verified,
                                        "accurate_views": views_map,
                                    }
                            except Exception as e:
                                logger.warning(f"[Instagram] Ошибка профиля @{username}: {e}")
                            finally:
                                try:
                                    await pg.close()
                                except Exception:
                                    pass

                        # Запускаем профили параллельно батчами по PARALLEL_WORKERS
                        unique_authors = list(author_posts_map.keys())
                        for i in range(0, len(unique_authors), PARALLEL_WORKERS):
                            batch = unique_authors[i:i + PARALLEL_WORKERS]
                            await asyncio.gather(*[
                                _profile_worker(u, author_posts_map[u]) for u in batch
                            ])

                        # Применяем данные профилей к результатам
                        for r in parsed_results:
                            username = r.get("author_username")
                            video_id = r.get("video_id")
                            if username in profiles_cache:
                                c = profiles_cache[username]
                                r["followers_count"] = c["followers"]
                                r["following_count"] = c["following"]
                                r["total_posts"]     = c["posts"]
                                r["bio"]             = c["bio"]
                                r["is_verified"]     = c["verified"]
                                pv = c["accurate_views"].get(video_id, 0)
                                if pv > 0:
                                    r["views_count"] = pv
                                r["match_percentage"] = _compute_match_percentage(_build_text_blob(r))

                        # ── Воркер просмотров: скроллит /reels/ в своей вкладке ─────
                        # Только для постов где views_count всё ещё 0
                        need_views = [
                            r for r in parsed_results
                            if r.get("views_count", 0) == 0
                               and r.get("author_username", "unknown") != "unknown"
                               and r.get("video_id")
                               # Исключаем фото: просмотры есть только у Reels
                               and "/p/" not in r.get("video_url", "")
                        ]

                        if need_views:
                            logger.info(
                                f"[Instagram] Ищем просмотры параллельно "
                                f"для {len(need_views)} постов..."
                            )

                            views_lock = asyncio.Lock()

                            async def _views_worker(record: dict):
                                uname   = record.get("author_username", "")
                                vid     = record.get("video_id", "")
                                v_url   = record.get("video_url", "")

                                # ── Фильтр: пропускаем фото (/p/) ────────────────────
                                # Просмотры существуют только у Reels (/reel/).
                                # У обычных фото (/p/) счётчика просмотров нет.
                                if "/p/" in v_url and "/reel/" not in v_url:
                                    logger.debug(
                                        f"[ReelsViews] Пропуск фото (не Reels): {v_url}"
                                    )
                                    return

                                pg = await context.new_page()
                                try:
                                    v = await self.get_reel_views_by_shortcode(pg, uname, vid)
                                    if v > 0:
                                        async with views_lock:
                                            record["views_count"] = v
                                            record["match_percentage"] = _compute_match_percentage(
                                                _build_text_blob(record)
                                            )
                                except Exception as e:
                                    logger.debug(f"[ReelsViews] worker error {vid}: {e}")
                                finally:
                                    try:
                                        await pg.close()
                                    except Exception:
                                        pass

                            # Батчи по PARALLEL_WORKERS
                            for i in range(0, len(need_views), PARALLEL_WORKERS):
                                batch = need_views[i:i + PARALLEL_WORKERS]
                                await asyncio.gather(*[_views_worker(r) for r in batch])

                    logger.success(f"[Instagram] Готово. Собрано и обогащено {len(parsed_results)} записей.")
                    await browser.close()
                    return parsed_results

                except Exception as e:
                    logger.error(f"[Instagram] Ошибка сессии: {e}. Ротация IP...")
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    await self.vpn_rotator.rotate_ip()
                    attempt += 1
                    await asyncio.sleep(5)

        logger.error("[Instagram] Все попытки исчерпаны.")
        return parsed_results