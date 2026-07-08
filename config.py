import os
import logging
from dotenv import load_dotenv
load_dotenv()


def _bool_env(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).lower() in ("1", "true", "yes")


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "changeme-use-strong-key")

    # ── Database ───────────────────────────────────────────────────────────────
    # Render/Neon/Supabase set DATABASE_URL. Older Heroku/Render postgres URLs
    # start with "postgres://" but SQLAlchemy 1.4+ requires "postgresql://".
    _db_url = os.environ.get("DATABASE_URL", "sqlite:///hirebridge.db")
    if _db_url.startswith("postgres://"):
        _db_url = _db_url.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = _db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        # Re-use connections; avoids "server closed the connection" on Neon/Supabase
        "pool_pre_ping": True,
        "pool_recycle": 300,
    }

    # ── File uploads ───────────────────────────────────────────────────────────
    # Deliberately OUTSIDE static/ — resumes served via authenticated route only
    UPLOAD_FOLDER = os.path.join(os.path.dirname(__file__), "instance", "uploads")
    MAX_CONTENT_LENGTH = 5 * 1024 * 1024  # 5 MB

    # ── Security / CSRF ────────────────────────────────────────────────────────
    WTF_CSRF_ENABLED = True

    # ── Static files ───────────────────────────────────────────────────────────
    # Tell browsers to cache static assets for 1 year (fingerprinted by Flask)
    SEND_FILE_MAX_AGE_DEFAULT = 31_536_000

    # ── Templates ─────────────────────────────────────────────────────────────
    # Disable auto-reload in production (saves CPU on every request)
    TEMPLATES_AUTO_RELOAD = _bool_env("FLASK_ENV") if os.environ.get("FLASK_ENV") == "development" else False

    # ── Rate limiting ──────────────────────────────────────────────────────────
    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")

    # ── Mail ───────────────────────────────────────────────────────────────────
    MAIL_SERVER          = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT            = int(os.environ.get("MAIL_PORT", 587))
    MAIL_USE_TLS         = True
    MAIL_USERNAME        = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD        = os.environ.get("MAIL_PASSWORD", "")
    MAIL_DEFAULT_SENDER  = os.environ.get("MAIL_USERNAME", "hirebridge@gmail.com")

    # ── App meta ───────────────────────────────────────────────────────────────
    ADMIN_EMAILS = os.environ.get("ADMIN_EMAILS", "hirebridge@gmail.com")
    APP_NAME     = "HireBridge Africa"

    # ── Job scraper API keys (all optional — free tiers available) ────────────
    ADZUNA_APP_ID     = os.environ.get("ADZUNA_APP_ID", "")
    ADZUNA_APP_KEY    = os.environ.get("ADZUNA_APP_KEY", "")
    RAPIDAPI_KEY      = os.environ.get("RAPIDAPI_KEY", "")
    REED_API_KEY      = os.environ.get("REED_API_KEY", "")
    FINDWORK_API_KEY  = os.environ.get("FINDWORK_API_KEY", "")
    JSEARCH_API_KEY   = os.environ.get("JSEARCH_API_KEY", "")
    SCALESERP_API_KEY = os.environ.get("SCALESERP_API_KEY", "")
    JOOBLE_API_KEY    = os.environ.get("JOOBLE_API_KEY", "")
    GOOGLE_ALERTS_RSS = os.environ.get("GOOGLE_ALERTS_RSS", "")

    # ── Logging ────────────────────────────────────────────────────────────────
    @staticmethod
    def configure_logging():
        """Call once at startup to set up structured logging."""
        level = logging.DEBUG if os.environ.get("FLASK_ENV") == "development" else logging.INFO
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        # Quieten noisy third-party loggers
        for noisy in ("urllib3", "requests", "werkzeug"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
