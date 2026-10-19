"""KPI maths. All times are naive factory local time.

net_minutes_between(): minutes between two timestamps minus every break window that falls inside.
Break windows are taken from the company's shift settings (DAY/NIGHT, each with a list of [start,end]).
Both shifts' breaks are merged into one daily list, which is what the factory asked for
(10:00-10:10, 12:00-13:00, 15:00-15:10, 17:00-17:30, 22:00-22:10, 00:00-01:00, 03:00-03:10, 05:00-05:30)."""
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from statistics import mean, median

from sqlalchemy import func
from sqlalchemy.orm import defer, Session, selectinload

from ..models import ST, Assignment, Die, RepairRequest, OPEN_STATUSES
from ..database import today_local


def _parse_hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def daily_breaks(shifts: dict):
    windows = []
    for sh in (shifts or {}).values():
        for b in sh.get("breaks", []):
            try:
                windows.append((_parse_hm(b[0]), _parse_hm(b[1])))
            except Exception:
                continue
    return windows


def net_minutes_between(start: datetime, end: datetime, shifts: dict) -> int:
    if not start or not end or end <= start:
        return 0
    total = (end - start).total_seconds() / 60
    windows = daily_breaks(shifts)
    deducted = 0.0
    day = start.date() - timedelta(days=1)
    last_day = end.date()
    while day <= last_day:
        for b_start, b_end in windows:
            bs = datetime.combine(day, b_start)
            be = datetime.combine(day, b_end)
            if be <= bs:  # crosses midnight (e.g. 23:50-00:10)
                be += timedelta(days=1)
            lo, hi = max(bs, start), min(be, end)
            if hi > lo:
                deducted += (hi - lo).total_seconds() / 60
        day += timedelta(days=1)
    return int(round(max(total - deducted, 0)))


def fmt_minutes(m):
    if m is None:
        return "-"
    m = int(m)
    if m < 60:
        return f"{m} นาที"
    h, mm = divmod(m, 60)
    if h < 48:
        return f"{h} ชม. {mm} นาที" if mm else f"{h} ชม."
    d, hh = divmod(h, 24)
    return f"{d} วัน {hh} ชม."


def hours(a: datetime, b: datetime):
    if not a or not b:
        return None
    return round((b - a).total_seconds() / 3600, 1)


# ---------------------------------------------------------------- aggregates
def _period_rows(db: Session, company_id: int, start: date = None, end: date = None, with_users=False):
    """One query for the jobs + one per relationship (selectinload) instead of one query per job."""
    opt = selectinload(RepairRequest.assignments)
    if with_users:
        opt = opt.selectinload(Assignment.user)
    # long free-text columns are never used by the KPIs -> not transferred (free Postgres meters every byte read)
    heavy = defer(RepairRequest.other_details), defer(RepairRequest.action_text), defer(RepairRequest.foreman_comment), \
        defer(RepairRequest.leader_comment), defer(RepairRequest.tryout_result), defer(RepairRequest.qc_comment), \
        defer(RepairRequest.pm_plan_note), defer(RepairRequest.requester_phone), defer(RepairRequest.part_name)
    q = (db.query(RepairRequest).options(opt, *heavy)
         .filter(RepairRequest.company_id == company_id, RepairRequest.is_deleted == False))
    if start:
        q = q.filter(RepairRequest.created_at >= datetime.combine(start, time.min))
    if end:
        q = q.filter(RepairRequest.created_at <= datetime.combine(end, time.max))
    return q.all()

def summary(db: Session, company_id: int, start: date = None, end: date = None) -> dict:
    """Headline numbers for dashboard / TV. Period filter applies to created_at."""
    rows = _period_rows(db, company_id, start, end, with_users=False)

    open_rows = [r for r in rows if r.status in OPEN_STATUSES]
    closed = [r for r in rows if r.status == ST["CLOSED"]]
    today = today_local()

    def _stage(r_list, a, b):
        vals = [hours(getattr(r, a), getattr(r, b)) for r in r_list]
        vals = [v for v in vals if v is not None and v >= 0]
        return round(median(vals), 1) if vals else None

    done = [r for r in rows if r.tech_completed_at]
    net = [r.net_repair_minutes for r in done if r.net_repair_minutes]
    mttr_min = round(mean(net)) if net else None

    # MTBF: average days between consecutive failures of the same die (repair jobs only)
    by_die = defaultdict(list)
    for r in rows:
        if r.request_type == "REPAIR" and r.die_id:
            by_die[r.die_id].append(r.created_at)
    gaps = []
    for ts in by_die.values():
        ts.sort()
        gaps += [(b - a).total_seconds() / 86400 for a, b in zip(ts, ts[1:])]
    mtbf_days = round(mean(gaps), 1) if gaps else None

    # PM achievement: PM jobs PLANNED (target date) in the period that were closed on/before their target
    pm_jobs = [r for r in rows if r.request_type == "PM"]
    if start or end:
        from ..models import RepairRequest as _R
        q = db.query(_R).filter(_R.company_id == company_id, _R.request_type == "PM", _R.is_deleted == False, _R.legacy_id == None,
                                _R.status.notin_([ST["CANCELLED"], ST["FOREMAN_REJECTED"], ST["LEADER_REJECTED"]]))
        if start:
            q = q.filter(_R.target_date >= start)
        if end:
            q = q.filter(_R.target_date <= end)
        planned = q.all()
    else:
        planned = [r for r in pm_jobs if r.legacy_id is None and r.status not in (ST["CANCELLED"], ST["FOREMAN_REJECTED"], ST["LEADER_REJECTED"])]
    pm_done_on_time = [r for r in planned if r.closed_at and r.target_date and r.closed_at.date() <= r.target_date]
    pm_ach = round(100 * len(pm_done_on_time) / len(planned)) if planned else None

    status_counts = defaultdict(int)
    for r in rows:
        status_counts[r.status] += 1

    return {
        "total": len(rows),
        "open": len(open_rows),
        "closed": len(closed),
        "emergency_open": sum(1 for r in open_rows if r.is_emergency),
        "overdue_open": sum(1 for r in open_rows if r.target_date and r.target_date < today),
        "wait_foreman": status_counts[ST["WAIT_FOREMAN"]],
        "wait_leader": status_counts[ST["WAIT_LEADER"]],
        "in_progress": status_counts[ST["IN_PROGRESS"]] + status_counts[ST["ASSIGNED"]],
        "repair_done": status_counts[ST["REPAIR_DONE"]],
        "wait_qc": status_counts[ST["WAIT_QC"]],
        "ng_return": status_counts[ST["NG_RETURN"]] + status_counts[ST["RETURNED"]],
        "ng_total": sum(r.ng_count or 0 for r in rows),
        "mttr_minutes": mttr_min,
        "mttr_text": fmt_minutes(mttr_min) if mttr_min else "-",
        "mtbf_days": mtbf_days,
        "pm_achievement": pm_ach,
        "pm_jobs": len(pm_jobs),
        "lead_req_to_foreman_h": _stage(rows, "created_at", "foreman_at"),
        "lead_foreman_to_leader_h": _stage(rows, "foreman_at", "leader_at"),
        "lead_leader_to_receive_h": _stage(rows, "leader_at", "tech_received_at"),
        "lead_receive_to_complete_h": _stage(rows, "tech_received_at", "tech_completed_at"),
        "lead_complete_to_tryout_h": _stage(rows, "tech_completed_at", "tryout_at"),
        "lead_tryout_to_qc_h": _stage(rows, "tryout_at", "qc_at"),
        "lead_total_h": _stage(closed, "created_at", "closed_at"),
        "lead_to_repair_done_h": _stage(done, "created_at", "tech_completed_at"),
        "parts_cost": round(sum(r.parts_cost or 0 for r in rows), 2),
        "downtime_minutes": sum(r.downtime_minutes or 0 for r in rows),
        "status_counts": dict(status_counts),
    }


def breakdowns(db: Session, company_id: int, start: date = None, end: date = None) -> dict:
    rows = _period_rows(db, company_id, start, end, with_users=True)

    def count_by(fn):
        d = defaultdict(int)
        for r in rows:
            k = fn(r) or "-"
            d[k] += 1
        return sorted(d.items(), key=lambda x: -x[1])

    def sum_by(fn, attr):
        d = defaultdict(float)
        for r in rows:
            d[fn(r) or "-"] += getattr(r, attr) or 0
        return sorted(d.items(), key=lambda x: -x[1])

    by_month = defaultdict(lambda: {"REPAIR": 0, "PM": 0, "ACCIDENT": 0, "closed": 0, "net_minutes": [], "ng": 0})
    for r in rows:
        k = r.created_at.strftime("%Y-%m")
        by_month[k][r.request_type or "REPAIR"] += 1
        if r.status == ST["CLOSED"]:
            by_month[k]["closed"] += 1
        if r.net_repair_minutes:
            by_month[k]["net_minutes"].append(r.net_repair_minutes)
        by_month[k]["ng"] += r.ng_count or 0
    months = []
    for k in sorted(by_month):
        v = by_month[k]
        months.append({"month": k, "repair": v["REPAIR"], "pm": v["PM"], "accident": v["ACCIDENT"], "closed": v["closed"],
                       "mttr": round(mean(v["net_minutes"])) if v["net_minutes"] else 0, "ng": v["ng"]})

    tech = defaultdict(lambda: {"jobs": 0, "open": 0, "net": 0})
    for r in rows:
        for a in r.assignments:
            if not a.user:
                continue
            tech[a.user.full_name]["jobs"] += 1
            if r.status in OPEN_STATUSES:
                tech[a.user.full_name]["open"] += 1
            tech[a.user.full_name]["net"] += r.net_repair_minutes or 0

    return {
        "by_problem": count_by(lambda r: r.problem_text),
        "by_root_cause": count_by(lambda r: r.root_cause_text),
        "by_customer": count_by(lambda r: r.customer),
        "by_line": count_by(lambda r: r.line),
        "by_type": count_by(lambda r: r.request_type),
        "by_required_day": count_by(lambda r: r.required_day),
        "by_die": count_by(lambda r: f"{r.part_no} | {r.die_process}")[:15],
        "cost_by_die": [(k, round(v, 2)) for k, v in sum_by(lambda r: f"{r.part_no} | {r.die_process}", "parts_cost")[:15] if v],
        "months": months,
        "technicians": sorted(tech.items(), key=lambda x: -x[1]["jobs"]),
    }


def technician_board(db: Session, company_id: int):
    """Who is doing what right now (for leader assignment + TV). Reads only 5 small columns of the open jobs."""
    from types import SimpleNamespace
    from ..models import User
    techs = db.query(User).filter(User.company_id == company_id, User.role == "technician", User.is_active == True).all()
    rows = (db.query(Assignment.user_id, RepairRequest.status, RepairRequest.is_emergency, RepairRequest.dr_no, RepairRequest.part_no)
            .join(RepairRequest, Assignment.request_id == RepairRequest.id)
            .filter(RepairRequest.company_id == company_id, RepairRequest.is_deleted == False,
                    RepairRequest.status.in_(OPEN_STATUSES)).all())
    by_user = defaultdict(list)
    for uid, st, em, dr, pn in rows:
        by_user[uid].append(SimpleNamespace(status=st, is_emergency=em, dr_no=dr, part_no=pn))
    board = []
    for t in techs:
        mine = by_user.get(t.id, [])
        board.append({
            "user": t,
            "open": len(mine),
            "in_progress": sum(1 for r in mine if r.status == ST["IN_PROGRESS"]),
            "emergency": sum(1 for r in mine if r.is_emergency),
            "jobs": mine,
        })
    board.sort(key=lambda b: (b["open"], b["user"].full_name))
    return board


def open_counts(db: Session, company_id: int) -> dict:
    """Open-job counters straight from SQL (no rows transferred)."""
    from sqlalchemy import case
    today = today_local()
    base = db.query(RepairRequest).filter(RepairRequest.company_id == company_id, RepairRequest.is_deleted == False,
                                          RepairRequest.status.in_(OPEN_STATUSES))
    by = dict(base.with_entities(RepairRequest.status, func.count(RepairRequest.id)).group_by(RepairRequest.status).all())
    em, od = base.with_entities(func.sum(case((RepairRequest.is_emergency == True, 1), else_=0)),
                                func.sum(case((RepairRequest.target_date < today, 1), else_=0))).one()
    g = lambda k: by.get(ST[k], 0)
    return {"open": sum(by.values()), "emergency_open": int(em or 0), "overdue_open": int(od or 0),
            "wait_foreman": g("WAIT_FOREMAN"), "wait_leader": g("WAIT_LEADER"), "in_progress": g("IN_PROGRESS") + g("ASSIGNED"),
            "repair_done": g("REPAIR_DONE"), "wait_qc": g("WAIT_QC"), "ng_return": g("NG_RETURN") + g("RETURNED")}


def pm_due(db: Session, company_id: int, horizon_days: int = 7):
    """Dies whose PM is overdue / due (by days OR by strokes) — see services/pm.py."""
    from . import pm as pm_service
    return pm_service.due_groups(db, company_id, horizon_days)
