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
    TIKTOK_USERNAME, TIKTOK_PASSWORD,
    PARALLEL_WORKERS,
)
from vpn_rotator import VPNRotator

# Файл сессии TikTok — рядом с instagram_session.json
import pathlib
# tiktok.py лежит в parsers/, поэтому сессию ищем на уровень выше —
# в корне проекта, рядом с main.py (там же, где и instagram_session.json).
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
TIKTOK_SESSION_FILE = str(_PROJECT_ROOT / "tiktok_session.json")
# Хранит UA, с которым была создана сессия — TikTok привязывает cookies
# к отпечатку браузера, и смена UA между запусками сбрасывает на логин.
TIKTOK_UA_FILE = str(_PROJECT_ROOT / "tiktok_session_ua.txt")

# Ключевые слова для скоринга мошенничества — те же что в instagram.py
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
    score = min(100.0, round((1 - math.exp(-hits * 0.45)) * 100, 1))
    return score


class TikTokParser:
    def __init__(self, vpn_rotator: VPNRotator):
        self.vpn_rotator = vpn_rotator
        self._semaphore = asyncio.Semaphore(PARALLEL_WORKERS)

    # ------------------------------------------------------------------
    # Сохраняем cookies сессии ВМЕСТЕ с UA, под которым логинились —
    # иначе при следующем запуске случайный UA рассинхронится с сессией
    # и TikTok сбросит на логин.
    # ------------------------------------------------------------------
    async def _save_session(self, page: Page):
        await page.context.storage_state(path=TIKTOK_SESSION_FILE)
        try:
            current_ua = await page.evaluate("() => navigator.userAgent")
            with open(TIKTOK_UA_FILE, "w", encoding="utf-8") as f:
                f.write(current_ua or "")
        except Exception as e:
            logger.debug(f"[TikTok] Не удалось сохранить UA сессии: {e}")

    # ------------------------------------------------------------------
    # Человекоподобное движение мыши по кривой Безье
    # ------------------------------------------------------------------
    async def _simulate_human_mouse(
        self, page: Page,
        from_x: int, from_y: int,
        to_x: int, to_y: int,
    ):
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
                await page.evaluate(
                    f"window.scrollBy(0, -{int(scroll_amount * 0.3)})"
                )
                await asyncio.sleep(random.uniform(0.3, 0.8))

    # ------------------------------------------------------------------
    # Принять куки / баннеры TikTok
    # ------------------------------------------------------------------
    async def _accept_cookies(self, page: Page):
        for selector in [
            "button:has-text('Accept all')",
            "button:has-text('Accept All')",
            "button:has-text('Allow all')",
            "button:has-text('Принять все')",
            "[data-e2e='cookie-banner-accept']",
            "button[class*='cookie']",
            "button[id*='accept']",
        ]:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    logger.info("[TikTok] Куки приняты.")
                    await asyncio.sleep(1)
                    return
            except Exception:
                continue

    # ------------------------------------------------------------------
    # Закрыть попап входа/регистрации если появился
    # ------------------------------------------------------------------
    async def _close_login_popup(self, page: Page):
        for selector in [
            "[data-e2e='modal-close-inner-button']",
            "button[aria-label='Close']",
            "button:has-text('Not now')",
            "button:has-text('Не сейчас')",
            "div[class*='close']:visible",
        ]:
            try:
                btn = page.locator(selector).first
                if await btn.is_visible(timeout=1500):
                    await btn.click()
                    logger.debug("[TikTok] Попап закрыт.")
                    await asyncio.sleep(0.5)
                    return
            except Exception:
                continue


    # ------------------------------------------------------------------
    # Капча TikTok: детекция + ожидание ручного решения
    # ------------------------------------------------------------------
    _CAPTCHA_SELECTORS = [
        "[class*='captcha']",
        "[id*='captcha']",
        "[class*='secsdk']",
        "[class*='captcha-verify']",
        "div:has-text('Drag the puzzle piece')",
        "div:has-text('Sliding puzzle')",
        "div:has-text('Verify to continue')",
        "div:has-text('Перетащите')",
        "div:has-text('Подтвердите')",
        "iframe[src*='captcha']",
        "[class*='TUXButton']:has-text('Refresh')",
    ]
    _SLIDER_SELECTORS = [
        "[class*='captcha-slider']",
        "[class*='secsdk-captcha-drag']",
        "div[class*='drag-icon']",
        "div[style*='cursor: pointer'][class*='captcha']",
        "div[class*='slider-btn']",
        "[class*='secsdk-captcha-drag-icon']",
    ]

    async def _is_captcha_visible(self, page: Page) -> bool:
        for sel in self._CAPTCHA_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=800):
                    return True
            except Exception:
                pass
        try:
            for frame in page.frames:
                url = (frame.url or "").lower()
                if "captcha" in url or "verify" in url:
                    return True
        except Exception:
            pass
        return False

    async def _wait_for_captcha_cleared(
        self, page: Page, timeout_sec: int = 120, label: str = "капча"
    ) -> bool:
        logger.warning("=" * 60)
        logger.warning(f"[TikTok] ОБНАРУЖЕНА {label.upper()}!")
        logger.warning("  Решите капчу в окне браузера (слайдер / puzzle / verify).")
        logger.warning(f"  Ожидание до {timeout_sec} секунд...")
        logger.warning("=" * 60)

        for tick in range(timeout_sec // 2):
            if not await self._is_captcha_visible(page):
                logger.success("[TikTok] Капча пройдена.")
                await page.wait_for_timeout(1500)
                return True
            await asyncio.sleep(2)
            if tick > 0 and tick % 15 == 0:
                logger.warning(f"[TikTok] Всё ещё жду решение капчи... ({tick * 2}с)")

        logger.error("[TikTok] Таймаут ожидания капчи.")
        return False

    async def _try_slider_captcha(self, page: Page) -> bool:
        slider = None
        for sel in self._SLIDER_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=1500):
                    slider = el
                    break
            except Exception:
                pass
        if slider is None:
            return False

        try:
            box = await slider.bounding_box()
            if not box:
                return False

            start_x = box["x"] + box["width"] / 2
            start_y = box["y"] + box["height"] / 2
            track_width = 260
            try:
                track_el = page.locator(
                    "[class*='captcha-slider-track'], "
                    "[class*='secsdk-captcha-drag-wrapper'], "
                    "[class*='drag-bar']"
                ).first
                track_box = await track_el.bounding_box()
                if track_box:
                    track_width = int(track_box["width"] * 0.85)
            except Exception:
                pass

            await page.mouse.move(start_x, start_y)
            await asyncio.sleep(random.uniform(0.3, 0.6))
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.1, 0.3))

            steps = random.randint(22, 38)
            for i in range(steps):
                progress = i / steps
                eased = (1 - math.cos(progress * math.pi)) / 2
                offset = eased * track_width
                jitter_y = random.uniform(-2, 2)
                await page.mouse.move(start_x + offset, start_y + jitter_y, steps=1)
                delay = random.uniform(0.012, 0.045)
                if progress > 0.75:
                    delay *= 2.0
                await asyncio.sleep(delay)

            await asyncio.sleep(random.uniform(0.2, 0.5))
            await page.mouse.up()
            await asyncio.sleep(2.5)
            return not await self._is_captcha_visible(page)
        except Exception as e:
            logger.debug(f"[TikTok] slider attempt failed: {e}")
            return False

    async def _solve_captcha(self, page: Page) -> bool:
        """Детектирует капчу, пробует auto-drag, затем ждёт ручного решения."""
        if not await self._is_captcha_visible(page):
            return False

        logger.warning("[TikTok] Капча обнаружена — пробую auto-drag...")
        if await self._try_slider_captcha(page):
            logger.info("[TikTok] Auto-drag помог, капча снята.")
            return True

        for refresh_sel in [
            "button:has-text('Refresh')",
            "button:has-text('Обновить')",
            "[class*='refresh']",
        ]:
            try:
                btn = page.locator(refresh_sel).first
                if await btn.is_visible(timeout=1000):
                    await btn.click()
                    await asyncio.sleep(2)
                    break
            except Exception:
                pass

        return await self._wait_for_captcha_cleared(page, timeout_sec=120)

    # ------------------------------------------------------------------
    # Ручной режим — ждём пока юзер пройдёт капчу/2FA
    # ------------------------------------------------------------------
    async def _wait_for_manual_login(self, page: Page) -> bool:
        logger.warning("=" * 60)
        logger.warning("[TikTok] РУЧНОЙ РЕЖИМ! Действия в окне браузера:")
        logger.warning("  1. Прими куки если есть баннер")
        logger.warning("  2. Войди в аккаунт")
        logger.warning("  3. Пройди капчу если появится")
        logger.warning("  4. Дойди до главной страницы TikTok")
        logger.warning("  У тебя 5 минут.")
        logger.warning("=" * 60)

        blocked = ("login", "signup", "register", "captcha", "challenge")
        for _ in range(150):
            await asyncio.sleep(2)
            try:
                url = page.url.lower()
            except Exception:
                logger.error("[TikTok] Окно браузера закрыто.")
                return False
            if all(m not in url for m in blocked) and "tiktok.com" in url:
                logger.success("[TikTok] Ручной вход прошёл успешно!")
                await page.wait_for_timeout(3000)
                return True

        logger.error("[TikTok] Таймаут ручного входа.")
        return False

    # ------------------------------------------------------------------
    # Авторизация
    # ------------------------------------------------------------------
    async def _login(self, page: Page) -> bool:
        if not TIKTOK_USERNAME or not TIKTOK_PASSWORD:
            logger.warning(
                "[TikTok] TIKTOK_USERNAME / TIKTOK_PASSWORD не заданы — "
                "переходим в ручной режим..."
            )
            # Баг: раньше тут сразу звали _wait_for_manual_login без перехода
            # на сайт — окно браузера оставалось на about:blank, и войти было
            # просто некуда. Сначала открываем страницу логина TikTok.
            try:
                await page.goto(
                    "https://www.tiktok.com/login/phone-or-email/email",
                    timeout=60000, wait_until="domcontentloaded",
                )
                await page.wait_for_timeout(2000)
                await self._accept_cookies(page)
            except Exception as e:
                logger.warning(
                    f"[TikTok] Не удалось открыть страницу логина: {e}"
                )
            ok = await self._wait_for_manual_login(page)
            if ok:
                await self._save_session(page)
            return ok

        try:
            logger.info("[TikTok] Открываем страницу входа...")
            await page.goto(
                "https://www.tiktok.com/login/phone-or-email/email",
                timeout=60000, wait_until="domcontentloaded",
            )
            await page.wait_for_timeout(3000)
            await self._accept_cookies(page)
            await page.wait_for_timeout(1500)

            # Проверяем — не появился ли сразу запрос 2FA или капча
            url = page.url.lower()
            if any(x in url for x in ("captcha", "challenge", "verify")):
                logger.warning("[TikTok] Сразу потребовалась верификация.")
                ok = await self._wait_for_manual_login(page)
                if ok:
                    await self._save_session(page)
                return ok

            # Ищем поле email
            email_input = None
            for sel in [
                "input[name='username']",
                "input[type='email']",
                "input[placeholder*='email' i]",
                "input[placeholder*='Phone' i]",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=3000):
                        email_input = el
                        break
                except Exception:
                    continue

            # Ищем поле пароля
            password_input = None
            for sel in [
                "input[type='password']",
                "input[name='password']",
                "input[placeholder*='password' i]",
            ]:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=3000):
                        password_input = el
                        break
                except Exception:
                    continue

            if not email_input or not password_input:
                logger.warning(
                    "[TikTok] Поля ввода не найдены — переходим в ручной режим..."
                )
                ok = await self._wait_for_manual_login(page)
                if ok:
                    await self._save_session(page)
                return ok

            await self._human_type(page, email_input, TIKTOK_USERNAME)
            await asyncio.sleep(random.uniform(0.8, 1.5))
            await self._human_type(page, password_input, TIKTOK_PASSWORD)
            await asyncio.sleep(random.uniform(0.8, 1.5))

            # Кнопка входа
            login_btn = None
            for sel in [
                "button[type='submit']",
                "button:has-text('Log in')",
                "button:has-text('Войти')",
                "[data-e2e='login-button']",
            ]:
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
                    await self._simulate_human_mouse(
                        page, 640, 400,
                        int(box["x"] + box["width"] / 2),
                        int(box["y"] + box["height"] / 2),
                    )
                await login_btn.click()
            else:
                await password_input.press("Enter")

            await page.wait_for_timeout(5000)
            await self._solve_captcha(page)

            url = page.url.lower()
            if any(x in url for x in ("captcha", "challenge", "verify", "two")):
                logger.warning("[TikTok] Требуется верификация после логина.")
                ok = await self._wait_for_manual_login(page)
                if ok:
                    await self._save_session(page)
                    logger.success(f"[TikTok] Сессия сохранена: {TIKTOK_SESSION_FILE}")
                return ok

            if "login" not in url and "tiktok.com" in url:
                logger.success("[TikTok] Авторизация успешна!")
                await self._save_session(page)
                logger.success(f"[TikTok] Сессия сохранена: {TIKTOK_SESSION_FILE}")
                return True

            logger.warning("[TikTok] Неизвестное состояние после логина — ручной режим...")
            ok = await self._wait_for_manual_login(page)
            if ok:
                await self._save_session(page)
            return ok

        except Exception as e:
            logger.error(f"[TikTok] Критическая ошибка при логине: {e}")
            try:
                ok = await self._wait_for_manual_login(page)
                if ok:
                    await self._save_session(page)
                return ok
            except Exception:
                return False

    # ------------------------------------------------------------------
    # Перехват видео через network responses
    # ------------------------------------------------------------------
    async def _intercept_and_download_video(
        self, page: Page, video_id: str
    ) -> str:
        """
        Слушаем сетевые ответы и ловим mp4-файл напрямую.
        TikTok раздаёт видео через CDN — content-type: video/mp4.
        """
        captured: Dict[str, bytes] = {}

        async def handle_response(response: Response):
            url = response.url
            ct = response.headers.get("content-type", "")
            if ".mp4" in url or "video/mp4" in ct or "video/webm" in ct:
                try:
                    body = await response.body()
                    # > 50KB — точно видео, не превью
                    if len(body) > 51200 and len(body) > len(captured.get("data", b"")):
                        captured["data"] = body
                        captured["url"] = url
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

        await page.evaluate("window.scrollBy(0, 100)")
        await asyncio.sleep(1.5)
        page.remove_listener("response", handle_response)

        if "data" in captured:
            local_filename = f"tt_{video_id}.mp4"
            local_path = os.path.join(str(DOWNLOAD_DIR), local_filename)
            try:
                with open(local_path, "wb") as f:
                    f.write(captured["data"])
                logger.info(
                    f"[TikTok] Видео перехвачено: {local_filename} "
                    f"({len(captured['data']) // 1024}KB)"
                )
                return local_path
            except Exception as e:
                logger.warning(f"[TikTok] Не удалось сохранить видео: {e}")
        return ""

    # ------------------------------------------------------------------
    # Извлечение метаданных поста
    # ------------------------------------------------------------------
    async def _extract_post_metadata(
        self, page: Page, keyword: str
    ) -> Dict[str, Any]:
        result = {
            "caption": "",
            "author_username": "",
            "author_display_name": "",
            "likes_count": 0,
            "comments_count": 0,
            "shares_count": 0,
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
            json_ld_text = await page.evaluate(
                """() => {
                    const scripts = Array.from(
                        document.querySelectorAll('script[type="application/ld+json"]')
                    );
                    for (let s of scripts) {
                        const t = s.innerText || s.textContent || "";
                        if (t.includes("author") || t.includes("datePublished")
                            || t.includes("interactionCount")) return t;
                    }
                    return "";
                }"""
            )
            if json_ld_text:
                data = json.loads(json_ld_text)
                main = data[0] if isinstance(data, list) else data

                result["caption"] = (
                    main.get("name") or main.get("description") or ""
                )
                result["publish_date"] = (
                    main.get("datePublished") or main.get("uploadDate") or ""
                )

                author = main.get("author", {})
                if isinstance(author, dict):
                    result["author_username"] = (
                        author.get("identifier") or
                        author.get("alternateName") or ""
                    ).lstrip("@")
                    result["author_display_name"] = (
                        author.get("name") or result["author_username"]
                    )

                for stat in main.get("interactionStatistic", []):
                    itype = stat.get("interactionType", "")
                    count = int(stat.get("userInteractionCount", 0) or 0)
                    if "LikeAction" in itype:
                        result["likes_count"] = count
                    elif "CommentAction" in itype:
                        result["comments_count"] = count
                    elif "WatchAction" in itype or "PlayAction" in itype:
                        result["views_count"] = count

        except Exception as e:
            logger.debug(f"[TikTok] JSON-LD parse error: {e}")

        # ── 2. DOM — username, stats, audio ──────────────────────────
        try:
            dom_data = await page.evaluate(
                r"""() => {
                    const out = {
                        username: "", display_name: "", publish_date: "",
                        video_duration: 0, views: 0, likes: 0,
                        comments: 0, shares: 0,
                        audio_id: "", audio_title: "", audio_artist: "",
                        audio_usage_count: 0
                    };

                    // username из URL
                    const urlMatch = location.pathname.match(/\/@([\w.]+)\//);
                    if (urlMatch) out.username = urlMatch[1];

                    // display name
                    const nameEl = document.querySelector(
                        '[data-e2e="browse-username"], '
                        + '[data-e2e="video-author-uniqueid"], '
                        + 'h1[data-e2e*="author"]'
                    );
                    if (nameEl) out.display_name = nameEl.innerText.trim();

                    // Просмотры
                    const viewsEl = document.querySelector(
                        '[data-e2e="video-views"], '
                        + 'strong[data-e2e="video-views"], '
                        + '[class*="video-count"]'
                    );
                    if (viewsEl) {
                        const raw = (viewsEl.innerText || "").replace(/[^0-9KMkm.,]/g, "");
                        let n = parseFloat(raw.replace(",", "."));
                        if (raw.toUpperCase().includes("K")) n *= 1000;
                        if (raw.toUpperCase().includes("M")) n *= 1000000;
                        if (!isNaN(n)) out.views = Math.floor(n);
                    }

                    // Лайки
                    const likesEl = document.querySelector(
                        '[data-e2e="like-count"], '
                        + 'strong[data-e2e="like-count"]'
                    );
                    if (likesEl) {
                        const raw = (likesEl.innerText || "").replace(/[^0-9KMkm.,]/g, "");
                        let n = parseFloat(raw.replace(",", "."));
                        if (raw.toUpperCase().includes("K")) n *= 1000;
                        if (raw.toUpperCase().includes("M")) n *= 1000000;
                        if (!isNaN(n)) out.likes = Math.floor(n);
                    }

                    // Комментарии
                    const commEl = document.querySelector(
                        '[data-e2e="comment-count"], '
                        + 'strong[data-e2e="comment-count"]'
                    );
                    if (commEl) {
                        const raw = (commEl.innerText || "").replace(/[^0-9KMkm.,]/g, "");
                        let n = parseFloat(raw.replace(",", "."));
                        if (raw.toUpperCase().includes("K")) n *= 1000;
                        if (raw.toUpperCase().includes("M")) n *= 1000000;
                        if (!isNaN(n)) out.comments = Math.floor(n);
                    }

                    // Репосты / shares
                    const shareEl = document.querySelector(
                        '[data-e2e="share-count"], '
                        + 'strong[data-e2e="share-count"]'
                    );
                    if (shareEl) {
                        const raw = (shareEl.innerText || "").replace(/[^0-9KMkm.,]/g, "");
                        let n = parseFloat(raw.replace(",", "."));
                        if (raw.toUpperCase().includes("K")) n *= 1000;
                        if (raw.toUpperCase().includes("M")) n *= 1000000;
                        if (!isNaN(n)) out.shares = Math.floor(n);
                    }

                    // Длительность видео
                    const videoEl = document.querySelector("video");
                    if (videoEl && videoEl.duration && isFinite(videoEl.duration)) {
                        out.video_duration = Math.round(videoEl.duration * 10) / 10;
                    }

                    // Аудио
                    const audioLink = document.querySelector(
                        'a[href*="/music/"], [data-e2e="video-music"]'
                    );
                    if (audioLink) {
                        const href = audioLink.getAttribute("href") || "";
                        const idMatch = href.match(/\/music\/[^-]+-(\d+)/);
                        if (idMatch) out.audio_id = idMatch[1];
                        const txt = (audioLink.innerText || "").trim();
                        const parts = txt.split(" - ");
                        if (parts.length >= 2) {
                            out.audio_title = parts[0].trim();
                            out.audio_artist = parts[1].trim();
                        } else {
                            out.audio_title = txt;
                        }
                    }

                    // Количество видео, использующих этот звук (best-effort) —
                    // на странице самого видео TikTok иногда показывает счётчик
                    // рядом со ссылкой на музыку (напр. "12.3K videos").
                    const audioUsageEl = document.querySelector(
                        '[data-e2e="video-music"] ~ span, '
                        + 'a[href*="/music/"] ~ span, '
                        + '[class*="SoundMusic"] [class*="count" i]'
                    );
                    if (audioUsageEl) {
                        const raw = (audioUsageEl.innerText || "")
                            .replace(/[^0-9KMkm.,]/g, "");
                        let n = parseFloat(raw.replace(",", "."));
                        if (raw.toUpperCase().includes("K")) n *= 1000;
                        if (raw.toUpperCase().includes("M")) n *= 1000000;
                        if (!isNaN(n) && n > 0) out.audio_usage_count = Math.floor(n);
                    }

                    // Дата публикации
                    const timeEl = document.querySelector("time[datetime]");
                    if (timeEl) out.publish_date = timeEl.getAttribute("datetime") || "";

                    return out;
                }"""
            )

            if dom_data.get("username") and not result["author_username"]:
                result["author_username"] = dom_data["username"]
            if dom_data.get("display_name") and not result["author_display_name"]:
                result["author_display_name"] = dom_data["display_name"]
            if dom_data.get("publish_date") and not result["publish_date"]:
                result["publish_date"] = dom_data["publish_date"]
            if dom_data.get("video_duration", 0) > 0 and result["video_duration"] == 0.0:
                result["video_duration"] = dom_data["video_duration"]
            if dom_data.get("views", 0) > 0 and result["views_count"] == 0:
                result["views_count"] = dom_data["views"]
            if dom_data.get("likes", 0) > 0 and result["likes_count"] == 0:
                result["likes_count"] = dom_data["likes"]
            if dom_data.get("comments", 0) > 0 and result["comments_count"] == 0:
                result["comments_count"] = dom_data["comments"]
            if dom_data.get("shares", 0) > 0 and result["shares_count"] == 0:
                result["shares_count"] = dom_data["shares"]
            if dom_data.get("audio_id"):
                result["audio_id"] = dom_data["audio_id"]
            if dom_data.get("audio_title"):
                result["audio_title"] = dom_data["audio_title"]
            if dom_data.get("audio_artist"):
                result["audio_artist"] = dom_data["audio_artist"]
            if dom_data.get("audio_usage_count", 0) > 0:
                result["audio_usage_count"] = dom_data["audio_usage_count"]

        except Exception as e:
            logger.debug(f"[TikTok] DOM metadata error: {e}")

        # ── 3. og:description fallback ────────────────────────────────
        try:
            meta_desc = await page.evaluate(
                "() => document.querySelector("
                "'meta[property=\"og:description\"]')?.content || ''"
            )
            if meta_desc and not result["caption"]:
                result["caption"] = meta_desc

            if not result["author_username"]:
                m = re.search(r'@([\w.]+)', meta_desc)
                if m:
                    result["author_username"] = m.group(1)
        except Exception:
            pass

        # ── 4. og:title как последний fallback caption ────────────────
        if not result["caption"]:
            try:
                result["caption"] = await page.evaluate(
                    "() => document.querySelector('meta[property=\"og:title\"]')"
                    "?.content || document.title || ''"
                )
            except Exception:
                result["caption"] = f"TikTok #{keyword}"

        if not result["author_display_name"] and result["author_username"]:
            result["author_display_name"] = result["author_username"]

        return result

    # ------------------------------------------------------------------
    # Извлечение данных профиля автора
    # ------------------------------------------------------------------
    async def _extract_profile_data(
        self, page: Page, username: str
    ) -> Tuple[int, int, int, str, int, dict]:
        followers_count = 0
        following_count = 0
        total_posts = 0
        bio = ""
        is_verified = 0
        already_watched_views: dict = {}

        try:
            try:
                await page.goto(
                    f"https://www.tiktok.com/@{username}",
                    timeout=50000, wait_until="domcontentloaded",
                )
            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "err_http_response_code_failure" in error_str:
                    logger.warning(
                        f"[TikTok] Блокировка при открытии профиля @{username}"
                    )
                    await self.vpn_rotator.rotate_ip()
                    try:
                        await page.goto(
                            f"https://www.tiktok.com/@{username}",
                            timeout=50000, wait_until="domcontentloaded",
                        )
                    except Exception:
                        logger.warning(
                            f"[TikTok] Не удалось загрузить профиль @{username}"
                        )
                        return followers_count, following_count, total_posts, bio, is_verified, already_watched_views
                else:
                    logger.warning(
                        f"[TikTok] Ошибка профиля @{username}: {error_str[:80]}"
                    )
                    return followers_count, following_count, total_posts, bio, is_verified, already_watched_views

            await page.wait_for_timeout(random.randint(2000, 3500))

            # ── "Уже смотрели" — TikTok помечает просмотренные видео ───
            # Если на странице профиля есть плитка с надписью "Уже смотрели" /
            # "Already watched", значит это видео мы уже открывали. TikTok
            # показывает счётчик просмотров прямо на превью — читаем его.
            already_watched_views: dict = {}  # video_id -> views
            try:
                watched_data = await page.evaluate(r"""() => {
                    function parseAbbrev(raw) {
                        if (!raw) return 0;
                        const s = String(raw).trim().replace(/\u00a0/g,'').replace(/,/g,'.').toLowerCase()
                            .replace(/к/g,'k').replace(/м/g,'m').replace(/тыс/g,'k').replace(/млн/g,'m');
                        const m = s.match(/([\d.]+)\s*([km]?)/);
                        if (!m) return 0;
                        let n = parseFloat(m[1]);
                        if (isNaN(n)) return 0;
                        if (m[2]==='k') n *= 1000;
                        if (m[2]==='m') n *= 1000000;
                        return Math.floor(n);
                    }
                    const result = {};
                    // Ищем все плитки на странице профиля
                    const items = document.querySelectorAll('[data-e2e="user-post-item"]');
                    items.forEach(item => {
                        // Проверяем наличие метки "Уже смотрели" / "Already watched"
                        const text = (item.innerText || item.textContent || '').toLowerCase();
                        const isWatched = text.includes('уже смотрели') || text.includes('already watched');
                        if (!isWatched) return;

                        // Достаём ссылку на видео → video_id
                        const link = item.querySelector('a[href*="/video/"]');
                        if (!link) return;
                        const href = link.getAttribute('href') || '';
                        const m = href.match(/\/video\/(\d+)/);
                        if (!m) return;
                        const videoId = m[1];

                        // Счётчик просмотров — strong или span с числом на превью
                        const viewsEl = item.querySelector(
                            'strong[data-e2e="video-views"], ' +
                            '[data-e2e="video-views"], ' +
                            'strong[class*="video-count"], ' +
                            '[class*="video-count"]'
                        );
                        let views = 0;
                        if (viewsEl) {
                            views = parseAbbrev((viewsEl.innerText || '').replace(/[^0-9KMkm.,]/g,''));
                        }
                        if (views === 0) {
                            // Fallback: любой числовой span/div внутри плитки
                            const spans = item.querySelectorAll('span, strong');
                            for (const sp of spans) {
                                const t = (sp.innerText || '').trim();
                                if (/^[\d.,]+\s*[KkMmкКм]?$/.test(t)) {
                                    const n = parseAbbrev(t);
                                    if (n > 10) { views = n; break; }
                                }
                            }
                        }
                        result[videoId] = views;
                    });
                    return result;
                }""")
                if watched_data:
                    already_watched_views = watched_data
                    logger.info(
                        f"[TikTok] 'Уже смотрели' на @{username}: "
                        f"{len(already_watched_views)} видео с просмотрами"
                    )
            except Exception as e:
                logger.debug(f"[TikTok] Ошибка парсинга 'Уже смотрели': {e}")

            profile_data = await page.evaluate(
                r"""() => {
                    const out = {
                        followers: 0, following: 0, posts: 0,
                        bio: "", verified: false
                    };

                    // Подписчики
                    const followersEl = document.querySelector(
                        '[data-e2e="followers-count"]'
                    );
                    if (followersEl) {
                        const raw = (followersEl.innerText || "")
                            .replace(/[^0-9KMkm.,]/g, "");
                        let n = parseFloat(raw.replace(",", "."));
                        if (raw.toUpperCase().includes("K")) n *= 1000;
                        if (raw.toUpperCase().includes("M")) n *= 1000000;
                        if (!isNaN(n)) out.followers = Math.floor(n);
                    }

                    // Подписки
                    const followingEl = document.querySelector(
                        '[data-e2e="following-count"]'
                    );
                    if (followingEl) {
                        const raw = (followingEl.innerText || "")
                            .replace(/[^0-9KMkm.,]/g, "");
                        let n = parseFloat(raw.replace(",", "."));
                        if (raw.toUpperCase().includes("K")) n *= 1000;
                        if (raw.toUpperCase().includes("M")) n *= 1000000;
                        if (!isNaN(n)) out.following = Math.floor(n);
                    }

                    // Количество видео
                    const videoItems = document.querySelectorAll(
                        '[data-e2e="user-post-item"]'
                    );
                    out.posts = videoItems.length;

                    // Bio
                    const bioEl = document.querySelector(
                        '[data-e2e="user-bio"], h2[data-e2e="user-subtitle"]'
                    );
                    if (bioEl) out.bio = bioEl.innerText.trim();

                    // Верификация
                    out.verified = !!(
                        document.querySelector('[data-e2e="verify-badge"]') ||
                        document.querySelector('svg[class*="verified" i]') ||
                        document.querySelector('[class*="Verified"]')
                    );

                    return out;
                }"""
            )

            followers_count = profile_data.get("followers", 0)
            following_count = profile_data.get("following", 0)
            total_posts = profile_data.get("posts", 0)
            bio = profile_data.get("bio", "")
            is_verified = 1 if profile_data.get("verified", False) else 0

            # Fallback через meta description
            if followers_count == 0:
                try:
                    meta = await page.evaluate(
                        "() => document.querySelector("
                        "'meta[name=\"description\"]')?.content || ''"
                    )
                    if meta:
                        m = re.search(r'([\d,.]+[KkMm]?)\s+Followers', meta)
                        if m:
                            raw = m.group(1).replace(",", "")
                            n = float(raw.replace("K", "e3").replace(
                                "M", "e6").replace("k", "e3").replace("m", "e6"))
                            followers_count = int(n)
                except Exception:
                    pass

        except Exception as e:
            logger.warning(f"[TikTok] Ошибка парсинга профиля @{username}: {e}")

        return followers_count, following_count, total_posts, bio, is_verified, already_watched_views

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
        views_count: int = 0,
    ) -> Optional[Dict]:
        async with self._semaphore:
            await asyncio.sleep(worker_idx * random.uniform(0.3, 1.0))
            page = await context.new_page()
            local_path = ""
            try:
                # Реферер — страница тега, снижает вероятность блокировки
                cleaned_tag = re.sub(r"[#\W]+", "", keyword).lower().strip()
                try:
                    await page.goto(
                        f"https://www.tiktok.com/tag/{cleaned_tag}",
                        timeout=30000, wait_until="domcontentloaded",
                    )
                    await page.wait_for_timeout(500)
                except Exception:
                    pass

                # Открываем страницу поста
                try:
                    response = await page.goto(
                        v_url, timeout=60000, wait_until="domcontentloaded"
                    )
                except Exception as goto_error:
                    error_str = str(goto_error).lower()
                    if any(x in error_str for x in [
                        "429", "403", "err_http_response_code_failure", "net::"
                    ]):
                        logger.warning(
                            f"[TikTok Worker {worker_idx}] Блокировка: {error_str[:100]}"
                        )
                        if vpn_rotator:
                            logger.info(
                                f"[TikTok Worker {worker_idx}] Попытка ротации VPN..."
                            )
                            await vpn_rotator.rotate_ip()
                            try:
                                response = await page.goto(
                                    v_url, timeout=60000,
                                    wait_until="domcontentloaded",
                                )
                            except Exception as retry_error:
                                logger.warning(
                                    f"[TikTok Worker {worker_idx}] Повтор не помог: "
                                    f"{str(retry_error)[:80]}"
                                )
                                return None
                        else:
                            return None
                    else:
                        logger.warning(
                            f"[TikTok Worker {worker_idx}] Ошибка {v_url}: "
                            f"{error_str[:80]}"
                        )
                        return None

                if response and response.status in (404, 410, 403):
                    logger.debug(
                        f"[TikTok] Пост недоступен ({response.status}): {v_url}"
                    )
                    return None
                if "login" in page.url.lower():
                    logger.debug(f"[TikTok] Редирект на логин: {v_url}")
                    return None

                # TikTok показывает модальный логин без смены URL —
                # проверяем по DOM-признаку.
                try:
                    login_modal_visible = await page.evaluate(r"""() => {
                        const sels = [
                            '[data-e2e="login-modal"]',
                            'div[class*="LoginModal"]',
                            'div[class*="login-modal"]',
                            'form[action*="login"]',
                        ];
                        return sels.some(s => !!document.querySelector(s));
                    }""")
                    if login_modal_visible:
                        logger.debug(f"[TikTok] Login-модал без смены URL: {v_url}")
                        await self._close_login_popup(page)
                        await page.wait_for_timeout(1500)
                except Exception:
                    pass

                # Ждём появления реального контента.
                # TikTok рендерит через React: domcontentloaded срабатывает
                # до гидрации — body.innerText сразу после загрузки пустой.
                # Ждём конкретный элемент (видео-плеер или обёртку поста).
                try:
                    await page.wait_for_selector(
                        "video, "
                        "[data-e2e='video-detail'], "
                        "[data-e2e='browse-video'], "
                        "[class*='DivVideoWrapper'], "
                        "[class*='video-player'], "
                        "[class*='VideoPlayer']",
                        timeout=10000,
                    )
                except Exception:
                    # Элемент не появился — даём ещё немного времени
                    await page.wait_for_timeout(3000)

                await page.wait_for_timeout(random.randint(1000, 2000))

                # Закрываем попап если появился
                await self._close_login_popup(page)
                await self._solve_captcha(page)

                # TikTok — React SPA: innerText почти пуст до гидрации.
                # Проверяем HTML-размер (всегда > 5000 у реальной страницы)
                # и отсутствие редиректа на логин.
                page_ok = False
                for _check_attempt in range(3):
                    try:
                        html_len = await page.evaluate(
                            "document.documentElement.innerHTML.length"
                        )
                        on_login = await page.evaluate(
                            "location.pathname.includes('/login') || "
                            "location.pathname.includes('/signup')"
                        )
                        if html_len > 5000 and not on_login:
                            page_ok = True
                            break
                        # Страница не готова — ждём гидрацию и закрываем попапы
                        await self._close_login_popup(page)
                        await self._solve_captcha(page)
                        await page.wait_for_timeout(3000)
                    except Exception:
                        break

                if not page_ok:
                    logger.debug(f"[TikTok] Пустая страница: {v_url}")
                    return None

                # video_id из URL: /video/1234567890
                parts = v_url.rstrip("/").split("/")
                video_id = parts[-1] if parts else f"tt_id_{random.randint(10000, 99999)}"

                meta = await self._extract_post_metadata(page, keyword)

                author_username    = meta["author_username"]
                author_display_name = meta["author_display_name"]
                likes_count        = meta["likes_count"]
                comments_count     = meta["comments_count"]
                shares_count       = meta["shares_count"]
                final_views        = meta["views_count"] if meta["views_count"] > 0 else views_count
                publish_date       = meta["publish_date"]
                video_duration     = meta["video_duration"]
                audio_id           = meta["audio_id"]
                audio_title        = meta["audio_title"]
                audio_artist       = meta["audio_artist"]
                audio_usage_count  = meta["audio_usage_count"]

                # Данные профиля — Этап 2
                followers_count = 0
                following_count = 0
                total_posts     = 0
                bio             = ""
                is_verified     = 0

                # Перехват видео
                local_path = await self._intercept_and_download_video(page, video_id)

                if not local_path:
                    try:
                        video_el = page.locator("video").first
                        if await video_el.count() > 0:
                            video_src = await video_el.get_attribute("src") or ""
                            if video_src and not video_src.startswith("blob:"):
                                local_filename = f"tt_{video_id}.mp4"
                                local_path = os.path.join(
                                    str(DOWNLOAD_DIR), local_filename
                                )
                                video_resp = await page.context.request.get(
                                    video_src, timeout=20000
                                )
                                content = await video_resp.body()
                                if len(content) > 10240:
                                    with open(local_path, "wb") as f:
                                        f.write(content)
                                else:
                                    local_path = ""
                    except Exception:
                        local_path = ""

                # Определяем длительность через OpenCV если не получили из DOM
                if (local_path and os.path.exists(local_path)
                        and os.path.getsize(local_path) > 50 * 1024
                        and video_duration == 0.0):
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
                ocr_text   = ""
                if (local_path and os.path.exists(local_path)
                        and os.path.getsize(local_path) > 50 * 1024):
                    if ml_processor:
                        try:
                            audio_text = await asyncio.to_thread(
                                ml_processor.extract_audio_text, local_path
                            )
                        except Exception as ml_ae:
                            logger.warning(f"[ML Guard] Ошибка Whisper: {ml_ae}")
                        try:
                            ocr_text = await asyncio.to_thread(
                                ml_processor.extract_ocr_text, local_path
                            )
                        except Exception as ml_oe:
                            logger.warning(f"[ML Guard] Ошибка Tesseract: {ml_oe}")
                    try:
                        os.remove(local_path)
                    except Exception:
                        pass
                else:
                    if local_path and os.path.exists(local_path):
                        try:
                            os.remove(local_path)
                        except Exception:
                            pass

                hashtags = (
                    " ".join(w for w in meta["caption"].split() if w.startswith("#"))
                    or f"#{keyword}"
                )

                text_blob = " ".join([
                    meta["caption"], hashtags, audio_text, ocr_text,
                    audio_title, audio_artist, bio,
                ])
                match_percentage = _compute_match_percentage(text_blob)

                return {
                    "video_url":          v_url,
                    "video_id":           video_id,
                    "platform":           "tiktok",
                    "caption":            meta["caption"] or f"TikTok #{keyword}",
                    "hashtags":           hashtags,
                    "likes_count":        likes_count,
                    "comments_count":     comments_count,
                    "shares_count":       shares_count,
                    "views_count":        final_views,
                    "video_duration":     video_duration,
                    "publish_date":       publish_date,
                    "author_username":    author_username or "unknown",
                    "author_id":          (
                        f"tt_{author_username}" if author_username
                        else f"tt_unknown_{video_id}"
                    ),
                    "author_display_name": (
                        author_display_name or author_username or "Unknown"
                    ),
                    "followers_count":    followers_count,
                    "following_count":    following_count,
                    "total_posts":        total_posts,
                    "account_creation_date": "",
                    "bio":                bio,
                    "is_verified":        is_verified,
                    "external_url":       (
                        f"https://www.tiktok.com/@{author_username}/"
                        if author_username else ""
                    ),
                    "audio_id":           audio_id,
                    "audio_title":        audio_title,
                    "audio_artist":       audio_artist,
                    "audio_usage_count":  audio_usage_count,
                    "audio_text":         audio_text,
                    "ocr_text":           ocr_text,
                    "status":             "parsed",
                    "match_percentage":   match_percentage,
                    "local_path":         "",
                }

            except Exception as e:
                logger.warning(
                    f"[TikTok Worker {worker_idx}] Ошибка поста {v_url}: {e}"
                )
                return None
            finally:
                try:
                    await page.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Главная точка входа
    # ------------------------------------------------------------------
    async def parse(
        self, keyword: str, ml_processor=None
    ) -> List[Dict[str, Any]]:
        parsed_results = []
        attempt = 0
        max_attempts = 3

        while attempt < max_attempts:
            async with async_playwright() as p:
                storage_state = (
                    TIKTOK_SESSION_FILE
                    if os.path.exists(TIKTOK_SESSION_FILE) else None
                )

                # Привязываем UA к сессии: если сессия уже есть — берём тот же
                # UA, с которым логинились. Иначе выбираем случайный и сохраняем,
                # чтобы все последующие запуски с этим cookie-файлом его переиспользовали.
                if storage_state and os.path.exists(TIKTOK_UA_FILE):
                    with open(TIKTOK_UA_FILE, "r", encoding="utf-8") as f:
                        session_user_agent = f.read().strip() or random.choice(USER_AGENTS)
                else:
                    session_user_agent = random.choice(USER_AGENTS)

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
                    user_agent=session_user_agent,
                    viewport={"width": 1280, "height": 800},
                    storage_state=storage_state,
                    locale="en-US",
                    timezone_id="Europe/Amsterdam",
                    extra_http_headers={
                        "Accept": (
                            "text/html,application/xhtml+xml,"
                            "application/xml;q=0.9,image/webp,*/*;q=0.8"
                        ),
                        "Accept-Encoding": "gzip, deflate, br",
                        "Accept-Language": "en-US,en;q=0.9",
                        "DNT": "1",
                        "Referer": "https://www.tiktok.com/",
                        "Sec-Fetch-Dest": "document",
                        "Sec-Fetch-Mode": "navigate",
                        "Sec-Fetch-Site": "none",
                        "Sec-Fetch-User": "?1",
                        "Upgrade-Insecure-Requests": "1",
                    },
                )
                await context.add_init_script("""
                    Object.defineProperty(navigator, 'webdriver',
                        { get: () => undefined });
                    window.chrome = { runtime: {} };
                """)
                page = await context.new_page()

                try:
                    # ── Авторизация / восстановление сессии ──────────
                    if not storage_state:
                        logger.info("[TikTok] Сессия не найдена — логинимся...")
                        login_ok = await self._login(page)
                        if not login_ok:
                            await browser.close()
                            attempt += 1
                            continue
                    else:
                        logger.info(
                            f"[TikTok] Загружаем сессию из {TIKTOK_SESSION_FILE}"
                        )
                        try:
                            await page.goto(
                                "https://www.tiktok.com/",
                                timeout=50000, wait_until="domcontentloaded",
                            )
                        except Exception as e:
                            error_str = str(e).lower()
                            if "429" in error_str or "err_http" in error_str:
                                logger.warning("[TikTok] Блокировка при открытии главной")
                                await self.vpn_rotator.rotate_ip()
                                try:
                                    await page.goto(
                                        "https://www.tiktok.com/",
                                        timeout=50000,
                                        wait_until="domcontentloaded",
                                    )
                                except Exception:
                                    await browser.close()
                                    attempt += 1
                                    continue

                        await page.wait_for_timeout(random.randint(2500, 3500))
                        await self._accept_cookies(page)
                        await self._close_login_popup(page)

                        # Проверяем валидность сессии
                        if "login" in page.url.lower():
                            logger.warning("[TikTok] Сессия протухла — логинимся заново...")
                            try:
                                os.remove(TIKTOK_SESSION_FILE)
                            except Exception:
                                pass
                            await browser.close()
                            attempt += 1
                            continue

                    urls: list = []
                    url_views_map: dict = {}

                    # ── Поиск через /search/video (единственный метод) ────────
                    search_keyword = keyword.lstrip("#").strip()
                    search_url = (
                        "https://www.tiktok.com/search/video?q="
                        + quote(search_keyword, safe="")
                    )
                    logger.info(f"[TikTok] Поиск: {search_url}")
                    try:
                        response = await page.goto(
                            search_url, timeout=60000,
                            wait_until="domcontentloaded",
                        )
                        if response and response.status == 429:
                            logger.warning("[TikTok] 429 при поиске — ротация IP...")
                            await browser.close()
                            await self.vpn_rotator.rotate_ip()
                            attempt += 1
                            continue

                        await page.wait_for_timeout(3000)
                        await self._close_login_popup(page)
                        await self._solve_captcha(page)
                        try:
                            await page.wait_for_selector(
                                "a[href*='/video/']", timeout=12000
                            )
                        except Exception:
                            pass
                        await self._chaotic_scroll(page)
                        await page.wait_for_timeout(2000)

                        posts_data = await page.evaluate(
                            r"""() => {
                                const results = [];
                                const links = document.querySelectorAll(
                                    'a[href*="/video/"]'
                                );
                                links.forEach(a => {
                                    const href = a.href;
                                    if (!href) return;
                                    let views = 0;
                                    const viewsEl = a.querySelector(
                                        'strong[class*="video-count"], '
                                        + '[class*="video-count"], '
                                        + '[data-e2e="video-views"]'
                                    );
                                    if (viewsEl) {
                                        const raw = (viewsEl.innerText || "")
                                            .replace(/[^0-9KMkm.,]/g, "");
                                        let n = parseFloat(raw.replace(",", "."));
                                        if (raw.toUpperCase().includes("K")) n *= 1000;
                                        if (raw.toUpperCase().includes("M")) n *= 1000000;
                                        if (!isNaN(n) && n > 0) views = Math.floor(n);
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
                            logger.info(
                                f"[TikTok] Поиск «{search_keyword}» → {len(urls)} постов."
                            )
                        else:
                            logger.warning(
                                f"[TikTok] Поиск ничего не дал: «{search_keyword}»"
                            )
                    except Exception as se:
                        logger.warning(f"[TikTok] Ошибка поиска: {se}")

                    urls = list(dict.fromkeys(urls))[:MAX_VIDEOS_PER_KEYWORD]


                    if not urls:
                        logger.warning("[TikTok] Постов не найдено.")
                        await browser.close()
                        return parsed_results

                    logger.info(
                        f"[TikTok] Запускаю {PARALLEL_WORKERS} воркеров "
                        f"для {len(urls)} постов..."
                    )

                    # ── ЭТАП 1: параллельный сбор постов ─────────────
                    tasks = [
                        self._process_single_post(
                            context, v_url, keyword, idx,
                            ml_processor, self.vpn_rotator,
                            views_count=url_views_map.get(v_url, 0),
                        )
                        for idx, v_url in enumerate(urls)
                    ]
                    results = await asyncio.gather(*tasks, return_exceptions=True)

                    for r in results:
                        if isinstance(r, dict):
                            parsed_results.append(r)

                    # Если все воркеры вернули None — TikTok заблокировал сессию.
                    # Ротируем IP и повторяем попытку вместо молчаливого 0 результата.
                    non_none = sum(1 for r in results if isinstance(r, dict))
                    if non_none == 0 and len(results) > 0:
                        logger.warning(
                            f"[TikTok] Все {len(results)} постов вернули None — "
                            f"возможна блокировка. Ротация IP..."
                        )
                        try:
                            await browser.close()
                        except Exception:
                            pass
                        await self.vpn_rotator.rotate_ip()
                        attempt += 1
                        await asyncio.sleep(5)
                        continue

                    # ── ЭТАП 2: параллельный сбор профилей ───────────
                    if parsed_results:
                        logger.info(
                            f"[TikTok] Посты собраны. Запускаю {PARALLEL_WORKERS} "
                            f"воркеров для профилей..."
                        )

                        profiles_cache: dict = {}
                        profile_lock = asyncio.Lock()

                        async def _profile_worker(username: str):
                            pg = await context.new_page()
                            try:
                                logger.info(
                                    f"[TikTok] Парсим профиль: @{username}"
                                )
                                followers, following, posts, bio, verified, watched_views = \
                                    await self._extract_profile_data(pg, username)
                                async with profile_lock:
                                    profiles_cache[username] = {
                                        "followers":      followers,
                                        "following":      following,
                                        "posts":          posts,
                                        "bio":            bio,
                                        "verified":       verified,
                                        "watched_views":  watched_views,
                                    }
                            except Exception as e:
                                logger.warning(
                                    f"[TikTok] Ошибка профиля @{username}: {e}"
                                )
                            finally:
                                try:
                                    await pg.close()
                                except Exception:
                                    pass

                        # Уникальные авторы
                        unique_authors = list({
                            r.get("author_username")
                            for r in parsed_results
                            if r.get("author_username")
                            and r.get("author_username") != "unknown"
                        })

                        # Батчи по PARALLEL_WORKERS
                        for i in range(0, len(unique_authors), PARALLEL_WORKERS):
                            batch = unique_authors[i:i + PARALLEL_WORKERS]
                            await asyncio.gather(*[
                                _profile_worker(u) for u in batch
                            ])

                        # Применяем данные профилей
                        for r in parsed_results:
                            username = r.get("author_username")
                            if username in profiles_cache:
                                c = profiles_cache[username]
                                r["followers_count"] = c["followers"]
                                r["following_count"] = c["following"]
                                r["total_posts"]     = c["posts"]
                                r["bio"]             = c["bio"]
                                r["is_verified"]     = c["verified"]
                                # Применяем просмотры из "Уже смотрели"
                                video_id = r.get("video_id", "")
                                watched = c.get("watched_views", {})
                                if video_id and video_id in watched and watched[video_id] > 0:
                                    r["views_count"] = watched[video_id]
                                    logger.info(
                                        f"[TikTok] 'Уже смотрели': "
                                        f"video {video_id} → {watched[video_id]:,} просмотров"
                                    )

                    logger.success(
                        f"[TikTok] Готово. Собрано и обогащено "
                        f"{len(parsed_results)} записей."
                    )
                    await browser.close()
                    return parsed_results

                except Exception as e:
                    logger.error(
                        f"[TikTok] Ошибка сессии: {e}. Ротация IP..."
                    )
                    try:
                        await browser.close()
                    except Exception:
                        pass
                    await self.vpn_rotator.rotate_ip()
                    attempt += 1
                    await asyncio.sleep(5)

        logger.error("[TikTok] Все попытки исчерпаны.")
        return parsed_results