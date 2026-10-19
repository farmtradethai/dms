"""Preventive maintenance engine.

A die can have a PM plan by DAYS (pm_interval_days), by STROKES (pm_interval_strokes) or both — whichever
comes first makes it due.  Everything PM-related reads from here: the PM page, calendar, annual plan,
TV / home counters, the daily auto-creation of PM jobs, the checklist on a PM job and the PM KPIs."""
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import mean

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import now_local, today_local
from ..models import (ST, OPEN_STATUSES, Company, Die, PMCheckResult, PMEvent, PMTemplate, PMTemplateItem, RepairRequest,
                      StrokeLog, User)

DEFAULT_TEMPLATES = [
    ("PM มาตรฐาน (ทุกแม่พิมพ์)", "", [
        ("ทั่วไป", "ทำความสะอาดแม่พิมพ์ / เศษ scrap / คราบน้ำมัน", "check"),
        ("ทั่วไป", "ตรวจรอยแตกร้าวของ Die set / Plate", "check"),
        ("ชุดบน", "ตรวจ Punch: บิ่น / สึก / หลวม", "check"),
        ("ชุดบน", "ความยาว Punch หลังเจียร", "value", "mm"),
        ("ชุดบน", "ตรวจ Stripper / PAD และน็อตยึด", "check"),
        ("ชุดล่าง", "ตรวจ Button Die / Insert: บิ่น / สึก / แตก", "check"),
        ("ชุดล่าง", "ตรวจคมตัด (Cutting edge)", "check"),
        ("ชุดล่าง", "ตรวจ Locater / Pilot / Guide", "check"),
        ("ระบบ", "ตรวจสปริง / แก๊สสปริง / ลิฟเตอร์ (ความแรง รั่ว หัก)", "check"),
        ("ระบบ", "ตรวจ Guide post / Bush และหล่อลื่น", "check"),
        ("ระบบ", "ขันน็อตทุกตัวตามค่าแรงบิด", "check"),
        ("ความปลอดภัย", "Safety block / Stopper อยู่ในสภาพดี", "check"),
        ("ผลลัพธ์", "Try-out ชิ้นงานแรกผ่าน (ขอบ / รู / ครีบ)", "check"),
    ]),
    ("PM แม่พิมพ์ Progressive", "progressive, prog", [
        ("ทั่วไป", "ทำความสะอาดแม่พิมพ์ / ร่อง scrap / chute", "check"),
        ("Strip", "ตรวจ Pilot / Lifter ระยะ feed ถูกต้อง", "check"),
        ("Strip", "ตรวจ Stock guide / Misfeed sensor", "check"),
        ("ชุดบน", "ตรวจ Punch ทุก station: บิ่น / สึก / หลวม", "check"),
        ("ชุดบน", "ความยาว Punch หลังเจียร (ตัวที่สั้นสุด)", "value", "mm"),
        ("ชุดล่าง", "ตรวจ Button / Die insert ทุก station", "check"),
        ("ชุดล่าง", "ตรวจคมตัด Trim / Cut-off", "check"),
        ("ระบบ", "ตรวจสปริง / แก๊สสปริง / ลิฟเตอร์", "check"),
        ("ระบบ", "หล่อลื่น Guide post / Bush", "check"),
        ("ระบบ", "ขันน็อตทุกตัวตามค่าแรงบิด", "check"),
        ("ความปลอดภัย", "Safety block / Stopper อยู่ในสภาพดี", "check"),
        ("ผลลัพธ์", "ความสูงครีบ (Burr) ชิ้นงานหลัง Try-out", "value", "mm"),
    ]),
]


# ================================================================== templates / checklist
def ensure_templates(db: Session, company_id: int):
    if db.query(PMTemplate).filter(PMTemplate.company_id == company_id).count():
        return
    for name, match, items in DEFAULT_TEMPLATES:
        t = PMTemplate(company_id=company_id, name=name, match=match)
        for i, it in enumerate(items):
            t.items.append(PMTemplateItem(sort=i, section=it[0], text=it[1], kind=it[2], unit=it[3] if len(it) > 3 else ""))
        db.add(t)
    db.flush()


def template_for(db: Session, die: Die):
    if die is None:
        return None
    if die.pm_template_id:
        t = db.get(PMTemplate, die.pm_template_id)
        if t and t.is_active and t.company_id == die.company_id:
            return t
    temps = db.query(PMTemplate).filter(PMTemplate.company_id == die.company_id, PMTemplate.is_active == True).order_by(PMTemplate.id).all()
    proc = f"{die.die_process or ''} {die.part_name or ''}".lower()
    for t in temps:
        words = [w.strip().lower() for w in (t.match or "").split(",") if w.strip()]
        if words and any(w in proc for w in words):
            return t
    return next((t for t in temps if not (t.match or "").strip()), temps[0] if temps else None)


def attach_checklist(db: Session, req: RepairRequest, force=False):
    """Copy the die's checklist into the PM job (only once, unless force)."""
    if req.request_type != "PM" or (req.pm_checks and not force):
        return 0
    ensure_templates(db, req.company_id)
    t = template_for(db, req.die)
    if not t:
        return 0
    for c in list(req.pm_checks):
        req.pm_checks.remove(c)
    for it in t.items:
        req.pm_checks.append(PMCheckResult(item_id=it.id, sort=it.sort, section=it.section, text=it.text, kind=it.kind,
                                           unit=it.unit, spec_min=it.spec_min, spec_max=it.spec_max))
    db.flush()
    return len(t.items)


def checklist_state(req: RepairRequest) -> dict:
    rows = list(req.pm_checks)
    answered = [c for c in rows if c.result]
    ng = [c for c in rows if c.result == "NG"]
    return {"total": len(rows), "answered": len(answered), "ng": len(ng), "done": bool(rows) and len(answered) == len(rows),
            "pct": round(100 * len(answered) / len(rows)) if rows else 0}


def judge_value(c: PMCheckResult):
    if c.value is None:
        return None
    if c.spec_min is not None and c.value < c.spec_min:
        return "NG"
    if c.spec_max is not None and c.value > c.spec_max:
        return "NG"
    return "OK"


# ================================================================== due status
def next_date(d: Die):
    if not d.pm_interval_days:
        return None
    return d.pm_next_date or (d.pm_last_date + timedelta(days=d.pm_interval_days) if d.pm_last_date else None)


def status(d: Die, today: date, horizon: int = 14, stroke_due_pct: int = 90) -> dict:
    """state: overdue / due / ok / none ; reason: date / stroke / never."""
    st = {"state": "none", "reason": "", "next": next_date(d), "days_left": None, "stroke_pct": d.stroke_pct}
    states = []
    if d.pm_interval_days:
        nd = st["next"]
        if nd is None:
            states.append(("due", "never"))
        else:
            st["days_left"] = (nd - today).days
            states.append(("overdue" if nd < today else ("due" if nd <= today + timedelta(days=horizon) else "ok"), "date"))
    if d.pm_interval_strokes:
        p = d.stroke_pct or 0
        states.append(("overdue" if p >= 100 else ("due" if p >= stroke_due_pct else "ok"), "stroke"))
    if not states:
        return st
    rank = {"overdue": 0, "due": 1, "ok": 2}
    states.sort(key=lambda x: rank[x[0]])
    st["state"], st["reason"] = states[0]
    return st


def due_groups(db: Session, company_id: int, horizon: int = 14, line: str = "", customer: str = ""):
    company = db.get(Company, company_id)
    pct = int((company.settings if company else {}).get("pm_due_stroke_pct", 90) or 90)
    today = today_local()
    q = db.query(Die).filter(Die.company_id == company_id, Die.is_active == True,
                             or_(Die.pm_interval_days > 0, Die.pm_interval_strokes > 0))
    if line:
        q = q.filter(Die.line == line)
    if customer:
        q = q.filter(Die.customer == customer)
    out = {"overdue": [], "due": [], "upcoming": [], "status": {}}
    for d in q.all():
        s = status(d, today, horizon, pct)
        out["status"][d.id] = s
        out[{"overdue": "overdue", "due": "due"}.get(s["state"], "upcoming")].append(d)
    key = lambda d: (out["status"][d.id]["next"] or date.max, -(d.stroke_pct or 0))
    for k in ("overdue", "due", "upcoming"):
        out[k].sort(key=key)
    return out


def open_pm_die_ids(db: Session, company_id: int) -> set:
    return {r[0] for r in db.query(RepairRequest.die_id).filter(RepairRequest.company_id == company_id, RepairRequest.request_type == "PM",
                                                                RepairRequest.is_deleted == False, RepairRequest.status.in_(OPEN_STATUSES))}


# ================================================================== creating PM jobs
def open_pm_job(db: Session, company: Company, die: Die, user: User, target: date = None, note: str = "", auto=False) -> RepairRequest:
    from .. import workflow
    from ..routers.requests import next_dr_no
    target = target or next_date(die) or today_local() + timedelta(days=7)
    if target < today_local():
        target = today_local() + timedelta(days=1)
    plan = []
    if die.pm_interval_days:
        plan.append(f"ทุก {die.pm_interval_days} วัน")
    if die.pm_interval_strokes:
        plan.append(f"ทุก {die.pm_interval_strokes:,} stroke (ใช้ไป {die.strokes_since_pm:,})")
    req = RepairRequest(company_id=company.id, dr_no=next_dr_no(db, company), request_type="PM", die_id=die.id, die_code=die.die_code,
                        customer=die.customer, model=die.model, part_no=die.part_no, part_name=die.part_name, die_process=die.die_process,
                        line=die.line, machine_no=die.machine_no, required_day=">7", target_date=target, problem_text="PM",
                        other_details=(note.strip() or ("PM ตามแผน (สร้างอัตโนมัติ)" if auto else "PM ตามแผน")) + (" · " + ", ".join(plan) if plan else ""),
                        department=user.department or "Die Maintenance", requester_id=user.id, requester_name=user.full_name,
                        requester_phone=user.phone, status=ST["DRAFT"],
                        pm_plan_note=f"interval={die.pm_interval_days}d/{die.pm_interval_strokes or 0}st last={die.pm_last_date or '-'}")
    db.add(req)
    db.flush()
    attach_checklist(db, req)
    workflow.apply(db, req, "submit", user, company_settings=company.settings)
    return req


def system_user(db: Session, company: Company) -> User:
    """Requester used for automatically created PM jobs: the first active die leader, else an admin."""
    for role in ("die_leader", "admin", "manager"):
        u = db.query(User).filter(User.company_id == company.id, User.role == role, User.is_active == True).order_by(User.id).first()
        if u:
            return u
    return db.query(User).filter(User.role == "superadmin").first()


def auto_create(db: Session, company: Company) -> list:
    """Daily job: open PM jobs for dies due within `pm_auto_create_days` (or stroke-due) without an open PM job."""
    days = int(company.settings.get("pm_auto_create_days") or 0)
    if days <= 0:
        return []
    groups = due_groups(db, company.id, days)
    busy = open_pm_die_ids(db, company.id)
    u = system_user(db, company)
    if not u:
        return []
    made = []
    for d in groups["overdue"] + groups["due"]:
        if d.id in busy:
            continue
        made.append(open_pm_job(db, company, d, u, auto=True))
    return made


def on_pm_closed(db: Session, req: RepairRequest, user: User):
    """Called by the workflow when QC closes a PM job."""
    die = req.die
    if not die:
        return
    die.strokes_at_last_pm = die.stroke_count or 0
    db.add(PMEvent(company_id=req.company_id, die_id=die.id, kind="DONE", from_date=req.target_date, to_date=(req.closed_at or now_local()).date(),
                   reason=req.dr_no, request_id=req.id, user_name=user.full_name))


# ================================================================== strokes
def record_strokes(db: Session, company_id: int, die: Die, value: int, mode: str = "add", source: str = "MANUAL",
                   user_name: str = "", work_date: date = None, note: str = "") -> StrokeLog:
    value = int(value)
    if value < 0:
        raise ValueError("จำนวน stroke ต้องไม่ติดลบ")
    before = die.stroke_count or 0
    if mode == "total":
        if value < before:
            raise ValueError(f"ยอดสะสมใหม่ ({value:,}) น้อยกว่ายอดเดิม ({before:,}) — ถ้าเปลี่ยน counter ให้ใช้แบบ 'เพิ่ม'")
        add = value - before
    else:
        add = value
    die.stroke_count = before + add
    log = StrokeLog(company_id=company_id, die_id=die.id, strokes=add, total_after=die.stroke_count, source=source,
                    user_name=user_name, work_date=work_date or today_local(), note=(note or "")[:200])
    db.add(log)
    return log


def find_die(db: Session, company_id: int, code: str = "", part_no: str = "", process: str = ""):
    code, part_no, process = (code or "").strip(), (part_no or "").strip(), (process or "").strip()
    q = db.query(Die).filter(Die.company_id == company_id)
    if code:
        d = q.filter(Die.die_code == code).first()
        if d:
            return d
    if part_no:
        qq = q.filter(Die.part_no == part_no)
        if process:
            qq = qq.filter(Die.die_process == process)
        rows = qq.limit(2).all()
        if len(rows) == 1:
            return rows[0]
    return None


# ================================================================== plan / calendar
def planned_dates(d: Die, start: date, end: date, after: date = None) -> list:
    """Projected PM dates of a day-based plan inside [start, end]: the next due date, then every interval after it.
    `after` = target of a PM job already open -> the projection continues from that job instead."""
    if not d.pm_interval_days:
        return []
    step = timedelta(days=d.pm_interval_days)
    x = (after + step) if after else (next_date(d) or today_local())
    if x < start:
        x += step * ((start - x).days // d.pm_interval_days)
        while x < start:
            x += step
    out = []
    while x <= end:
        out.append(x)
        x += step
    return out


def _merge_dates(dates, tol=6):
    out = []
    for x in sorted(dates):
        if not out or (x - out[-1]).days > tol:
            out.append(x)
    return out


def year_plan(db: Session, company_id: int, year: int, line: str = "", customer: str = ""):
    start, end = date(year, 1, 1), date(year, 12, 31)
    q = db.query(Die).filter(Die.company_id == company_id, Die.is_active == True, or_(Die.pm_interval_days > 0, Die.pm_interval_strokes > 0))
    if line:
        q = q.filter(Die.line == line)
    if customer:
        q = q.filter(Die.customer == customer)
    dies = q.order_by(Die.line, Die.customer, Die.part_no, Die.die_process).all()
    ids = [d.id for d in dies]
    done = defaultdict(list)
    t0, t1 = datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.max.time())
    if ids:
        for r in (db.query(RepairRequest).filter(RepairRequest.company_id == company_id, RepairRequest.request_type == "PM",
                                                 RepairRequest.die_id.in_(ids), RepairRequest.status == ST["CLOSED"],
                                                 RepairRequest.closed_at >= t0, RepairRequest.closed_at <= t1)):
            done[r.die_id].append((r.closed_at.date(), r.id, r.dr_no, r.target_date))
    post = defaultdict(list)
    if ids:
        for e in db.query(PMEvent).filter(PMEvent.company_id == company_id, PMEvent.kind == "POSTPONE", PMEvent.die_id.in_(ids),
                                          PMEvent.at >= t0, PMEvent.at <= t1):
            post[e.die_id].append(e)
    today = today_local()
    targets = defaultdict(list)          # what was actually planned in the past = target dates of the PM jobs
    open_t = {}
    if ids:
        for r in (db.query(RepairRequest.die_id, RepairRequest.target_date).filter(
                RepairRequest.company_id == company_id, RepairRequest.request_type == "PM", RepairRequest.die_id.in_(ids),
                RepairRequest.is_deleted == False, RepairRequest.status.in_(OPEN_STATUSES))):
            if r[1]:
                open_t[r[0]] = max(open_t.get(r[0], r[1]), r[1])
        for r in (db.query(RepairRequest.die_id, RepairRequest.target_date).filter(
                RepairRequest.company_id == company_id, RepairRequest.request_type == "PM", RepairRequest.die_id.in_(ids),
                RepairRequest.is_deleted == False, RepairRequest.status != ST["CANCELLED"],
                RepairRequest.target_date.between(start, end))):
            targets[r[0]].append(r[1])
    rows = []
    tot_p = tot_a = tot_late = 0
    for d in dies:
        months = [{"p": [], "a": [], "late": False, "post": []} for _ in range(12)]
        nd = next_date(d)
        plan = list(targets[d.id]) + planned_dates(d, max(start, today), end, open_t.get(d.id))
        if nd and start <= nd < today and d.id not in open_t:
            plan.append(nd)                      # overdue: still planned, not done
        for x in _merge_dates(plan):
            months[x.month - 1]["p"].append(x)
        for dt, rid, dr, tgt in done[d.id]:
            months[dt.month - 1]["a"].append({"date": dt, "id": rid, "dr": dr, "late": bool(tgt and dt > tgt)})
        for e in post[d.id]:
            m = (e.from_date or e.at.date()).month
            if (e.from_date or e.at.date()).year == year:
                months[m - 1]["post"].append(e)
        for m in months:
            past_plan = [x for x in m["p"] if x < today]
            m["late"] = bool(past_plan and not m["a"])
        p = sum(len(m["p"]) for m in months)
        a = sum(len(m["a"]) for m in months)
        tot_p += p
        tot_a += a
        tot_late += sum(1 for m in months if m["late"])
        rows.append({"die": d, "months": months, "planned": p, "actual": a})
    return {"rows": rows, "planned": tot_p, "actual": tot_a, "late_months": tot_late, "year": year}


def month_calendar(db: Session, company_id: int, year: int, month: int):
    first = date(year, month, 1)
    last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
    plan = defaultdict(list)
    dies = db.query(Die).filter(Die.company_id == company_id, Die.is_active == True, Die.pm_interval_days > 0).all()
    today = today_local()
    open_t = {}
    for r in db.query(RepairRequest.die_id, RepairRequest.target_date).filter(
            RepairRequest.company_id == company_id, RepairRequest.request_type == "PM", RepairRequest.is_deleted == False,
            RepairRequest.status.in_(OPEN_STATUSES)):
        if r[0] and r[1]:
            open_t[r[0]] = max(open_t.get(r[0], r[1]), r[1])
    for d in dies:
        nd = next_date(d)
        extra = [nd] if (nd and first <= nd < today and d.id not in open_t) else []
        for x in planned_dates(d, max(first, today), last, open_t.get(d.id)) + extra:
            plan[x].append(d)
    t0, t1 = datetime.combine(first, datetime.min.time()), datetime.combine(last, datetime.max.time())
    jobs = defaultdict(list)
    for r in db.query(RepairRequest).filter(RepairRequest.company_id == company_id, RepairRequest.request_type == "PM",
                                            RepairRequest.is_deleted == False, RepairRequest.status != ST["CANCELLED"],
                                            or_(RepairRequest.closed_at.between(t0, t1),
                                                RepairRequest.target_date.between(first, last))):
        day = r.closed_at.date() if (r.closed_at and r.status == ST["CLOSED"]) else r.target_date
        if day and first <= day <= last:
            jobs[day].append(r)
    weeks, cur = [], first - timedelta(days=first.weekday())
    while cur <= last or cur.weekday() != 0:
        if cur.weekday() == 0:
            weeks.append([])
        weeks[-1].append({"date": cur, "in": cur.month == month, "plan": plan.get(cur, []), "jobs": jobs.get(cur, [])})
        cur += timedelta(days=1)
        if cur > last and cur.weekday() == 0:
            break
    return {"weeks": weeks, "first": first, "last": last, "n_plan": sum(len(v) for v in plan.values()),
            "n_jobs": sum(len(v) for v in jobs.values())}


# ================================================================== KPIs + suggestions
def kpis(db: Session, company_id: int, start: date, end: date) -> dict:
    t0, t1 = datetime.combine(start, datetime.min.time()), datetime.combine(end, datetime.max.time())
    # legacy imports are excluded: their "target" is request date + required day, not a PM plan date
    pm_planned = (db.query(RepairRequest).filter(RepairRequest.company_id == company_id, RepairRequest.request_type == "PM",
                                                 RepairRequest.is_deleted == False, RepairRequest.legacy_id == None,
                                                 RepairRequest.status.notin_([ST["CANCELLED"], ST["FOREMAN_REJECTED"], ST["LEADER_REJECTED"]]),
                                                 RepairRequest.target_date.between(start, end)).all())
    on_time = [r for r in pm_planned if r.closed_at and r.target_date and r.closed_at.date() <= r.target_date]
    closed_pm = (db.query(RepairRequest).filter(RepairRequest.company_id == company_id, RepairRequest.request_type == "PM",
                                                RepairRequest.status == ST["CLOSED"], RepairRequest.closed_at.between(t0, t1)).all())
    repairs = (db.query(RepairRequest).filter(RepairRequest.company_id == company_id, RepairRequest.request_type != "PM",
                                              RepairRequest.is_deleted == False, RepairRequest.created_at.between(t0, t1 + timedelta(days=30))).all())
    by_die = defaultdict(list)
    for r in repairs:
        if r.die_id:
            by_die[r.die_id].append(r.created_at)
    after = 0
    for p in closed_pm:
        if p.die_id and any(p.closed_at < x <= p.closed_at + timedelta(days=30) for x in by_die.get(p.die_id, [])):
            after += 1
    n_rep = sum(1 for r in repairs if r.created_at <= t1)
    mins = [r.net_repair_minutes for r in closed_pm if r.net_repair_minutes]
    ng_items = (db.query(PMCheckResult).join(RepairRequest, PMCheckResult.request_id == RepairRequest.id)
                .filter(RepairRequest.company_id == company_id, PMCheckResult.result == "NG", PMCheckResult.at.between(t0, t1)).all())
    ng_top = defaultdict(int)
    for c in ng_items:
        ng_top[c.text] += 1
    return {
        "planned": len(pm_planned), "on_time": len(on_time),
        "compliance": round(100 * len(on_time) / len(pm_planned)) if pm_planned else None,
        "done": len(closed_pm), "repairs": n_rep,
        "pm_ratio": round(100 * len(closed_pm) / (len(closed_pm) + n_rep)) if (closed_pm or n_rep) else None,
        "fail_after": after, "fail_after_pct": round(100 * after / len(closed_pm)) if closed_pm else None,
        "avg_minutes": round(mean(mins)) if mins else None,
        "ng_top": sorted(ng_top.items(), key=lambda x: -x[1])[:8],
    }


def suggestions(db: Session, company_id: int, days: int = 365, limit: int = 40) -> list:
    """Dies that break often: suggest a PM interval = 70% of their MTBF (rounded to weeks, 7..180 days)."""
    since = now_local() - timedelta(days=days)
    rows = (db.query(RepairRequest.die_id, RepairRequest.created_at).filter(RepairRequest.company_id == company_id,
                                                                            RepairRequest.request_type == "REPAIR",
                                                                            RepairRequest.die_id != None,
                                                                            RepairRequest.created_at >= since).all())
    ts = defaultdict(list)
    for die_id, at in rows:
        ts[die_id].append(at)
    out = []
    dies = {d.id: d for d in db.query(Die).filter(Die.company_id == company_id, Die.id.in_(list(ts) or [-1]))}
    for die_id, t in ts.items():
        if len(t) < 3 or die_id not in dies:
            continue
        t.sort()
        gaps = [(b - a).total_seconds() / 86400 for a, b in zip(t, t[1:])]
        gaps = [g for g in gaps if g >= 0.5] or gaps          # same-day re-reports are one failure
        mtbf = mean(gaps)
        sug = int(min(180, max(7, round(mtbf * 0.7 / 7) * 7)))
        d = dies[die_id]
        cur = d.pm_interval_days or 0
        if cur and cur <= sug * 1.25:
            continue                                            # current plan is already short enough
        out.append({"die": d, "repairs": len(t), "mtbf": round(mtbf, 1), "suggest": sug, "current": cur,
                    "why": "ยังไม่มีแผน PM" if not cur else f"รอบปัจจุบัน {cur} วัน ยาวกว่าระยะเสียเฉลี่ย"})
    out.sort(key=lambda x: (-x["repairs"], x["mtbf"]))
    return out[:limit]
