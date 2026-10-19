"""Admin › 🚀 เริ่มต้นบริษัท (start-up kit + go-live readiness), Admin › 🧭 ผังผู้อนุมัติ, staff photos."""
import io
import secrets
import zipfile
from typing import List

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy.orm import Session

from .. import approvals
from ..auth import require_roles, require_user
from ..config import DATA_DIR
from ..database import get_db
from ..models import ApproverRule, Company, ProdLine, User
from ..services import images, onboarding
from ..web import render
from .admin import _need_company
from .requests import audit

router = APIRouter(prefix="/admin", dependencies=[Depends(_need_company)])
admin_only = require_roles("admin")
TMP = DATA_DIR / "tmp"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# ================================================================== start-up kit + readiness
@router.get("/setup")
def setup_page(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db), msg: str = ""):
    company = request.state.company
    return render(request, "admin/setup.html", r=onboarding.readiness(db, company), report=None, token="", msg=msg, db=db)


@router.get("/setup/kit.xlsx")
def setup_kit(request: Request, data: int = 0, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    company = request.state.company
    blob = onboarding.build_kit(db, company, with_data=bool(data))
    name = f"DMS_starter_kit_{company.code}{'_current' if data else ''}.xlsx"
    return Response(blob, media_type=XLSX, headers={"Content-Disposition": f"attachment; filename={name}"})


@router.post("/setup/upload")
async def setup_upload(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db),
                       file: UploadFile = File(None), token: str = Form(""), do_import: str = Form("")):
    company = request.state.company
    TMP.mkdir(parents=True, exist_ok=True)
    if file is not None and file.filename:
        data = await file.read()
        if not file.filename.lower().endswith(".xlsx"):
            return RedirectResponse("/admin/setup?msg=กรุณาใช้ไฟล์ .xlsx จากปุ่มดาวน์โหลด Template", 303)
        token = f"{company.code}_{secrets.token_hex(8)}"
        (TMP / f"kit_{token}.xlsx").write_bytes(data)
    else:
        if not token or not token.startswith(company.code + "_") or not token.replace("_", "").isalnum():
            raise HTTPException(400, "ไม่พบไฟล์ — กรุณาอัปโหลดใหม่")
        p = TMP / f"kit_{token}.xlsx"
        if not p.exists():
            return RedirectResponse("/admin/setup?msg=ไฟล์หมดอายุ กรุณาอัปโหลดใหม่", 303)
        data = p.read_bytes()
    report = onboarding.process_kit(db, company, data, user, commit=bool(do_import))
    if report.get("imported"):
        (TMP / f"kit_{token}.xlsx").unlink(missing_ok=True)
        approvals.invalidate(db)
        audit(db, request, "setup_kit_import", "company", company.code,
              "; ".join(f"{s['sheet']}: +{s['created']} ~{s['updated']}" for s in report["sheets"] if s["present"]))
        db.commit()
        token = ""
    company = db.get(Company, company.id)
    request.state.company = company
    return render(request, "admin/setup.html", r=onboarding.readiness(db, company), report=report, token=token, msg="", db=db)


# ================================================================== approval routing
def _approver_ctx(db, company):
    approvals.invalidate(db)
    rules = (db.query(ApproverRule).filter(ApproverRule.company_id == company.id)
             .order_by(ApproverRule.step, ApproverRule.line, ApproverRule.is_backup).all())
    rows, warnings = approvals.coverage(db, company.id)
    users = (db.query(User).filter(User.company_id == company.id, User.is_active == True, User.role != "technician")
             .order_by(User.role, User.full_name).all())
    lines = [l.name for l in db.query(ProdLine).filter_by(company_id=company.id, is_active=True).order_by(ProdLine.name)]
    return dict(rules=rules, cov=rows, warnings=warnings, users=users, lines=lines, STEPS=approvals.STEPS)


@router.get("/approvers")
def approvers_page(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db), msg: str = ""):
    company = request.state.company
    return render(request, "admin/approvers.html", msg=msg, vis=company.settings.get("approver_visibility") or "own",
                  **_approver_ctx(db, company), db=db)


@router.post("/approvers")
def approver_add(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db),
                 step: str = Form(...), line: List[str] = Form(default=[]), user_ids: List[str] = Form(default=[]), is_backup: str = Form("")):
    cid = request.state.company.id
    if step not in approvals.STEPS:
        raise HTTPException(400)
    lines = [l.strip() for l in line if l is not None] or [""]
    if "" in lines:
        lines = [""]
    n = 0
    for uid in user_ids:
        u = db.get(User, int(uid))
        if not u or u.company_id != cid:
            continue
        for ln in lines:
            exists = db.query(ApproverRule).filter_by(company_id=cid, step=step, line=ln, user_id=u.id).first()
            if exists:
                exists.is_backup = bool(is_backup)
            else:
                db.add(ApproverRule(company_id=cid, step=step, line=ln, user_id=u.id, is_backup=bool(is_backup)))
                n += 1
    audit(db, request, "approver_add", "approver", step, f"lines={lines} users={user_ids} backup={bool(is_backup)}")
    db.commit()
    return RedirectResponse(f"/admin/approvers?msg=เพิ่มผู้อนุมัติ {n} รายการ", 303)


@router.post("/approvers/{rid}/delete")
def approver_delete(request: Request, rid: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    r = db.get(ApproverRule, rid)
    if not r or r.company_id != request.state.company.id:
        raise HTTPException(404)
    audit(db, request, "approver_delete", "approver", r.step, f"{r.line or 'ทุก Line'} · {r.user.full_name if r.user else r.user_id}")
    db.delete(r)
    db.commit()
    return RedirectResponse("/admin/approvers?msg=ลบแล้ว", 303)


@router.post("/approvers/toggle/{rid}")
def approver_toggle(request: Request, rid: int, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    r = db.get(ApproverRule, rid)
    if not r or r.company_id != request.state.company.id:
        raise HTTPException(404)
    r.is_backup = not r.is_backup
    db.commit()
    return RedirectResponse("/admin/approvers", 303)


@router.post("/approvers/autofill")
def approver_autofill(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db)):
    """Turn today's automatic result (role + Line ประจำ) into explicit rules the admin can then edit."""
    cid = request.state.company.id
    rt = approvals.Routing(db, cid)
    have = {(r.step, (r.line or "").lower(), r.user_id) for r in rt.rules}
    n = 0
    for step, meta in approvals.STEPS.items():
        if step == "TRYOUT" or any(r.step == step for r in rt.rules):
            continue
        people = meta["role"] and [u for u in rt.by_role.get(meta["role"], [])]
        for u in people or []:
            ln = (u.line or "").strip()
            if (step, ln.lower(), u.id) not in have:
                db.add(ApproverRule(company_id=cid, step=step, line=ln, user_id=u.id))
                have.add((step, ln.lower(), u.id))
                n += 1
    audit(db, request, "approver_autofill", "approver", "", f"{n} rules")
    db.commit()
    return RedirectResponse(f"/admin/approvers?msg=สร้างผังจากบทบาท + Line ประจำ {n} รายการ — ตรวจสอบและแก้ไขได้", 303)


@router.post("/approvers/visibility")
def approver_visibility(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db), vis: str = Form("own")):
    c = db.get(Company, request.state.company.id)
    c.set_settings({"approver_visibility": "all" if vis == "all" else "own"})
    audit(db, request, "approver_visibility", "company", c.code, vis)
    db.commit()
    return RedirectResponse("/admin/approvers?msg=บันทึกแล้ว", 303)


# ================================================================== staff photos
def _tv_refresh():
    from .dashboard import _TV_CACHE
    _TV_CACHE.clear()          # the TV board shows the new photo at its next refresh


def _own_or_admin(request, user, target):
    if target is None:
        raise HTTPException(404)
    if user.id == target.id:
        return
    if not user.is_admin or (target.company_id != request.state.company.id and not user.is_superadmin):
        raise HTTPException(403)


@router.post("/users/{uid}/photo")
async def user_photo(request: Request, uid: int, user: User = Depends(require_user), db: Session = Depends(get_db),
                     file: UploadFile = File(...), back: str = Form("/admin/users")):
    target = db.get(User, uid)
    _own_or_admin(request, user, target)
    try:
        images.save_staff_photo(target.company.code if target.company else "SYS", target, await file.read())
    except Exception as e:
        return RedirectResponse(f"{back}?msg=อัปโหลดรูปไม่สำเร็จ: {e}", 303)
    db.commit()
    _tv_refresh()
    return RedirectResponse(f"{back}?msg=บันทึกรูปของ {target.full_name} แล้ว", 303)


@router.post("/users/{uid}/photo/delete")
def user_photo_delete(request: Request, uid: int, user: User = Depends(require_user), db: Session = Depends(get_db),
                      back: str = Form("/admin/users")):
    target = db.get(User, uid)
    _own_or_admin(request, user, target)
    images.remove_staff_photo(target)
    db.commit()
    _tv_refresh()
    return RedirectResponse(f"{back}?msg=ลบรูปแล้ว", 303)


@router.post("/users/photos-zip")
async def photos_zip(request: Request, user: User = Depends(admin_only), db: Session = Depends(get_db), file: UploadFile = File(...)):
    """Many photos at once: a ZIP whose file names are the username or the employee code (somchai.p.jpg / 10001234.png)."""
    company = request.state.company
    users = db.query(User).filter(User.company_id == company.id).all()
    by = {}
    for u in users:
        by[u.username.lower()] = u
        if u.emp_code:
            by[u.emp_code.strip().lower()] = u
    ok, miss = 0, []
    try:
        z = zipfile.ZipFile(io.BytesIO(await file.read()))
    except zipfile.BadZipFile:
        return RedirectResponse("/admin/users?msg=ไฟล์ต้องเป็น .zip", 303)
    for info in z.infolist():
        if info.is_dir() or info.file_size > 15 * 1024 * 1024:
            continue
        base = info.filename.rsplit("/", 1)[-1]
        if base.startswith(".") or "." not in base:
            continue
        stem, ext = base.rsplit(".", 1)
        if ext.lower() not in ("jpg", "jpeg", "png", "webp", "heic", "bmp"):
            continue
        u = by.get(stem.strip().lower())
        if not u:
            miss.append(base)
            continue
        try:
            images.save_staff_photo(company.code, u, z.read(info))
            ok += 1
        except Exception:
            miss.append(base)
    audit(db, request, "photos_zip", "user", file.filename, f"{ok} ok, {len(miss)} not matched")
    db.commit()
    _tv_refresh()
    msg = f"บันทึกรูป {ok} คน" + (f" · ไม่ตรงกับผู้ใช้ {len(miss)} ไฟล์: {', '.join(miss[:8])}" if miss else "")
    return RedirectResponse(f"/admin/users?msg={msg}#photos", 303)
