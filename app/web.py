"""Template rendering helpers shared by all routers."""
import json
from datetime import date, datetime

from fastapi import Request
from fastapi.templating import Jinja2Templates
from sqlalchemy import or_

from .config import BASE_DIR, settings
from .i18n import t
from .models import ST, Notification, REQUIRED_DAYS, REQUEST_TYPES, ROLES
from .services.images import url_for
from .services.kpi import fmt_minutes
from .database import today_local

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

STATUS_COLORS = {
    "DRAFT": "gray", "WAIT_FOREMAN": "amber", "FOREMAN_REJECTED": "red", "WAIT_LEADER": "amber",
    "LEADER_REJECTED": "red", "ASSIGNED": "blue", "IN_PROGRESS": "blue", "REPAIR_DONE": "violet",
    "RETURNED": "red", "WAIT_QC": "teal", "NG_RETURN": "red", "CLOSED": "green", "CANCELLED": "gray",
}
THEMES = ["light", "dark", "ocean", "forest", "sunset", "factory"]


def fmt_date(v, with_time=False):
    if not v:
        return "-"
    if isinstance(v, datetime):
        return v.strftime("%d-%m-%Y %H:%M") if with_time else v.strftime("%d-%m-%Y")
    if isinstance(v, date):
        return v.strftime("%d-%m-%Y")
    return str(v)


def fmt_dt(v):
    return fmt_date(v, True)


def fmt_money(v):
    try:
        return f"{float(v):,.2f}"
    except (TypeError, ValueError):
        return "0.00"


def fmt_ym(v):
    """'2026-10' -> '10-2026'."""
    s = str(v or "")
    return f"{s[5:7]}-{s[:4]}" if len(s) == 7 and s[4] == "-" else s


templates.env.filters.update(d=fmt_date, dt=fmt_dt, money=fmt_money, minutes=fmt_minutes, media=url_for, ym=fmt_ym)
templates.env.globals.update(ST=ST, STATUS_COLORS=STATUS_COLORS, THEMES=THEMES, ROLES=ROLES,
                             REQUIRED_DAYS=REQUIRED_DAYS, REQUEST_TYPES=REQUEST_TYPES, app_name=settings.APP_NAME,
                             base_url=settings.BASE_URL, app_version=__import__("app.migrate", fromlist=["APP_VERSION"]).APP_VERSION, tojson=lambda v: json.dumps(v, ensure_ascii=False, default=str))


def render(request: Request, name: str, **ctx):
    user = getattr(request.state, "user", None)
    company = getattr(request.state, "company", None)
    lang = (user.lang if user else request.cookies.get("lang")) or "th"
    unread = 0
    all_companies = []
    db = ctx.get("db")
    if user and user.is_superadmin and db is not None:
        from .models import Company
        all_companies = db.query(Company).order_by(Company.code).all()
    if user and company:
        if db is not None:
            q = db.query(Notification).filter(Notification.company_id == company.id,
                                              or_(Notification.user_id == user.id, Notification.role == user.role))
            unread = sum(1 for n in q.order_by(Notification.id.desc()).limit(200) if user.id not in n.read_by)
    ctx.pop("db", None)
    from . import perms as _perms
    ctx.update(allowed=(lambda key: _perms.page_allowed(user, key)), sees_all=bool(user and _perms.sees_all(user, db)))
    ctx.update(base_url=settings.BASE_URL, request=request, user=user, company=company, lang=lang, unread=unread, all_companies=all_companies,
               t=lambda k: t(k, lang), st=lambda s: t(f"st_{s}", lang), today=today_local(),
               theme=(user.theme if user else "light"), accent=(user.accent if user else "#1f5fbf"))
    return templates.TemplateResponse(request, name, ctx)
