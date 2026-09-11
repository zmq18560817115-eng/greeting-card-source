"""全局配置：从 .env 读取，带默认值。"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_env():
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_env()


def _b(key, default="false"):
    return os.environ.get(key, default).strip().lower() in ("1", "true", "yes", "on")


def _t(key, default):
    """'10:00' -> (10, 0)"""
    raw = os.environ.get(key, default).strip()
    h, m = raw.split(":")
    return int(h), int(m)


# 飞书
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", "")
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", "")
FEISHU_BASE = os.environ.get("FEISHU_BASE", "https://open.feishu.cn").rstrip("/")

# 方舟
ARK_API_KEY = os.environ.get("ARK_API_KEY", "")
ARK_MODEL = os.environ.get("ARK_MODEL", "doubao-seedream-5-0-260128")
ARK_BASE = os.environ.get("ARK_BASE", "https://ark.cn-beijing.volces.com/api/v3").rstrip("/")

# 业务
TZ = os.environ.get("TZ", "Asia/Shanghai")
CARDS_PER_EVENT = int(os.environ.get("CARDS_PER_EVENT", "5"))
ANNIVERSARY_PUSH_TIME = _t("ANNIVERSARY_PUSH_TIME", "10:00")
BIRTHDAY_PUSH_TIME = _t("BIRTHDAY_PUSH_TIME", "11:00")
WEEKLY_CRON_DAY = os.environ.get("WEEKLY_CRON_DAY", "thu")
WEEKLY_CRON_HOUR, WEEKLY_CRON_MINUTE = _t("WEEKLY_CRON_TIME", "20:00")

# 服务
HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "8848"))
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "").strip()
DRY_RUN = _b("DRY_RUN", "true")

# 路径
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "app.db"
OUTPUT_DIR = BASE_DIR / "output"
BG_DIR = OUTPUT_DIR / "bg"
CARD_DIR = OUTPUT_DIR / "cards"
ASSETS_DIR = BASE_DIR / "assets"
TEMPLATE_DIR = ASSETS_DIR / "templates"
FONT_DIR = ASSETS_DIR / "fonts"
TEMPLATE_CONFIG = BASE_DIR / "config" / "templates.json"
LOG_DIR = BASE_DIR / "logs"

for _d in (DATA_DIR, OUTPUT_DIR, BG_DIR, CARD_DIR, TEMPLATE_DIR, FONT_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# 推送重试
MAX_PUSH_ATTEMPTS = int(os.environ.get("MAX_PUSH_ATTEMPTS", "3"))
