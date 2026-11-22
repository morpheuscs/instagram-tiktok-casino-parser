import os
from pathlib import Path

# Базовые директории проекта
BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(env_path: Path) -> None:
    """Лёгкий загрузчик .env без внешних зависимостей."""
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")

DOWNLOAD_DIR = BASE_DIR / "downloads"

# ── БД теперь хранится в папке src ───────────────────────────────────────────
SRC_DIR = BASE_DIR / "src"
os.makedirs(SRC_DIR, exist_ok=True)
DB_PATH = SRC_DIR / "analytics.db"

KEYWORDS_FILE = BASE_DIR / "keywords.txt"
CSV_EXPORT_PATH = BASE_DIR / "final_analytics_report.csv"

os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Системные лимиты и задержки
MAX_VIDEOS_PER_KEYWORD = int(os.getenv("MAX_VIDEOS_PER_KEYWORD", "15"))
MIN_DELAY = 1.0   # Было 5.0 — слишком медленно при 15 воркерах
MAX_DELAY = 3.0   # Было 15.0
MAX_ITEMS_PER_TAG = int(os.getenv("MAX_ITEMS_PER_TAG", "50"))
SESSION_FILE = BASE_DIR / "instagram_session.json"

# Количество параллельных воркеров
PARALLEL_WORKERS = int(os.getenv("PARALLEL_WORKERS", "5"))
 
USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_4) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]
 
# Учётные данные
INSTAGRAM_USERNAME = os.getenv("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD = os.getenv("INSTAGRAM_PASSWORD", "")
TIKTOK_USERNAME = os.getenv("TIKTOK_USERNAME", "")
TIKTOK_PASSWORD = os.getenv("TIKTOK_PASSWORD", "")
 
# Включение/отключение парсеров
ENABLE_INSTAGRAM = os.getenv("ENABLE_INSTAGRAM", "1").strip().lower() in ("1", "true", "yes", "on")
ENABLE_TIKTOK = os.getenv("ENABLE_TIKTOK", "1").strip().lower() in ("1", "true", "yes", "on")

# Настройки VPN-ротации (опционально, для Linux/OpenVPN)
ENABLE_VPN_ROTATION = os.getenv("ENABLE_VPN_ROTATION", "0").strip().lower() in ("1", "true", "yes", "on")
VPN_CONFIG_FILE = os.getenv("VPN_CONFIG_FILE", "")
VPN_CREDS_FILE = os.getenv("VPN_CREDS_FILE", "")