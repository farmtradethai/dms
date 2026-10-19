import threading
import time
from datetime import timedelta
import traceback
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import migrate
from .auth import ensure_default_admin, get_current_user
from .config import BASE_DIR, UPLOAD_DIR, settings
from .database import SessionLocal
from .models import Company
from .routers import admin, analytics, api, auth, dashboard, pm, reports, requests, setup
from .services import images
from .web import render

STARTED_AT = time.time()


def _daily_complete(c, now) -> bool:
    """True when every daily job of this company already ran today (then the scheduler need not touch the DB)."""
    s, today = c.settings, now.date().isoformat()
    if s.get("_last_check") != f"checks_{today}":
        return False
    if s.get("auto_backup", True) and s.get("_last_auto_backup") != today:
        return False
    if s.get("daily_report") and s.get("_last_daily_report") != today:
        return False
    if int(s.get("pm_auto_create_days") or 0) > 0 and s.get("_last_pm_auto") != today:
        return False
    return True


def _scheduler():
    """Every hour: overdue / PM-due / storage warnings, daily auto-backup, daily report, PM auto-create (each once a day).
    Once all of today's jobs are done it sleeps until just after midnight, so a free scale-to-zero Postgres
    (Neon / Supabase) is not woken up every hour for nothing."""
    from .database import now_local
    time.sleep(30)
    while True:
        db = SessionLocal()
        all_done = True
        try:
            for c in db.query(Company).filter(Company.is_active == True).all():
                try:
                    admin.run_checks(db, c)
                    admin.run_daily_jobs(db, c)
                    db.refresh(c)
                    all_done = all_done and _daily_complete(c, now_local())
                except Exception:
                    db.rollback()
                    all_done = False
                    traceback.print_exc()
        except Exception:
            all_done = False
            traceback.print_exc()
        finally:
            db.close()
        now = now_local()
        if all_done:
            nxt = (now + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
            time.sleep(max(600, (nxt - now).total_seconds()))
        else:
            time.sleep(3600)


@asynccontextmanager
async def lifespan(app: FastAPI):
    migrate.run()                       # create tables + add new columns of newer versions (never drops data)
    db = SessionLocal()
    try:
        ensure_default_admin(db)
    finally:
        db.close()
    threading.Thread(target=_scheduler, daemon=True).start()
    yield


class CachedStatic(StaticFiles):
    """Long browser cache for CSS/JS/fonts -> saves bandwidth (free hosts meter outbound traffic)."""
    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = "public, max-age=604800"
        return resp


app = FastAPI(title=settings.APP_NAME, version=migrate.APP_VERSION, lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=800)   # HTML/JSON shrink ~5-8x
app.mount("/static", CachedStatic(directory=str(BASE_DIR / "static")), name="static")

for r in (auth.router, requests.router, pm.router, dashboard.router, reports.router, analytics.router, admin.router, setup.router, api.router):
    app.include_router(r)


@app.middleware("http")
async def learn_base_url(request: Request, call_next):
    """When BASE_URL was not set explicitly, learn the public https address from the first real request
    (behind Render/Koyeb proxies X-Forwarded-Proto tells us it is https). Links in LINE/Telegram/e-mail then work."""
    if settings.BASE_URL_AUTO:
        host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
        if host and not host.split(":")[0] in ("localhost", "127.0.0.1", "0.0.0.0", "testserver"):
            proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
            settings.BASE_URL = f"{proto}://{host}".rstrip("/")
            settings.BASE_URL_AUTO = False
    return await call_next(request)


@app.get("/media/{path:path}")
def media(path: str, request: Request, t: str = ""):
    """Private images: needs a login, or a signed ?t= token (links sent to LINE/Telegram, valid 7 days)."""
    if not (t and images.media_token_ok(path, t)):
        db = SessionLocal()
        try:
            if not get_current_user(request, db):
                raise HTTPException(401)
        finally:
            db.close()
    if images.using_s3():
        # never stream through the app server: redirect the browser to the bucket (no app bandwidth used)
        return RedirectResponse(images.presigned_url(path, 3600), 302, headers={"Cache-Control": "private, max-age=3000"})
    p = (UPLOAD_DIR / path).resolve()
    if not str(p).startswith(str(UPLOAD_DIR.resolve())) or not p.exists():
        raise HTTPException(404)
    return FileResponse(p, headers={"Cache-Control": "private, max-age=604800"})


@app.get("/healthz")
def healthz():
    """Keep-alive endpoint for cron-job.org / UptimeRobot. Does NOT touch the database on purpose,
    so pinging it keeps the web service awake without waking a scale-to-zero free Postgres."""
    return {"ok": True, "version": migrate.APP_VERSION, "uptime_s": int(time.time() - STARTED_AT), "base_url": settings.BASE_URL}


@app.exception_handler(HTTPException)
async def http_exc(request: Request, exc: HTTPException):
    if exc.status_code == 307 and exc.headers and "Location" in exc.headers:
        return RedirectResponse(exc.headers["Location"], 302)
    if exc.status_code == 401 and not request.url.path.startswith("/api/"):
        return RedirectResponse(f"/login?next={request.url.path}", 302)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    db = SessionLocal()
    try:
        try:
            get_current_user(request, db)
        except Exception:
            pass
        resp = render(request, "error.html", code=exc.status_code, detail=exc.detail, db=db)
    finally:
        db.close()
    resp.status_code = exc.status_code
    return resp


@app.exception_handler(Exception)
async def any_exc(request: Request, exc: Exception):
    traceback.print_exc()
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)
    return HTMLResponse(f"<h3>เกิดข้อผิดพลาดในระบบ / Server error</h3><pre>{type(exc).__name__}: {exc}</pre>"
                        f"<p>แจ้งผู้ดูแลระบบพร้อมภาพหน้าจอนี้</p><a href='/'>Home</a>", status_code=500)
