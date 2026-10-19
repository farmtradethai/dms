from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session, selectinload
import time as _time

from .. import perms, approvals
from ..auth import get_current_user, require_user
from ..database import get_db, now_local, today_local
from ..models import ST, OPEN_STATUSES, Assignment, RepairRequest, User
from ..services import kpi, images
from ..web import render, STATUS_COLORS
from ..i18n import t

router = APIRouter()


def _status_rank():
    from sqlalchemy import case
    order = [ST["NG_RETURN"], ST["RETURNED"], ST["IN_PROGRESS"], ST["ASSIGNED"], ST["WAIT_LEADER"], ST["WAIT_FOREMAN"], ST["WAIT_QC"], ST["REPAIR_DONE"]]
    return case({s: i for i, s in enumerate(order)}, value=RepairRequest.status, else_=99)


@router.get("/")
def home(request: Request, user=Depends(get_current_user), db: Session = Depends(get_db)):
    if not user:
        return RedirectResponse("/login", 302)
    company = request.state.company
    if not company:
        return RedirectResponse("/admin/companies", 302)
    today = today_local()
    month_start = today.replace(day=1)
    s_month = kpi.summary(db, company.id, month_start, today)
    # "waiting for me" = the company's approval routing (same rule as the list tab and the approve buttons)
    mine = approvals.my_queue(db, user, company.id)
    base = perms.scope_requests(db.query(RepairRequest).filter(RepairRequest.company_id == company.id,
                                                               RepairRequest.is_deleted == False), user)
    queue = (base.filter(mine).order_by(RepairRequest.is_emergency.desc(), RepairRequest.target_date).limit(12).all())
    recent = base.order_by(RepairRequest.created_at.desc()).limit(8).all()
    # headline boxes use the SAME visibility as the list they link to (a requester sees only his own numbers)
    open_q = base.filter(RepairRequest.status.in_(OPEN_STATUSES))
    s_all = {"open": open_q.count(),
             "emergency_open": open_q.filter(RepairRequest.is_emergency == True).count(),
             "overdue_open": open_q.filter(RepairRequest.target_date < today).count(),
             "wait_qc": open_q.filter(RepairRequest.status == ST["WAIT_QC"]).count(),
             "queue": base.filter(mine).count(),
             "scope_all": perms.sees_all(user, db)}
    pm = kpi.pm_due(db, company.id, 14)
    storage = images.storage_stats(db, company.id) if user.is_admin or user.role == "manager" else None
    return render(request, "home.html", s_month=s_month, s_all=s_all, queue=queue, recent=recent,
                  pm_overdue=len(pm["overdue"]), pm_due=len(pm["due"]), storage=storage, db=db)


@router.get("/dashboard")
def dashboard(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db),
              dfrom: str = "", dto: str = ""):
    company = request.state.company
    today = today_local()
    start = datetime.strptime(dfrom, "%Y-%m-%d").date() if dfrom else (today - timedelta(days=180)).replace(day=1)
    end = datetime.strptime(dto, "%Y-%m-%d").date() if dto else today
    s = kpi.summary(db, company.id, start, end)
    b = kpi.breakdowns(db, company.id, start, end)
    board = kpi.technician_board(db, company.id)
    return render(request, "dashboard.html", s=s, b=b, board=board, dfrom=start.isoformat(), dto=end.isoformat(), db=db)


@router.get("/tv")
def tv(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    return render(request, "tv.html", tv_hours=request.state.company.settings.get("tv_working_hours", ""), db=db)


_TV_CACHE = {}
TV_CACHE_SECONDS = 50   # several TVs / browsers share one computation -> far fewer DB wake-ups on free Postgres
_TV_SLOW = {}           # 6-month charts change slowly: recomputed every 30 min
TV_SLOW_SECONDS = 1800


@router.get("/api/tv")
def api_tv(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db)):
    company = request.state.company
    key = (company.id, user.lang or "th")
    hit = _TV_CACHE.get(key)
    if hit and _time.time() - hit[0] < TV_CACHE_SECONDS:
        return hit[1]
    data = _build_tv(db, company, user)
    _TV_CACHE[key] = (_time.time(), data)
    return data


def _hours_text(h):
    if h is None:
        return "-"
    return f"{h:.0f} ชม." if h < 48 else f"{h / 24:.0f} วัน"


def _person(u, extra=None):
    d = {"id": u.id, "name": u.full_name, "short": _short_name(u.full_name), "photo": images.url_for(u.photo_path) if u.photo_path else "",
         "init": _initials(u.full_name), "hue": [210, 160, 25, 280, 340, 190, 45, 120][u.id % 8], "online": u.is_online}
    if extra:
        d.update(extra)
    return d


_TITLES = ("นางสาว", "นาย", "นาง", "Mr.", "Mrs.", "Ms.", "Miss")


def _short_name(n):
    n = (n or "").strip()
    for t_ in _TITLES:
        if n.startswith(t_):
            n = n[len(t_):].strip()
            break
    return n


def _initials(n):
    n = _short_name(n) or "?"
    return n[:2] if n[0] in "เแโใไ" else n[0].upper()


def _build_tv(db, company, user):
    from collections import Counter
    from sqlalchemy import func
    from .. import approvals
    today = today_local()
    now = now_local()
    lang = user.lang or "th"
    month_start = today.replace(day=1)
    s_month = kpi.summary(db, company.id, month_start, today)
    s_open = kpi.open_counts(db, company.id)
    slow = _TV_SLOW.get(company.id)
    if not slow or _time.time() - slow[0] > TV_SLOW_SECONDS:
        bb = kpi.breakdowns(db, company.id, month_start - timedelta(days=150), today)
        slow = (_time.time(), {"by_die": bb["by_die"][:7], "by_problem": bb["by_problem"][:7], "months": bb["months"][-6:]})
        _TV_SLOW[company.id] = slow
    b = slow[1]
    pm = kpi.pm_due(db, company.id, 7)
    day0 = datetime.combine(today, datetime.min.time())
    m0 = datetime.combine(month_start, datetime.min.time())
    base = db.query(RepairRequest).filter(RepairRequest.company_id == company.id, RepairRequest.is_deleted == False)
    new_today = base.filter(RepairRequest.created_at >= day0).count()
    closed_today = base.filter(RepairRequest.status == ST["CLOSED"], RepairRequest.closed_at >= day0).count()
    closed_month_q = base.filter(RepairRequest.status == ST["CLOSED"], RepairRequest.closed_at >= m0)
    closed_month = closed_month_q.count()
    ontime = closed_month_q.filter(RepairRequest.target_date != None,
                                   func.date(RepairRequest.closed_at) <= RepairRequest.target_date).count()
    from sqlalchemy.orm import load_only
    jobs = (base.options(selectinload(RepairRequest.assignments).selectinload(Assignment.user),
                         selectinload(RepairRequest.requester),
                         load_only(RepairRequest.id, RepairRequest.dr_no, RepairRequest.request_type, RepairRequest.part_no,
                                   RepairRequest.die_process, RepairRequest.line, RepairRequest.customer, RepairRequest.problem_text,
                                   RepairRequest.status, RepairRequest.progress_pct, RepairRequest.is_emergency, RepairRequest.target_date,
                                   RepairRequest.created_at, RepairRequest.updated_at, RepairRequest.ng_count, RepairRequest.requester_id,
                                   RepairRequest.foreman_at, RepairRequest.tech_completed_at, RepairRequest.tryout_at, RepairRequest.company_id))
            .filter(RepairRequest.status.in_(OPEN_STATUSES))
            .order_by(RepairRequest.is_emergency.desc(), _status_rank(), RepairRequest.target_date, RepairRequest.created_at.desc())
            .limit(120).all())
    rt = approvals.routing(db, company.id)
    waiting = Counter()
    oldest = {}
    people = {}
    out_jobs = []
    for r in jobs:
        step = approvals.STATUS_STEP.get(r.status)
        wait_for = []
        if step and r.status != ST["RETURNED"] and r.status != ST["NG_RETURN"]:
            if step == "TRYOUT" and approvals.requester_tries_out(r):
                wait_for = [r.requester]
            else:
                wait_for = [u for u, bk, _ in rt.approvers(step, r.line, include_backup=False)]
            entered = {ST["WAIT_FOREMAN"]: r.created_at, ST["WAIT_LEADER"]: r.foreman_at or r.created_at,
                       ST["REPAIR_DONE"]: r.tech_completed_at, ST["WAIT_QC"]: r.tryout_at}.get(r.status) or r.updated_at or r.created_at
            age = max(0.0, (now - entered).total_seconds() / 3600)
            for u in wait_for:
                waiting[u.id] += 1
                oldest[u.id] = max(oldest.get(u.id, 0), age)
                people[u.id] = u
        techs = [a.user for a in r.assignments if a.user]
        out_jobs.append({
            "id": r.id, "dr": r.dr_no, "type": r.request_type, "part": r.part_no or "-", "process": r.die_process or "",
            "line": r.line or "-", "customer": r.customer or "", "problem": r.problem_text or "-",
            "techs": [_person(u) for u in techs[:3]], "tech_more": max(0, len(techs) - 3),
            "status": r.status, "status_label": t(f"st_{r.status}", lang), "color": STATUS_COLORS.get(r.status, "gray"),
            "pct": r.progress_pct or 0, "emergency": bool(r.is_emergency),
            "target": r.target_date.strftime("%d-%m") if r.target_date else "-",
            "overdue": bool(r.target_date and r.target_date < today),
            "age": _hours_text((now - r.created_at).total_seconds() / 3600),
            "wait": ", ".join(_short_name(u.full_name).split(" ")[0] for u in wait_for[:2]) + (f" +{len(wait_for) - 2}" if len(wait_for) > 2 else ""),
            "ng": r.ng_count or 0,
        })
    # team: technicians + die leaders, with what they are doing right now
    board = kpi.technician_board(db, company.id)
    closed_by_tech = Counter(dict(
        db.query(Assignment.user_id, func.count(Assignment.id)).join(RepairRequest, Assignment.request_id == RepairRequest.id)
        .filter(RepairRequest.company_id == company.id, RepairRequest.status == ST["CLOSED"], RepairRequest.closed_at >= m0)
        .group_by(Assignment.user_id).all()))
    team = []
    for x in board:
        u = x["user"]
        working = [r for r in x["jobs"] if r.status == ST["IN_PROGRESS"]]
        cur = working[0] if working else None
        team.append(_person(u, {"role": "ช่าง", "shift": u.shift or "", "open": x["open"], "in_progress": x["in_progress"],
                                "emergency": x["emergency"], "closed_month": closed_by_tech.get(u.id, 0),
                                "doing": f"{cur.dr_no} · {cur.part_no}" if cur else "",
                                "state": "busy" if cur else ("queue" if x["open"] else "free")}))
    for u in rt.by_role.get("die_leader", []):
        team.append(_person(u, {"role": "หัวหน้า Die", "shift": u.shift or "", "open": waiting.get(u.id, 0), "in_progress": 0,
                                "emergency": 0, "closed_month": 0, "doing": "", "state": "lead"}))
    order = {"busy": 0, "queue": 1, "lead": 2, "free": 3}
    team.sort(key=lambda p: (order[p["state"]], -p["open"], p["name"]))
    approvers_wait = sorted(({**_person(people[i]), "count": c, "oldest": _hours_text(oldest[i]),
                              "late": oldest[i] > float(company.settings.get("approval_reminder_hours") or 8)}
                             for i, c in waiting.items()), key=lambda p: -p["count"])[:12]
    ticker = [f"🔥 {j['dr']} {j['part']} {j['process']} · {j['line']} · {j['status_label']}" for j in out_jobs if j["emergency"]][:8]
    ticker += [f"⏰ เกินกำหนด {j['dr']} ({j['target']}) · {j['part']} · {j['status_label']}" for j in out_jobs if j["overdue"] and not j["emergency"]][:8]
    ticker += [f"🛠 PM เกินกำหนด {d.part_no} {d.die_process}" for d in pm["overdue"][:5]]
    pm_list = [{"die": f"{d.part_no} {d.die_process}", "line": d.line or "-",
                "date": (d.pm_next_date.strftime("%d-%m") if d.pm_next_date else (f"{d.stroke_pct}% stroke" if d.pm_interval_strokes else "ยังไม่เคย PM")),
                "late": d in pm["overdue"]}
               for d in (pm["overdue"] + pm["due"])[:12]]
    mttr_h = round(s_month["mttr_minutes"] / 60, 1) if s_month["mttr_minutes"] else None
    return {
        "company": company.code, "company_name": company.name, "logo": company.logo_text, "now": now.strftime("%d-%m-%Y %H:%M:%S"),
        "kpi": {"open": s_open["open"], "emergency": s_open["emergency_open"], "overdue": s_open["overdue_open"],
                "approve": s_open["wait_foreman"] + s_open["wait_leader"], "tryout": s_open["repair_done"], "qc": s_open["wait_qc"],
                "new_today": new_today, "closed_today": closed_today, "closed_month": closed_month,
                "ontime": round(100 * ontime / closed_month) if closed_month else None,
                "mttr_h": mttr_h, "mtbf": s_month["mtbf_days"], "pm": s_month["pm_achievement"], "ng": s_month["ng_total"],
                "pm_overdue": len(pm["overdue"]), "pm_due": len(pm["due"])},
        "flow": [["รอ Foreman", s_open["wait_foreman"], "amber"], ["รอหัวหน้า Die", s_open["wait_leader"], "amber"],
                 ["มอบหมาย/กำลังซ่อม", s_open["in_progress"], "blue"], ["รอ Try-out", s_open["repair_done"], "violet"],
                 ["ส่งกลับซ่อม", s_open["ng_return"], "red"], ["รอ QC", s_open["wait_qc"], "teal"], ["ปิดเดือนนี้", closed_month, "green"]],
        "jobs": out_jobs, "team": team, "approvers": approvers_wait, "ticker": ticker, "pm": pm_list,
        "top_dies": b["by_die"][:7], "top_problems": b["by_problem"][:7], "months": b["months"][-6:],
    }
