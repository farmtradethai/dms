"""Application settings — everything comes from environment variables so the same
code runs on a laptop (SQLite + local disk) and on a free host (Postgres + S3/R2)."""
import os
import time
from pathlib import Path

# Free hosts run in UTC. Factory wall-clock is Asia/Bangkok: make the whole process use it (Linux/macOS).
os.environ.setdefault("TZ", os.getenv("APP_TZ", "Asia/Bangkok"))
if hasattr(time, "tzset"):
    time.tzset()

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT_DIR / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
BACKUP_DIR = DATA_DIR / "backups"

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)


def _base_url():
    """Public URL used in notification links / QR codes. Explicit BASE_URL wins, otherwise the host's own
    variable (Render / Koyeb / Fly / Railway); main.py also learns it from the first real request."""
    for key, fmt in (("BASE_URL", "{}"), ("RENDER_EXTERNAL_URL", "{}"), ("KOYEB_PUBLIC_DOMAIN", "https://{}"),
                     ("RAILWAY_PUBLIC_DOMAIN", "https://{}"), ("FLY_APP_NAME", "https://{}.fly.dev")):
        v = os.getenv(key, "").strip()
        if v:
            return fmt.format(v).rstrip("/"), key != "BASE_URL"
    return "http://localhost:8790", True


def _db_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return f"sqlite:///{DATA_DIR / 'die_management.db'}"
    # Render / Heroku style "postgres://" -> SQLAlchemy needs "postgresql://"
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    # SQLAlchemy >= 2.1 maps a bare "postgresql://" to psycopg v3; we ship psycopg2 -> pin the driver explicitly
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


def _secret_key() -> str:
    """SECRET_KEY signs the login cookie. Never fall back to a fixed, public value: if the env var is missing,
    create a random key once and keep it in data/.secret (survives restarts on a normal disk)."""
    k = os.getenv("SECRET_KEY", "").strip()
    if k and not k.lower().startswith("change"):
        return k
    f = DATA_DIR / ".secret"
    try:
        if f.exists() and len(f.read_text().strip()) >= 32:
            return f.read_text().strip()
        import secrets as _s
        k = _s.token_urlsafe(48)
        f.write_text(k)
        try:
            os.chmod(f, 0o600)
        except Exception:
            pass
        return k
    except Exception:
        import secrets as _s
        return _s.token_urlsafe(48)        # read-only disk: random per start (users just log in again)


class Settings:
    APP_NAME = os.getenv("APP_NAME", "Die Management System")
    SECRET_KEY = _secret_key()
    SECRET_FROM_ENV = bool(os.getenv("SECRET_KEY", "").strip()) and not os.getenv("SECRET_KEY", "").lower().startswith("change")
    DATABASE_URL = _db_url()
    BASE_URL, BASE_URL_AUTO = _base_url()
    PORT = int(os.getenv("PORT", "8790"))

    # Image policy (keeps the DB / disk small)
    IMAGE_MAX_SIDE = int(os.getenv("IMAGE_MAX_SIDE", "1600"))
    IMAGE_QUALITY = int(os.getenv("IMAGE_QUALITY", "78"))
    THUMB_SIDE = int(os.getenv("THUMB_SIDE", "360"))
    MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "12"))
    STORAGE_WARN_MB = int(os.getenv("STORAGE_WARN_MB", "400"))

    # Optional S3-compatible storage (Cloudflare R2 / Supabase Storage / MinIO)
    S3_ENDPOINT = os.getenv("S3_ENDPOINT", "")
    S3_BUCKET = os.getenv("S3_BUCKET", "")
    S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY", "")
    S3_SECRET_KEY = os.getenv("S3_SECRET_KEY", "")
    S3_PUBLIC_URL = os.getenv("S3_PUBLIC_URL", "")
    S3_REGION = os.getenv("S3_REGION", "auto")          # R2: auto · Supabase Storage: e.g. ap-southeast-1

    # Default super admin (created on first start only)
    ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin1234")

    SESSION_DAYS = int(os.getenv("SESSION_DAYS", "14"))
    TZ_OFFSET_HOURS = int(os.getenv("TZ_OFFSET_HOURS", "7"))  # Asia/Bangkok


settings = Settings()
