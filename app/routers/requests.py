import io
from datetime import date, datetime, timedelta
from typing import List, Optional

import qrcode
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import or_, func
from sqlalchemy.orm import Session, selectinload

from .. import perms, approvals, workflow
from ..auth import require_user
from ..config import settings
from ..database import get_db, now_local, today_local
from ..models import (ST, OPEN_STATUSES, Assignment, Attachment, AuditLog, Company, Customer, Die, PartUsage, Problem, ProdLine,
                      ProgressLog, RepairRequest, RootCause, User)
from ..services import images, notify
from ..services.kpi import technician_board, fmt_minutes
from ..web import render

router = APIRouter()


# ---------------------------------------------------------------- helpers
def next_dr_no(db: Session, company: Company) -> str:
    # row lock on Postgres so two people submitting at the same second never get the same number
    company = db.query(Company).filter(Company.id == company.id).with_for_update().one()
    company.dr_seq = (company.dr_seq or 0) + 1
    if company.dr_style == "monthly":
        ym = now_local().strftime("%y%m")
        # restart numbering each month
        prefix = f"{company.dr_prefix}-{ym}-"
        last = (db.query(RepairRequest.dr_no).filter(RepairRequest.company_id == company.id,
                                                      RepairRequest.dr_no.like(prefix + "%"))
                .order_by(RepairRequest.dr_no.desc()).first())
        n = int(last[0].rsplit("-", 1)[1]) + 1 if last else 1
        return f"{prefix}{n:03d}"
    # skip numbers that already exist (e.g. the admin restarted numbering after a purge)
    while db.query(RepairRequest.id).filter(RepairRequest.company_id == company.id,
                                            RepairRequest.dr_no == f"{company.dr_prefix}{company.dr_seq:05d}").first():
        company.dr_seq += 1
    return f"{company.dr_prefix}{company.dr_seq:05d}"


def get_req(db: Session, request: Request, rid: int) -> RepairRequest:
    req = db.get(RepairRequest, rid)
    if not req or req.is_deleted or req.company_id != request.state.company.id:
        raise HTTPException(404, "Request not found")
    user = getattr(request.state, "user", None)
    if user is not None and not perms.can_view(user, req):
        raise HTTPException(403, "ใบงานนี้ไม่ใช่ของคุณ — ผู้แจ้งเห็นเฉพาะใบงานของตัวเอง, ช่างเห็นเฉพาะงานที่ได้รับมอบหมาย")
    return req


def masters(db: Session, company_id: int) -> dict:
    return {
        "customers": [c.name for c in db.query(Customer).filter_by(company_id=company_id, is_active=True).order_by(Customer.name)],
        "lines": [l.name for l in db.query(ProdLine).filter_by(company_id=company_id, is_active=True).order_by(ProdLine.name)],
        "problems": db.query(Problem).filter_by(company_id=company_id, is_active=True).order_by(Problem.sort, Problem.name_th).all(),
        "root_causes": db.query(RootCause).filter_by(company_id=company_id, is_active=True).order_by(RootCause.sort, RootCause.name_th).all(),
        "technicians": db.query(User).filter_by(company_id=company_id, role="technician", is_active=True).order_by(User.full_name).all(),
    }


DIE_SIDE = ("die_leader", "admin", "superadmin")


def is_worker(user, req) -> bool:
    """Die-side people working on this ticket: assigned technicians, die leaders, admin."""
    return user.is_admin or user.role == "die_leader" or any(a.user_id == user.id for a in req.assignments)


def can_edit_req(user, req) -> bool:
    """Who may correct the data of a ticket (the same rule hides/shows the button and guards the POST)."""
    if user.is_superadmin:
        return True
    if req.status in (ST["CLOSED"], ST["CANCELLED"]):
        return user.is_admin
    if user.role in ("admin", "manager", "die_leader"):
        return True
    if user.role == "foreman":
        return req.requester_id == user.id or approvals.can_act(None, user, "foreman_approve", req) or req.status not in (ST["WAIT_FOREMAN"],)
    if user.role == "production":
        return req.requester_id == user.id and req.status in (ST["DRAFT"], ST["WAIT_FOREMAN"], ST["FOREMAN_REJECTED"], ST["LEADER_REJECTED"])
    return False


def audit(db, request, action, entity, entity_id, detail=""):
    u = request.state.user
    company = getattr(request.state, "company", None)
    db.add(AuditLog(company_id=company.id if company else None, user_id=u.id, user_name=u.full_name, action=action,
                    entity=entity, entity_id=str(entity_id), detail=detail[:2000] if detail else None))


# ---------------------------------------------------------------- list
@router.get("/requests")
def list_requests(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db),
                  view: str = "open", q: str = "", status: str = "", rtype: str = "", line: str = "",
                  customer: str = "", dfrom: str = "", dto: str = "", page: int = 1, flag: str = ""):
    company = request.state.company
    base = perms.scope_requests(db.query(RepairRequest).filter(RepairRequest.company_id == company.id,
                                                               RepairRequest.is_deleted == False), user)
    qry = base.options(selectinload(RepairRequest.assignments).selectinload(Assignment.user))
    if view == "open":
        qry = qry.filter(RepairRequest.status.in_(OPEN_STATUSES))
    elif view == "mine":
        if user.role == "technician":
            qry = qry.filter(RepairRequest.assignments.any(user_id=user.id))
        else:
            qry = qry.filter(RepairRequest.requester_id == user.id)
    elif view == "approve":
        qry = qry.filter(approvals.my_queue(db, user, company.id))
    elif view == "closed":
        qry = qry.filter(RepairRequest.status.in_([ST["CLOSED"], ST["CANCELLED"]]))
    if flag == "emergency":
        qry = qry.filter(RepairRequest.is_emergency == True, RepairRequest.status.in_(OPEN_STATUSES))
    elif flag == "overdue":
        qry = qry.filter(RepairRequest.target_date < today_local(), RepairRequest.status.in_(OPEN_STATUSES))
    elif flag == "today":
        qry = qry.filter(RepairRequest.created_at >= datetime.combine(today_local(), datetime.min.time()))
    elif flag == "ng":
        qry = qry.filter(RepairRequest.ng_count > 0)
    status_counts = dict(_status_counts(qry))      # chips: counts per status for the current view/filters
    if status:
        qry = qry.filter(RepairRequest.status.in_(status.split(",")))
    if rtype:
        qry = qry.filter(RepairRequest.request_type == rtype)
    if line:
        qry = qry.filter(RepairRequest.line == line)
    if customer:
        qry = qry.filter(RepairRequest.customer == customer)
    if dfrom:
        qry = qry.filter(RepairRequest.created_at >= datetime.strptime(dfrom, "%Y-%m-%d"))
    if dto:
        qry = qry.filter(RepairRequest.created_at < datetime.strptime(dto, "%Y-%m-%d") + timedelta(days=1))
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(or_(RepairRequest.dr_no.ilike(like), RepairRequest.legacy_id.ilike(like), RepairRequest.part_no.ilike(like),
                             RepairRequest.part_name.ilike(like), RepairRequest.die_code.ilike(like), RepairRequest.problem_text.ilike(like),
                             RepairRequest.die_process.ilike(like), RepairRequest.requester_name.ilike(like)))
    total = qry.count()
    per = 40
    rows = qry.order_by(RepairRequest.is_emergency.desc(), RepairRequest.created_at.desc()).offset((page - 1) * per).limit(per).all()
    m = masters(db, company.id)
    return render(request, "request_list.html", rows=rows, total=total, page=page, pages=max(1, -(-total // per)),
                  view=view, q=q, status=status, rtype=rtype, line=line, customer=customer, dfrom=dfrom, dto=dto, flag=flag,
                  status_counts=status_counts, lines=m["lines"], customers=m["customers"], db=db)


def tryout_rounds(req: RepairRequest) -> list:
    """T/O Result rows of the paper form, rebuilt from the status history:
    each production try-out (accepted or returned) and the QC judgement that followed it."""
    rounds, cur = [], None
    for h in sorted(req.history, key=lambda x: x.at or datetime.min):
        if h.from_status == ST["REPAIR_DONE"] and h.to_status in (ST["WAIT_QC"], ST["RETURNED"]):
            cur = {"date": h.at, "comment": (h.comment or ("OK" if h.to_status == ST["WAIT_QC"] else "ไม่ผ่าน")),
                   "judge": None if h.to_status == ST["WAIT_QC"] else "T/O NG", "confirmer": h.user_name if h.to_status == ST["RETURNED"] else None,
                   "qc_date": None}
            rounds.append(cur)
        elif h.from_status == ST["WAIT_QC"] and h.to_status in (ST["CLOSED"], ST["NG_RETURN"]):
            if cur is None or cur["judge"]:
                cur = {"date": None, "comment": "", "judge": None, "confirmer": None, "qc_date": None}
                rounds.append(cur)
            cur["judge"] = "OK" if h.to_status == ST["CLOSED"] else "NG"
            cur["confirmer"], cur["qc_date"] = h.user_name, h.at
            if h.comment and h.comment != "นำเข้าจากข้อมูลเก่า":
                cur["comment"] = (cur["comment"] + " · QC: " + h.comment).strip(" ·") if cur["comment"] else h.comment
    if not rounds and (req.tryout_at or req.qc_at):          # imported / minimal data
        rounds.append({"date": req.tryout_at or req.qc_at, "comment": req.qc_comment or req.tryout_result or "",
                       "judge": req.qc_result, "confirmer": req.qc_by.full_name if req.qc_by else None, "qc_date": req.qc_at})
    for r in rounds:
        if r["comment"] == "นำเข้าจากข้อมูลเก่า":
            r["comment"] = req.qc_comment or ""
    return rounds


@router.get("/requests/print")
def print_batch(request: Request, user: User = Depends(perms.require_page("print_batch")), db: Session = Depends(get_db),
                q: str = "", line: str = "", customer: str = "", rtype: str = "", dfrom: str = "", dto: str = "", ids: str = "",
                view: str = "", status: str = "", flag: str = ""):
    """Print CLOSED tickets on the official TF-05.4-5 form (one A4 page each) — max 300 per batch."""
    company = request.state.company
    qry = perms.scope_requests(db.query(RepairRequest), user).filter(RepairRequest.company_id == company.id,
                                                                     RepairRequest.is_deleted == False,
                                                                     RepairRequest.status == ST["CLOSED"])
    if ids:
        qry = qry.filter(RepairRequest.id.in_([int(x) for x in ids.split(",") if x.strip().isdigit()]))
    if line:
        qry = qry.filter(RepairRequest.line == line)
    if customer:
        qry = qry.filter(RepairRequest.customer == customer)
    if rtype:
        qry = qry.filter(RepairRequest.request_type == rtype)
    if dfrom:
        qry = qry.filter(RepairRequest.closed_at >= datetime.strptime(dfrom, "%Y-%m-%d"))
    if dto:
        qry = qry.filter(RepairRequest.closed_at < datetime.strptime(dto, "%Y-%m-%d") + timedelta(days=1))
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(or_(RepairRequest.dr_no.ilike(like), RepairRequest.part_no.ilike(like), RepairRequest.legacy_id.ilike(like)))
    total = qry.count()
    rows = (qry.options(selectinload(RepairRequest.history), selectinload(RepairRequest.parts),
                        selectinload(RepairRequest.assignments).selectinload(Assignment.user))
            .order_by(RepairRequest.dr_no).limit(300).all())
    note = f"พบ {total} ใบ — แสดง 300 ใบแรก กรุณากรองช่วงวันที่ให้แคบลง" if total > 300 else f"ใบงานที่ปิดแล้ว {total} ใบ · ช่วงวันที่ = วันที่ปิดงาน"
    audit(db, request, "print_batch", "request", total, f"{dfrom}-{dto} line={line}")
    db.commit()
    return render(request, "request_print.html", items=[(r, tryout_rounds(r)) for r in rows], back="/requests?view=closed",
                  title=f"Die Correction Action Request — {company.code} ({len(rows)} ใบ)", note=note, db=db)


def _status_counts(qry):
    from sqlalchemy import func as _f
    return (qry.order_by(None).with_entities(RepairRequest.status, _f.count(RepairRequest.id))
            .group_by(RepairRequest.status).all())


# ---------------------------------------------------------------- create
@router.get("/requests/new")
def new_form(request: Request, user: User = Depends(perms.require_page("new")), db: Session = Depends(get_db),
             rtype: str = "REPAIR", die_id: int = 0):
    company = request.state.company
    m = masters(db, company.id)
    die = db.get(Die, die_id) if die_id else None
    if die and die.company_id != company.id:
        die = None
    return render(request, "request_form.html", m=m, rtype=rtype, die=die, req=None, db=db)


@router.get("/api/dies")
def api_dies(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db),
             customer: str = "", model: str = "", part_no: str = "", q: str = ""):
    """Cascading lookups for the request form: customer -> model -> part -> process."""
    cid = request.state.company.id
    base = db.query(Die).filter(Die.company_id == cid, Die.is_active == True)
    if q:
        like = f"%{q}%"
        rows = base.filter(or_(Die.part_no.ilike(like), Die.part_name.ilike(like), Die.die_code.ilike(like))).limit(30).all()
        return {"dies": [_die_json(d) for d in rows]}
    if customer:
        base = base.filter(Die.customer == customer)
    if not model:
        return {"models": sorted({d.model or "" for d in base.all()})}
    base = base.filter(Die.model == model)
    if not part_no:
        parts = {}
        for d in base.all():
            parts.setdefault(d.part_no, d.part_name)
        return {"parts": [{"part_no": k, "part_name": v} for k, v in sorted(parts.items())]}
    rows = base.filter(Die.part_no == part_no).order_by(Die.die_process).all()
    return {"dies": [_die_json(d) for d in rows]}


def _die_json(d: Die):
    return {"id": d.id, "die_code": d.die_code, "customer": d.customer, "model": d.model, "part_no": d.part_no,
            "part_name": d.part_name, "die_process": d.die_process, "line": d.line, "machine_no": d.machine_no}


@router.post("/requests/new")
async def create(request: Request, user: User = Depends(perms.require_page("new")), db: Session = Depends(get_db),
                 request_type: str = Form("REPAIR"), die_id: str = Form(""), customer: str = Form(""), model: str = Form(""),
                 part_no: str = Form(""), part_name: str = Form(""), die_process: str = Form(""), line: str = Form(""),
                 machine_no: str = Form(""), required_day: str = Form("3"), target_date: str = Form(""),
                 stock_oh: str = Form(""), sample_part: str = Form(""), problem_id: str = Form(""), problem_other: str = Form(""),
                 other_details: str = Form(""), requester_phone: str = Form(""), emergency: str = Form(""),
                 photos: List[UploadFile] = File([])):
    company = request.state.company
    die = db.get(Die, int(die_id)) if die_id.strip() else None
    if die and die.company_id != company.id:
        die = None
    if not die and part_no.strip():
        # ad-hoc die (not in register yet) -> create it so history links up
        die = Die(company_id=company.id, part_no=part_no.strip(), part_name=part_name.strip(), die_process=die_process.strip() or "-",
                  customer=customer.strip(), model=model.strip(), line=line.strip())
        db.add(die)
        db.flush()
    if not die:
        raise HTTPException(400, "Please select a die / part")
    problem = db.get(Problem, int(problem_id)) if problem_id.strip() else None
    problem_text = (problem.name_th if problem else "") or problem_other.strip() or "อื่นๆ"
    req = RepairRequest(company_id=company.id, dr_no=next_dr_no(db, company), request_type=request_type, die_id=die.id,
                        die_code=die.die_code, customer=die.customer or customer, model=die.model or model, part_no=die.part_no,
                        part_name=die.part_name or part_name, die_process=die.die_process, line=line.strip() or die.line,
                        machine_no=machine_no.strip() or die.machine_no, required_day=required_day,
                        target_date=datetime.strptime(target_date, "%Y-%m-%d").date() if target_date else None,
                        stock_oh=int(stock_oh) if stock_oh.strip().isdigit() else None, sample_part=bool(sample_part),
                        problem_id=problem.id if problem else None, problem_text=problem_text, other_details=other_details.strip(),
                        department=user.department or "Production", requester_id=user.id, requester_name=user.full_name,
                        requester_phone=requester_phone.strip() or user.phone, status=ST["DRAFT"])
    if problem_other.strip() and problem and problem.name_th.startswith("อื่น"):
        req.other_details = f"[{problem_other.strip()}] {req.other_details}".strip()
    db.add(req)
    db.flush()
    failed = 0
    for up in photos:
        data = await up.read()
        if data:
            try:
                images.save_upload(db, company.code, company.id, req.id, "PROBLEM", data, user.full_name)
            except Exception as e:
                failed += 1
                print("[upload]", e)
    workflow.apply(db, req, "submit", user, company_settings=company.settings, emergency=bool(emergency))
    audit(db, request, "create", "request", req.id, req.dr_no)
    db.commit()
    first = next((a for a in req.attachments), None)
    notify.emit(db, company, "submit", req, image_url=images.external_url(first.path) if first else None, actor_id=user.id)
    db.commit()
    extra = f"&msg=⚠ อัปโหลดรูปไม่สำเร็จ {failed} รูป (ไฟล์เสียหรือไม่ใช่รูปภาพ) — แนบใหม่ได้ที่ส่วนรูปภาพ" if failed else ""
    return RedirectResponse(f"/requests/{req.id}?created=1{extra}", 303)


# ---------------------------------------------------------------- detail
@router.get("/requests/{rid}")
def detail(request: Request, rid: int, user: User = Depends(require_user), db: Session = Depends(get_db), created: int = 0):
    req = get_req(db, request, rid)
    company = request.state.company
    m = masters(db, company.id)
    actions = workflow.available_actions(user, req)
    board = technician_board(db, company.id) if (user.role in DIE_SIDE or "leader_assign" in actions) else []
    my_open_log = next((w for w in req.work_logs if w.end_at is None and w.user_id == user.id), None)
    history_die = (db.query(RepairRequest).filter(RepairRequest.company_id == company.id, RepairRequest.die_id == req.die_id,
                                                  RepairRequest.id != req.id, RepairRequest.is_deleted == False)
                   .order_by(RepairRequest.created_at.desc()).limit(8).all()) if req.die_id else []
    spare_url = company.settings.get("spare_app_url", "")
    can_edit = can_edit_req(user, req)
    waiting_for = []
    step = approvals.STATUS_STEP.get(req.status)
    if step and req.status not in (ST["RETURNED"], ST["NG_RETURN"]):
        if step == "TRYOUT" and approvals.requester_tries_out(req):
            waiting_for = [req.requester.full_name]
        else:
            waiting_for = [u.full_name for u, bk, _ in approvals.routing(db, company.id).approvers(step, req.line, include_backup=False)]
        if len(waiting_for) > 4:
            waiting_for = waiting_for[:4] + [f"+{len(waiting_for) - 4}"]
    elif req.status in (ST["ASSIGNED"], ST["IN_PROGRESS"], ST["RETURNED"], ST["NG_RETURN"]) and req.technician_names:
        waiting_for = [req.technician_names]
    from .pm import can_fill_checklist
    from ..services import pm as pm_service
    if req.request_type == "PM" and not req.pm_checks and req.status in OPEN_STATUSES and req.die_id:
        if pm_service.attach_checklist(db, req):          # PM jobs opened before v1.6 get their checklist now
            db.commit()
    can_parts = (is_worker(user, req) or user.role in ("manager", "store")) and (req.status not in (ST["CLOSED"], ST["CANCELLED"]) or user.is_admin)
    return render(request, "request_detail.html", req=req, waiting_for=waiting_for, can_parts=can_parts,
                  pmc=pm_service.checklist_state(req), can_check=can_fill_checklist(user, req), m=m, board=board, actions=actions, my_open_log=my_open_log,
                  history_die=history_die, spare_url=spare_url, can_edit=can_edit, created=created,
                  photos_by_kind={k: [a for a in req.attachments if a.kind == k] for k in ["PROBLEM", "BEFORE", "AFTER", "TRYOUT", "QC", "OTHER"]},
                  db=db)


@router.post("/requests/{rid}/action/{action}")
async def do_action(request: Request, rid: int, action: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    req = get_req(db, request, rid)
    company = request.state.company
    form = await request.form()
    comment = (form.get("comment") or "").strip()
    kw = dict(company_settings=company.settings)
    if action == "leader_assign":
        kw["tech_ids"] = form.getlist("tech_ids")
    if action == "complete":
        kw.update(root_cause_id=int(form.get("root_cause_id")) if (form.get("root_cause_id") or "").strip() else None,
                  action_text=(form.get("action_text") or "").strip(), punch_length_after=(form.get("punch_length_after") or "").strip())
        rc = db.get(RootCause, kw["root_cause_id"]) if kw["root_cause_id"] else None
        kw["root_cause_text"] = rc.name_th if rc else (form.get("root_cause_other") or "").strip()
    if action == "tryout_accept":
        dm = (form.get("downtime_minutes") or "").strip()
        kw["downtime_minutes"] = int(dm) if dm.isdigit() else None
    try:
        workflow.apply(db, req, action, user, comment, **kw)
    except workflow.WorkflowInputError as e:
        db.rollback()
        return RedirectResponse(f"/requests/{rid}?msg=⚠ {e}", 303)
    except workflow.WorkflowError as e:
        raise HTTPException(400, str(e))
    # photos attached together with the action
    kind = {"complete": "AFTER", "receive": "BEFORE", "tryout_accept": "TRYOUT", "tryout_return": "TRYOUT",
            "qc_ok": "QC", "qc_ng": "QC"}.get(action, "OTHER")
    image_url, failed = None, 0
    for up in form.getlist("photos"):
        if hasattr(up, "read"):
            data = await up.read()
            if data:
                try:
                    att = images.save_upload(db, company.code, company.id, req.id, kind, data, user.full_name)
                    image_url = images.external_url(att.path)
                except Exception as e:
                    failed += 1
                    print("[upload]", e)
    audit(db, request, action, "request", req.id, comment)
    db.commit()
    notify.emit(db, company, action, req, image_url=image_url, actor_id=user.id)
    db.commit()
    extra = f"?msg=⚠ อัปโหลดรูปไม่สำเร็จ {failed} รูป" if failed else ""
    return RedirectResponse(f"/requests/{req.id}{extra}", 303)


@router.post("/requests/{rid}/progress")
def progress(request: Request, rid: int, user: User = Depends(require_user), db: Session = Depends(get_db),
             pct: int = Form(0), note: str = Form("")):
    req = get_req(db, request, rid)
    if req.status != ST["IN_PROGRESS"] or not (user.is_admin or user.role == "die_leader" or any(a.user_id == user.id for a in req.assignments)):
        raise HTTPException(403, "Not allowed")
    req.progress_pct = max(0, min(99, pct))
    db.add(ProgressLog(request_id=req.id, user_id=user.id, user_name=user.full_name, pct=req.progress_pct, note=note.strip()))
    db.commit()
    return RedirectResponse(f"/requests/{req.id}", 303)


@router.post("/requests/{rid}/pause")
def pause(request: Request, rid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    req = get_req(db, request, rid)
    if not is_worker(user, req):
        raise HTTPException(403, "เฉพาะช่างที่ได้รับมอบหมาย / หัวหน้า Die")
    workflow.pause_work(db, req, user, request.state.company.settings)
    db.commit()
    return RedirectResponse(f"/requests/{req.id}", 303)


@router.post("/requests/{rid}/resume")
def resume(request: Request, rid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    req = get_req(db, request, rid)
    if not is_worker(user, req):
        raise HTTPException(403, "เฉพาะช่างที่ได้รับมอบหมาย / หัวหน้า Die")
    if req.status == ST["IN_PROGRESS"]:
        workflow.resume_work(db, req, user)
        db.commit()
    return RedirectResponse(f"/requests/{req.id}", 303)


@router.post("/requests/{rid}/photo")
async def add_photo(request: Request, rid: int, user: User = Depends(require_user), db: Session = Depends(get_db),
                    kind: str = Form("OTHER"), note: str = Form(""), photos: List[UploadFile] = File(...)):
    req = get_req(db, request, rid)
    company = request.state.company
    for up in photos:
        data = await up.read()
        if data:
            try:
                images.save_upload(db, company.code, company.id, req.id, kind if kind in ("PROBLEM", "BEFORE", "AFTER", "TRYOUT", "QC", "OTHER") else "OTHER",
                                   data, user.full_name, note)
            except Exception as e:
                raise HTTPException(400, f"Upload failed: {e}")
    db.commit()
    return RedirectResponse(f"/requests/{req.id}", 303)


def can_delete_photo(user, att) -> bool:
    return user.is_admin or user.role in ("die_leader", "foreman", "manager") or att.uploaded_by == user.full_name


@router.post("/requests/{rid}/photo/{aid}/delete")
def delete_photo(request: Request, rid: int, aid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    req = get_req(db, request, rid)
    att = db.get(Attachment, aid)
    if att and att.request_id == req.id and can_delete_photo(user, att):
        images.delete_attachment(db, att)
        audit(db, request, "delete_photo", "attachment", aid)
        db.commit()
    return RedirectResponse(f"/requests/{req.id}", 303)


@router.post("/requests/{rid}/parts")
def add_part(request: Request, rid: int, user: User = Depends(require_user), db: Session = Depends(get_db),
             part_no: str = Form(...), part_name: str = Form(""), qty: float = Form(1), uom: str = Form("PCS"),
             unit_cost: float = Form(0), ref_no: str = Form(""), source: str = Form("MANUAL")):
    req = get_req(db, request, rid)
    if not (is_worker(user, req) or user.role in ("manager", "store")) or (req.status in (ST["CLOSED"], ST["CANCELLED"]) and not user.is_admin):
        raise HTTPException(403, "บันทึกอะไหล่ได้เฉพาะช่าง / หัวหน้า Die / Store ระหว่างที่งานยังเปิดอยู่")
    if qty <= 0 or unit_cost < 0:
        return RedirectResponse(f"/requests/{req.id}?msg=⚠ จำนวนต้องมากกว่า 0 และราคาต้องไม่ติดลบ#parts", 303)
    p = PartUsage(company_id=req.company_id, request_id=req.id, part_no=part_no.strip(), part_name=part_name.strip(), qty=qty,
                  uom=uom, unit_cost=unit_cost, total_cost=round(qty * unit_cost, 2), ref_no=ref_no.strip(),
                  source=source if source in ("MANUAL", "SCAN", "SPARE_APP") else "MANUAL", user_name=user.full_name)
    db.add(p)
    db.flush()
    req.parts_cost = round(sum(x.total_cost or 0 for x in req.parts), 2)
    db.commit()
    return RedirectResponse(f"/requests/{req.id}#parts", 303)


@router.post("/requests/{rid}/parts/{pid}/delete")
def delete_part(request: Request, rid: int, pid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    req = get_req(db, request, rid)
    if not (is_worker(user, req) or user.role in ("manager", "store")) or (req.status in (ST["CLOSED"], ST["CANCELLED"]) and not user.is_admin):
        raise HTTPException(403, "ลบอะไหล่ได้เฉพาะช่าง / หัวหน้า Die / Store ระหว่างที่งานยังเปิดอยู่")
    p = db.get(PartUsage, pid)
    if p and p.request_id == req.id:
        db.delete(p)
        db.flush()
        req.parts_cost = round(sum(x.total_cost or 0 for x in req.parts if x.id != pid), 2)
        db.commit()
    return RedirectResponse(f"/requests/{req.id}#parts", 303)


# ---------------------------------------------------------------- edit (fix wrong data)
@router.get("/requests/{rid}/edit")
def edit_form(request: Request, rid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    req = get_req(db, request, rid)
    if not can_edit_req(user, req):
        raise HTTPException(403, "คุณไม่มีสิทธิ์แก้ไขใบงานนี้")
    m = masters(db, request.state.company.id)
    return render(request, "request_form.html", m=m, rtype=req.request_type, die=req.die, req=req, db=db)


@router.post("/requests/{rid}/edit")
async def edit_save(request: Request, rid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    req = get_req(db, request, rid)
    if not can_edit_req(user, req):
        raise HTTPException(403, "คุณไม่มีสิทธิ์แก้ไขใบงานนี้ (ใบที่ปิดแล้วแก้ได้เฉพาะ Admin)")
    form = await request.form()
    old_line = req.line
    before = {k: getattr(req, k) for k in ("line", "machine_no", "required_day", "target_date", "stock_oh", "problem_text", "other_details",
                                           "requester_phone", "root_cause_text", "action_text", "downtime_minutes", "customer", "model", "part_name", "die_process")}
    new_line = (form.get("line") or req.line or "").strip()
    if new_line != (req.line or "") and not (user.role in ("admin", "manager", "die_leader", "superadmin")
                                             or req.status in (ST["DRAFT"], ST["WAIT_FOREMAN"], ST["FOREMAN_REJECTED"], ST["LEADER_REJECTED"])):
        new_line = req.line or ""          # after foreman approval the line (= who approves) is fixed, except for leaders/admin
    req.line = new_line
    req.machine_no = (form.get("machine_no") or "").strip()
    req.required_day = form.get("required_day") or req.required_day
    td = (form.get("target_date") or "").strip()
    try:
        req.target_date = datetime.strptime(td, "%Y-%m-%d").date() if td else req.target_date
    except ValueError:
        pass
    so = (form.get("stock_oh") or "").strip()
    req.stock_oh = int(so) if so.isdigit() else req.stock_oh
    req.sample_part = bool(form.get("sample_part"))
    pid = (form.get("problem_id") or "").strip()
    p = db.get(Problem, int(pid)) if pid.isdigit() else None
    if p and p.company_id == req.company_id:
        req.problem_id, req.problem_text = p.id, p.name_th
    elif form.get("problem_other"):
        req.problem_text = form.get("problem_other").strip()
    req.other_details = (form.get("other_details") or "").strip()
    req.requester_phone = (form.get("requester_phone") or "").strip()
    req.customer = (form.get("customer") or req.customer or "").strip()
    req.model = (form.get("model") or req.model or "").strip()
    req.part_name = (form.get("part_name") or req.part_name or "").strip()
    req.die_process = (form.get("die_process") or req.die_process or "").strip()
    rcid = (form.get("root_cause_id") or "").strip()
    rc = db.get(RootCause, int(rcid)) if rcid.isdigit() else None
    if rc and rc.company_id == req.company_id:
        req.root_cause_id, req.root_cause_text = rc.id, rc.name_th
    if form.get("action_text") is not None and user.role in ("technician", "die_leader", "admin", "superadmin"):
        req.action_text = form.get("action_text").strip() or req.action_text
    dm = (form.get("downtime_minutes") or "").strip()
    if dm.isdigit():
        req.downtime_minutes = int(dm)
    nm = (form.get("net_repair_minutes") or "").strip()
    if nm.isdigit() and user.role in ("die_leader", "admin", "superadmin"):
        req.net_repair_minutes = int(nm)
    req.is_emergency = bool(form.get("emergency")) or req.required_day == request.state.company.settings.get("emergency_required_day", "<1")
    changes = {k: [str(v), str(getattr(req, k))] for k, v in before.items() if str(v) != str(getattr(req, k))}
    audit(db, request, "edit", "request", req.id, str(changes))
    db.commit()
    if (req.line or "") != (old_line or "") and req.status == ST["WAIT_FOREMAN"]:
        notify.emit(db, request.state.company, "submit", req, actor_id=user.id)      # route to the new line's approver
        db.commit()
    return RedirectResponse(f"/requests/{req.id}?msg=บันทึกการแก้ไขแล้ว", 303)


# ---------------------------------------------------------------- print / qr
@router.get("/requests/{rid}/print")
def print_view(request: Request, rid: int, user: User = Depends(require_user), db: Session = Depends(get_db)):
    req = get_req(db, request, rid)
    return render(request, "request_print.html", items=[(req, tryout_rounds(req))], title=f"{req.dr_no} — Die Correction Action Request",
                  back=f"/requests/{req.id}", note="", db=db)


@router.get("/requests/{rid}/qr.png")
def qr_png(request: Request, rid: int, user: User = Depends(require_user), db: Session = Depends(get_db), size: int = 6):
    req = get_req(db, request, rid)
    img = qrcode.make(f"{settings.BASE_URL}/r/{req.dr_no}", box_size=max(2, min(size, 12)), border=1)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return Response(buf.getvalue(), media_type="image/png")


@router.get("/r/{dr_no}")
def qr_open(request: Request, dr_no: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    req = db.query(RepairRequest).filter(RepairRequest.company_id == request.state.company.id,
                                         or_(RepairRequest.dr_no == dr_no, RepairRequest.legacy_id == dr_no)).first()
    if not req:
        raise HTTPException(404, "DR not found")
    return RedirectResponse(f"/requests/{req.id}", 302)


@router.get("/scan")
def scan_page(request: Request, user: User = Depends(require_user), db: Session = Depends(get_db), rid: int = 0):
    req = get_req(db, request, rid) if rid else None
    return render(request, "scan.html", req=req, spare_url=request.state.company.settings.get("spare_app_url", ""), db=db)


@router.get("/api/lookup")
def api_lookup(request: Request, code: str, user: User = Depends(require_user), db: Session = Depends(get_db)):
    """Resolve a scanned code: DR no / legacy id -> request ; otherwise ask the spare part app (if configured)."""
    company = request.state.company
    code = code.strip()
    if code.startswith("http"):
        code = code.rstrip("/").rsplit("/", 1)[-1]
    req = perms.scope_requests(db.query(RepairRequest), user).filter(RepairRequest.company_id == company.id,
                                         or_(RepairRequest.dr_no == code, RepairRequest.legacy_id == code)).first()
    if req:
        return {"type": "request", "id": req.id, "dr_no": req.dr_no, "url": f"/requests/{req.id}"}
    die = db.query(Die).filter(Die.company_id == company.id, or_(Die.die_code == code, Die.part_no == code)).first()
    if die:
        return {"type": "die", "die": _die_json(die), "url": f"/dies/{die.id}"}
    spare_url = company.settings.get("spare_app_url")
    if spare_url:
        try:
            import httpx
            r = httpx.get(f"{spare_url.rstrip('/')}/api/parts/{code}", timeout=6,
                          headers={"X-API-Key": company.settings.get("spare_api_key", "")})
            if r.status_code == 200:
                return {"type": "part", "part": r.json()}
        except Exception as e:
            return {"type": "unknown", "error": f"spare app unreachable: {e}"}
    return {"type": "unknown"}
