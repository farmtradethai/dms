from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import or_
from sqlalchemy.orm import Session, selectinload

from .. import perms
from ..auth import require_user
from ..database import get_db, today_local, now_local
from ..models import ST, OPEN_STATUSES, Assignment, Die, RepairRequest, User
from ..services import export, kpi
from ..web import render

router = APIRouter()


def _filtered(db, company_id, dfrom, dto, rtype="", line="", customer="", status="", tech=""):
    q = (db.query(RepairRequest)
         .options(selectinload(RepairRequest.assignments).selectinload(Assignment.user),
                  selectinload(RepairRequest.foreman), selectinload(RepairRequest.leader))
         .filter(RepairRequest.company_id == company_id, RepairRequest.is_deleted == False))
    if dfrom:
        q = q.filter(RepairRequest.created_at >= datetime.strptime(dfrom, "%Y-%m-%d"))
    if dto:
        q = q.filter(RepairRequest.created_at < datetime.strptime(dto, "%Y-%m-%d") + timedelta(days=1))
    if rtype:
        q = q.filter(RepairRequest.request_type == rtype)
    if line:
        q = q.filter(RepairRequest.line == line)
    if customer:
        q = q.filter(RepairRequest.customer == customer)
    if status == "open":
        q = q.filter(RepairRequest.status.in_(OPEN_STATUSES))
    elif status:
        q = q.filter(RepairRequest.status == status)
    if tech:
        q = q.filter(RepairRequest.assignments.any(Assignment.user.has(User.full_name == tech)))
    return q.order_by(RepairRequest.created_at.desc())


@router.get("/reports")
def reports(request: Request, user: User = Depends(perms.require_page("reports")), db: Session = Depends(get_db), dfrom: str = "", dto: str = "",
            rtype: str = "", line: str = "", customer: str = "", status: str = "", tech: str = "", fmt: str = ""):
    company = request.state.company
    today = today_local()
    if not dfrom:
        dfrom = (today - timedelta(days=90)).isoformat()
    if not dto:
        dto = today.isoformat()
    q = perms.scope_requests(_filtered(db, company.id, dfrom, dto, rtype, line, customer, status, tech), user)
    rows = q.all()
    s = kpi.summary(db, company.id, date.fromisoformat(dfrom), date.fromisoformat(dto))
    b = kpi.breakdowns(db, company.id, date.fromisoformat(dfrom), date.fromisoformat(dto))
    kpis = {"จำนวนงาน": s["total"], "ปิดงาน": s["closed"], "MTTR": s["mttr_text"], "MTBF (วัน)": s["mtbf_days"] or "-",
            "PM Achievement": f"{s['pm_achievement']}%" if s["pm_achievement"] is not None else "-",
            "Lead time ถึงซ่อมเสร็จ (ชม.)": s["lead_to_repair_done_h"] or "-", "ต้นทุนอะไหล่": f"{s['parts_cost']:,.0f}"}
    title = f"Die Repair & PM Report {company.code} {_dmy(dfrom)} to {_dmy(dto)}"
    if fmt == "xlsx":
        extra = {"Summary": (["KPI", "Value"], list(kpis.items())), "By Problem": (["Problem", "Qty"], b["by_problem"]),
                 "By Root Cause": (["Root cause", "Qty"], b["by_root_cause"]), "By Customer": (["Customer", "Qty"], b["by_customer"]),
                 "By Die": (["Die", "Qty"], b["by_die"]), "Monthly": (["Month", "Repair", "PM", "Accident", "Closed", "MTTR(min)", "NG"],
                                                                     [[m["month"], m["repair"], m["pm"], m["accident"], m["closed"], m["mttr"], m["ng"]] for m in b["months"]]),
                 "Technicians": (["Technician", "Jobs", "Open", "Net minutes"], [[k, v["jobs"], v["open"], v["net"]] for k, v in b["technicians"]])}
        data = export.to_xlsx(rows, title, extra)
        return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f"attachment; filename=die_report_{company.code}_{_dmy(dfrom)}_{_dmy(dto)}.xlsx"})
    if fmt == "csv":
        return Response(export.to_csv(rows), media_type="text/csv",
                        headers={"Content-Disposition": f"attachment; filename=die_report_{company.code}_{_dmy(dfrom)}_{_dmy(dto)}.csv"})
    if fmt == "pdf":
        return Response(export.to_pdf_requests(rows, title, kpis), media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename=die_report_{company.code}_{_dmy(dfrom)}_{_dmy(dto)}.pdf"})
    lines = sorted({r[0] for r in db.query(RepairRequest.line).filter(RepairRequest.company_id == company.id).distinct() if r[0]})
    customers = sorted({r[0] for r in db.query(RepairRequest.customer).filter(RepairRequest.company_id == company.id).distinct() if r[0]})
    techs = [u.full_name for u in db.query(User).filter(User.company_id == company.id, User.role == "technician").order_by(User.full_name)]
    return render(request, "reports.html", rows=rows[:300], total=len(rows), s=s, b=b, kpis=kpis, dfrom=dfrom, dto=dto, rtype=rtype,
                  line=line, customer=customer, status=status, tech=tech, lines=lines, customers=customers, techs=techs, db=db)


@router.get("/reports/anomaly")
def anomaly(request: Request, user: User = Depends(perms.require_page("anomaly")), db: Session = Depends(get_db)):
    """Supervisor page: data that needs cleaning + early warnings (part/line with abnormal behaviour)."""
    company = request.state.company
    today = today_local()
    since = datetime.combine(today - timedelta(days=90), datetime.min.time())
    rows = perms.scope_requests(db.query(RepairRequest).options(selectinload(RepairRequest.assignments).selectinload(Assignment.user))
                                .filter(RepairRequest.company_id == company.id, RepairRequest.is_deleted == False), user).all()
    recent = [r for r in rows if r.created_at >= since]
    open_rows = [r for r in rows if r.status in OPEN_STATUSES]
    findings = []

    def add(level, title, items, link=None):
        if items:
            findings.append({"level": level, "title": title, "count": len(items), "items": items[:25], "link": link})

    # 1 stuck jobs
    stuck = [r for r in open_rows if r.status == ST["REPAIR_DONE"] and r.tech_completed_at and (now_local() - r.tech_completed_at).days >= 3]
    add("warn", "ซ่อมเสร็จแล้ว แต่ Production ยังไม่ Try-out/รับงาน เกิน 3 วัน", [f"{r.dr_no} • {r.part_no} • {r.line} ({(now_local() - r.tech_completed_at).days} วัน)" for r in stuck], "/requests?status=REPAIR_DONE")
    waitqc = [r for r in open_rows if r.status == ST["WAIT_QC"] and r.tryout_at and (now_local() - r.tryout_at).days >= 2]
    add("warn", "รอ QC ตรวจเกิน 2 วัน", [f"{r.dr_no} • {r.part_no}" for r in waitqc], "/requests?status=WAIT_QC")
    noapprove = [r for r in open_rows if r.status in (ST["WAIT_FOREMAN"], ST["WAIT_LEADER"]) and (now_local() - r.created_at).total_seconds() > 8 * 3600]
    add("danger", "รออนุมัติเกิน 8 ชั่วโมง", [f"{r.dr_no} • {r.status} • {r.part_no}" + (" • ด่วน" if r.is_emergency else "") for r in noapprove], "/requests?view=approve")
    overdue = [r for r in open_rows if r.target_date and r.target_date < today]
    add("danger", "งานเกินกำหนด Target Date", [f"{r.dr_no} • {r.part_no} • กำหนด {r.target_date:%d-%m-%Y} • {r.status}" for r in overdue])
    # 2 data quality
    other = [r for r in recent if (r.problem_text or "").startswith("อื่น") and not (r.other_details or "").strip()]
    add("info", "เลือกปัญหา 'อื่นๆ' โดยไม่ระบุรายละเอียด (90 วัน)", [f"{r.dr_no} • {r.part_no} • {r.requester_name}" for r in other])
    no_cause = [r for r in recent if r.tech_completed_at and not r.root_cause_text]
    add("info", "ซ่อมเสร็จแต่ไม่ระบุสาเหตุ (Root Cause)", [f"{r.dr_no} • {r.technician_names}" for r in no_cause])
    no_time = [r for r in recent if r.tech_completed_at and not r.net_repair_minutes]
    add("info", "ซ่อมเสร็จแต่ไม่มีเวลาซ่อมสุทธิ (ช่างไม่ได้กดเริ่ม/หยุด)", [f"{r.dr_no} • {r.technician_names}" for r in no_time])
    nodie = [r for r in recent if not r.die_code]
    add("info", "ใบงานที่แม่พิมพ์ยังไม่มี Die Code ในทะเบียน", sorted({f"{r.part_no} | {r.die_process}" for r in nodie}), "/dies")
    emerg = [r for r in recent if r.is_emergency]
    if recent and len(emerg) / len(recent) > 0.6:
        add("warn", f"สัดส่วนงานด่วนสูงผิดปกติ {round(100 * len(emerg) / len(recent))}% ของงาน 90 วัน — Priority อาจไม่สะท้อนความจริง",
            [f"{line}: {n}" for line, n in Counter(r.line for r in emerg).most_common(10)])
    # 3 early warning by die / line
    by_die = Counter(f"{r.part_no} | {r.die_process}" for r in recent if r.request_type == "REPAIR")
    repeat = [(k, n) for k, n in by_die.most_common(20) if n >= 3]
    add("warn", "แม่พิมพ์ที่แจ้งซ่อมซ้ำ ≥ 3 ครั้งใน 90 วัน (ควรพิจารณา PM / แก้ที่ต้นเหตุ)", [f"{k} — {n} ครั้ง" for k, n in repeat], "/reports")
    ng_rep = [r for r in rows if (r.ng_count or 0) >= 2]
    add("warn", "ใบงานที่ QC NG ซ้ำ ≥ 2 ครั้ง", [f"{r.dr_no} • {r.part_no} • NG {r.ng_count} ครั้ง" for r in ng_rep])
    same_problem = Counter((r.part_no, r.problem_text) for r in recent)
    rep_prob = [f"{p} — {pr} ({n} ครั้ง)" for (p, pr), n in same_problem.most_common(30) if n >= 3 and pr and not pr.startswith("อื่น")]
    add("info", "Part + ปัญหาเดิมซ้ำ ≥ 3 ครั้ง (90 วัน)", rep_prob)
    line_prev = Counter(r.line for r in rows if since - timedelta(days=90) <= r.created_at < since)
    line_now = Counter(r.line for r in recent)
    spikes = [f"{l}: {line_prev.get(l, 0)} → {n}" for l, n in line_now.items() if l and n >= 10 and n > 1.5 * max(line_prev.get(l, 0), 4)]
    add("warn", "Line ที่จำนวนแจ้งซ่อมเพิ่มขึ้น > 50% เทียบ 90 วันก่อน", spikes)
    # 4 PM
    pm = kpi.pm_due(db, company.id, 7)
    add("danger", "PM เกินกำหนด", [f"{d.part_no} | {d.die_process} • " + (f"กำหนด {d.pm_next_date:%d-%m-%Y}" if d.pm_next_date else (f"stroke {d.stroke_pct}%" if d.pm_interval_strokes else "ยังไม่เคย PM")) for d in pm["overdue"]], "/pm")
    # 5 master spelling variants
    variants = defaultdict(set)
    for r in rows:
        for fld in ("line", "customer", "die_process"):
            v = getattr(r, fld) or ""
            if v:
                variants[(fld, v.strip().lower().replace(" ", ""))].add(v)
    dup = [f"{fld}: " + " / ".join(sorted(vs)) for (fld, _), vs in variants.items() if len(vs) > 1]
    add("info", "ข้อมูล Master ที่สะกดต่างกันแต่ควรเป็นค่าเดียวกัน (ควร Cleaning)", dup, "/admin/masters")
    level_order = {"danger": 0, "warn": 1, "info": 2}
    findings.sort(key=lambda f: level_order[f["level"]])
    return render(request, "anomaly.html", findings=findings, db=db)


def _dmy(iso: str) -> str:
    """'2026-10-16' -> '16-10-2026' (Thai users read day first)."""
    try:
        return datetime.strptime(iso[:10], "%Y-%m-%d").strftime("%d-%m-%Y")
    except Exception:
        return iso
