import json

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..auth import (COOKIE, cookie_kwargs, get_current_user, hash_password, is_local_install, login_failed, login_locked,
                    login_ok, make_session_cookie, require_user, verify_password)
from ..config import settings
from ..database import get_db, now_local
from ..models import AuditLog, Company, Notification, User
from ..web import THEMES, render

router = APIRouter()


@router.get("/login")
def login_page(request: Request, next: str = "/", db: Session = Depends(get_db), user=Depends(get_current_user)):
    if user:
        return RedirectResponse("/", 302)
    no_company = db.query(Company).count() == 0
    return render(request, "login.html", next=next, error=None, no_company=no_company, db=db)


def _demo_accounts(db):
    """On a local install (localhost) list the seeded demo accounts so first login never fails."""
    if not is_local_install():
        return []
    names = ["ah_admin", "ah_manager", "ah_prod", "ah_foreman", "ah_leader", "ah_tech1", "ah_tech2", "ah_qc"]
    found = {u.username: u for u in db.query(User).filter(User.username.in_(names + [settings.ADMIN_USERNAME]))}
    out = [(settings.ADMIN_USERNAME, settings.ADMIN_PASSWORD, "Super admin")] if settings.ADMIN_USERNAME in found else []
    out += [(n, "1234", found[n].role) for n in names if n in found]
    return out


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), next: str = Form("/"),
          db: Session = Depends(get_db)):
    uname = username.strip().lower()
    # behind Render / Cloudflare the real client IP is in X-Forwarded-For
    ip = "ip:" + ((request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
                  or (request.client.host if request.client else "?"))
    wait = max(login_locked(uname), login_locked(ip))
    if wait:
        return render(request, "login.html", next=next, error=f"ใส่รหัสผิดหลายครั้ง ระบบล็อกชั่วคราว ลองใหม่ใน {wait // 60 + 1} นาที",
                      no_company=False, db=db)
    user = db.query(User).filter(User.username == uname).first()
    if user and user.company is not None and not user.company.is_active and not user.is_superadmin:
        user = None                 # company switched off by the superadmin -> its users cannot log in
    if not user or not user.is_active or not verify_password(password, user.password_hash):
        reason = "no such user" if not user else ("inactive" if not user.is_active else "wrong password")
        print(f"[login] FAILED user='{uname}' ip={ip} ({reason})", flush=True)
        login_failed(uname)
        login_failed(ip)
        return render(request, "login.html", next=next, error="ชื่อผู้ใช้หรือรหัสผ่านไม่ถูกต้อง / Invalid login",
                      no_company=db.query(Company).count() == 0, db=db)
    login_ok(uname)
    login_ok(ip)
    user.last_login = now_local()
    db.add(AuditLog(company_id=user.company_id, user_id=user.id, user_name=user.full_name, action="login", entity="user",
                    entity_id=str(user.id)))
    db.commit()
    target = "/profile?force=1" if user.must_change_pw else (next if next.startswith("/") and not next.startswith("//") else "/")
    resp = RedirectResponse(target, 302)
    resp.set_cookie(COOKIE, make_session_cookie(user.id, user.company_id), **cookie_kwargs(request))
    return resp


@router.get("/logout")
def logout():
    resp = RedirectResponse("/login", 302)
    resp.delete_cookie(COOKIE)
    return resp


@router.get("/profile")
def profile(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db), msg: str = "", force: int = 0):
    my_steps = []
    if user.company_id:
        from .. import approvals
        rt = approvals.routing(db, user.company_id)
        for step, meta in approvals.STEPS.items():
            ls = rt.my_lines(user, step)
            if ls is None:
                my_steps.append(f"{meta['th']} — ทุก Line")
            elif ls:
                names = [l for l in rt.lines if l.lower() in ls] + (["(ไม่ระบุ Line)"] if "" in ls else [])
                my_steps.append(f"{meta['th']} — {', '.join(names)}")
    return render(request, "profile.html", themes=THEMES, msg=msg, force=force or user.must_change_pw, my_steps=my_steps, db=db)


@router.post("/profile")
def profile_save(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db),
                 lang: str = Form("th"), theme: str = Form("light"), accent: str = Form("#1f5fbf"),
                 phone: str = Form(""), email: str = Form(""), password: str = Form(""), password2: str = Form("")):
    u = db.get(User, user.id)
    u.lang = lang if lang in ("th", "en") else "th"
    u.theme = theme if theme in THEMES else "light"
    u.accent = accent if accent.startswith("#") and len(accent) == 7 else u.accent
    u.phone, u.email = phone.strip(), email.strip()
    msg = "บันทึกแล้ว"
    if password:
        if password != password2 or len(password) < 6:
            msg = "รหัสผ่านไม่ตรงกันหรือสั้นกว่า 6 ตัวอักษร"
        else:
            if verify_password(password, u.password_hash) or password in ("1234", "123456", "admin1234", "password"):
                msg = "กรุณาใช้รหัสผ่านใหม่ที่ไม่ใช่รหัสเดิม/รหัสเริ่มต้น"
            else:
                u.password_hash = hash_password(password)
                u.must_change_pw = False
                msg = "บันทึกและเปลี่ยนรหัสผ่านแล้ว"
    db.commit()
    return RedirectResponse(f"/profile?msg={msg}", 302)


@router.post("/switch-company")
def switch_company(request: Request, company_id: int = Form(...), user: User = Depends(require_user),
                   db: Session = Depends(get_db)):
    if not user.is_superadmin:
        return RedirectResponse("/", 302)
    resp = RedirectResponse(request.headers.get("referer", "/"), 302)
    resp.set_cookie(COOKIE, make_session_cookie(user.id, company_id), **cookie_kwargs(request))
    return resp


def _my_notifications(db, user, company, limit=60):
    q = (db.query(Notification).filter(Notification.company_id == company.id,
                                       or_(Notification.user_id == user.id, Notification.role == user.role))
         .order_by(Notification.id.desc()).limit(limit))
    return q.all()


@router.get("/notifications")
def notifications(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    items = _my_notifications(db, user, request.state.company)
    return render(request, "notifications.html", items=items, db=db)


@router.post("/notifications/read")
def notifications_read(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db),
                       nid: int = Form(0)):
    items = [db.get(Notification, nid)] if nid else _my_notifications(db, user, request.state.company, 200)
    company = request.state.company
    for n in items:
        if n is None or company is None or n.company_id != company.id or not (n.user_id == user.id or (n.user_id is None and n.role == user.role)):
            continue
        if user.id not in n.read_by:
            rb = n.read_by + [user.id]
            n.read_by_json = json.dumps(rb)
    db.commit()
    return JSONResponse({"ok": True})


@router.get("/api/notifications/unread")
def unread(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    items = [n for n in _my_notifications(db, user, request.state.company, 100) if user.id not in n.read_by]
    return {"count": len(items), "items": [{"id": n.id, "title": n.title, "body": n.body, "link": n.link,
                                           "level": n.level, "at": n.at.strftime("%d-%m %H:%M")} for n in items[:8]]}


@router.get("/lang/{lang}")
def set_lang(request: Request, lang: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    u = db.get(User, user.id)
    u.lang = "en" if lang == "en" else "th"
    db.commit()
    return RedirectResponse(request.headers.get("referer", "/"), 302)


@router.get("/roles")
def roles_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    from .. import perms, workflow
    from ..models import ROLES
    roles = [r for r in ROLES if r != "superadmin"]
    action_labels = {"submit": "act_submit", "foreman_approve": "act_approve", "foreman_reject": "act_reject",
                     "leader_assign": "act_assign", "leader_reject": "act_reject", "receive": "act_receive",
                     "complete": "act_complete", "tryout_accept": "act_tryout_ok", "tryout_return": "act_tryout_return",
                     "qc_ok": "act_qc_ok", "qc_ng": "act_qc_ng", "cancel": "act_cancel", "reopen": "act_reopen"}
    steps = {"submit": "1", "foreman_approve": "2", "foreman_reject": "2", "leader_assign": "3", "leader_reject": "3",
             "receive": "4", "complete": "4", "tryout_accept": "5", "tryout_return": "5", "qc_ok": "6", "qc_ng": "6",
             "cancel": "-", "reopen": "-"}
    actions = [(a, action_labels.get(a, a), steps.get(a, ""), workflow.ACTIONS[a][0]) for a in workflow.ACTIONS]
    counts = {r: db.query(User).filter(User.company_id == request.state.company.id, User.role == r, User.is_active == True).count()
              for r in roles} if request.state.company else {}
    return render(request, "roles.html", roles=roles, pages=perms.PAGES, visibility=perms.VISIBILITY, actions=actions,
                  counts=counts, db=db)
