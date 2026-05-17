import os
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

def _i(k, d): 
    try: return int(os.getenv(k, d))
    except: return d

# AI
GROQ_API_KEY        = os.getenv("GROQ_API_KEY", "").strip()
GROQ_MODEL          = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
OPENROUTER_API_KEY  = os.getenv("OPENROUTER_API_KEY", "").strip()
OPENROUTER_MODEL    = os.getenv("OPENROUTER_MODEL", "meta-llama/llama-3.3-70b-instruct:free")

# Facebook
FB_PAGE_ID          = os.getenv("FB_PAGE_ID", "").strip()
FB_PAGE_TOKEN       = os.getenv("FB_PAGE_ACCESS_TOKEN", "").strip()

# YouTube
YT_CLIENT_SECRETS   = ROOT / os.getenv("YT_CLIENT_SECRETS", "client_secret.json")
YT_TOKEN_FILE       = ROOT / os.getenv("YT_TOKEN_FILE", "token.json")
YT_CATEGORY_ID      = os.getenv("YT_CATEGORY_ID", "22")
YT_PRIVACY          = os.getenv("YT_PRIVACY", "public")

# Scheduling
FB_PER_DAY          = _i("FB_POSTS_PER_DAY", 4)
YT_PER_DAY          = _i("YT_POSTS_PER_DAY", 4)
HOUR_START          = _i("ACTIVE_HOUR_START", 9)
HOUR_END            = _i("ACTIVE_HOUR_END", 23)
JITTER_MIN          = _i("JITTER_MINUTES", 25)

# Dashboard
HOST                = os.getenv("DASHBOARD_HOST", "127.0.0.1")
PORT                = _i("DASHBOARD_PORT", 5000)
SECRET              = os.getenv("DASHBOARD_SECRET", "change-me")

# Content
NICHE               = os.getenv("CONTENT_NICHE", "general entertainment")
LANGUAGE            = os.getenv("CONTENT_LANGUAGE", "English")
DEFAULT_HASHTAGS    = os.getenv("DEFAULT_HASHTAGS", "#shorts #viral #trending")

# Paths
DATA_DIR            = ROOT / "automation_data"
DOWNLOADS           = DATA_DIR / "downloads"
LOGS_DIR            = DATA_DIR / "logs"
DB_FILE             = DATA_DIR / "queue.db"
for p in (DATA_DIR, DOWNLOADS, LOGS_DIR):
    p.mkdir(parents=True, exist_ok=True)
