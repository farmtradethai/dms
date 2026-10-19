import hashlib
import hmac
import os
import secrets
from typing import Optional

from fastapi import Depends, HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db, now_local
from .models import Company, User

_serializer = URLSafeTimedSerializer(settings.SECRET_KEY, salt="dms-session")
COOKIE = "dms_session"


def hash_password(pw: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()
    return f"pbkdf2${salt}${dk}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, salt, dk = stored.split("$")
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000).hex()
    return hmac.compare_digest(calc, dk)


def make_session_cookie(user_id: int, company_id: Optional[int]) -> str:
    return _serializer.dumps({"u": user_id, "c": company_id})


def read_session(request: Request) -> Optional[dict]:
    raw = request.cookies.get(COOKIE)
    if not raw:
        return None
    try:
        return _serializer.loads(raw, max_age=settings.SESSION_DAYS * 86400)
    except BadSignature:
        return None


def get_current_user(request: Request, db: Session = Depends(get_db)) -> Optional[User]:
    data = read_session(request)
    if not data:
        return None
    user = db.get(User, data.get("u"))
    if not user or not user.is_active:
        return None
    if user.company is not None and not user.company.is_active and not user.is_superadmin:
        return None
    # the company the user is currently *working in* (superadmin can switch)
    if user.is_superadmin:
        cid = data.get("c")
        request.state.company = db.get(Company, cid) if cid else db.query(Company).order_by(Company.id).first()
    else:
        request.state.company = user.company
    request.state.user = user
    # "online" indicator, written at most every 2 minutes per user to keep DB writes low
    now = now_local()
    if not user.last_seen or (now - user.last_seen).total_seconds() > 120:
        try:
            user.last_seen = now
            db.commit()
        except Exception:
            db.rollback()
    return user


_PW_EXEMPT = ("/profile", "/logout", "/static", "/media", "/api/notifications", "/lang")


def require_user(request: Request, user: Optional[User] = Depends(get_current_user)) -> User:
    if not user:
        raise HTTPException(status_code=307, headers={"Location": f"/login?next={request.url.path}"})
    if not getattr(request.state, "company", None):
        if not user.is_superadmin:
            raise HTTPException(status_code=403, detail="No company assigned")
        if not request.url.path.startswith(("/admin", "/profile", "/logout", "/lang", "/static", "/media", "/switch-company")):
            # empty database (first deploy / disaster recovery): create a company or restore a backup first
            raise HTTPException(status_code=307, headers={"Location": "/admin/companies"})
    if user.must_change_pw and not request.url.path.startswith(_PW_EXEMPT):
        raise HTTPException(status_code=307, headers={"Location": "/profile?force=1"})
    return user


# ---- brute-force protection (in memory; resets on restart which is fine for a single instance)
# Per USERNAME: 8 wrong passwords -> that account waits 10 minutes.
# Per IP only a high limit (a whole factory shares one public IP, so one person must never lock everybody out).
_FAILS: dict = {}
LOCK_SECONDS = 600
LIMITS = {"u": 8, "ip": 60}


def login_locked(key: str) -> int:
    import time as _t
    n, until = _FAILS.get(key, (0, 0))
    return int(until - _t.time()) if until > _t.time() else 0


def login_failed(key: str):
    import time as _t
    n, until = _FAILS.get(key, (0, 0))
    n += 1
    limit = LIMITS["ip"] if key.startswith("ip:") else LIMITS["u"]
    _FAILS[key] = (0, _t.time() + LOCK_SECONDS) if n >= limit else (n, 0)


def login_ok(key: str):
    _FAILS.pop(key, None)


def require_roles(*roles):
    def dep(user: User = Depends(require_user)) -> User:
        if user.role in roles or user.is_superadmin:
            return user
        raise HTTPException(status_code=403, detail="Permission denied")
    return dep


def current_company(request: Request) -> Company:
    return request.state.company


def ensure_default_admin(db: Session):
    """Creates the first super admin + demo company on an empty database."""
    if db.query(User).filter(User.role == "superadmin").count() == 0:
        pw = settings.ADMIN_PASSWORD or secrets.token_urlsafe(8)
        existing = db.query(User).filter(User.username == settings.ADMIN_USERNAME).first()
        if existing:
            existing.role, existing.password_hash, existing.is_active = "superadmin", hash_password(pw), True
        else:
            db.add(User(username=settings.ADMIN_USERNAME, password_hash=hash_password(pw), full_name="System Admin",
                        role="superadmin", lang="th", theme="light",
                        # on a public host with the default password, force a change at first login
                        must_change_pw=(pw == "admin1234" and not is_local_install())))
        db.commit()
        print(f"[init] super admin '{settings.ADMIN_USERNAME}' ready (password from ADMIN_PASSWORD env, default admin1234)")


def is_local_install() -> bool:
    return any(h in settings.BASE_URL for h in ("localhost", "127.0.0.1"))


def cookie_kwargs(request=None) -> dict:
    """Secure flag follows the CURRENT connection, not BASE_URL: the same factory server may be opened through a
    Cloudflare Tunnel (https) and through its LAN IP (http) at the same time — both must be able to log in."""
    secure = False
    if request is not None:
        proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
        secure = proto == "https"
    return dict(max_age=settings.SESSION_DAYS * 86400, httponly=True, samesite="lax", secure=secure)
