"""Delete old repair / PM jobs of ONE company while keeping everything else
(companies, users, die register, masters, settings, notification channels, API keys, audit log).

Always called after an automatic backup, so a purge can be undone with Admin > Backup > Restore."""
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..models import (ST, Assignment, Attachment, Company, Die, Notification, PartUsage, ProgressLog, RepairRequest,
                      StatusHistory, User, WorkLog)
from . import images

SCOPES = {
    "legacy": "เฉพาะใบงานที่นำเข้าจาก Power Apps / Template (มีเลข Legacy)",
    "all": "ใบงานทั้งหมดของบริษัทนี้ (ทั้งเก่าและที่สร้างในระบบใหม่)",
    "range": "ตามช่วงวันที่แจ้ง",
}
STATUS_FILTERS = {
    "any": "ทุกสถานะ",
    "closed": "เฉพาะที่ปิดงาน / ยกเลิกแล้ว",
    "open": "เฉพาะที่ยังค้าง (ยังไม่ปิด)",
}


def _query(db: Session, company_id: int, scope: str, types: list, status: str, dfrom: str = "", dto: str = ""):
    q = db.query(RepairRequest.id).filter(RepairRequest.company_id == company_id)
    if scope == "legacy":
        q = q.filter(RepairRequest.legacy_id != None, RepairRequest.legacy_id != "")
    elif scope == "range":
        if dfrom:
            q = q.filter(RepairRequest.created_at >= datetime.strptime(dfrom, "%Y-%m-%d"))
        if dto:
            q = q.filter(RepairRequest.created_at < datetime.strptime(dto, "%Y-%m-%d") + timedelta(days=1))
    if types:
        q = q.filter(RepairRequest.request_type.in_(types))
    if status == "closed":
        q = q.filter(RepairRequest.status.in_([ST["CLOSED"], ST["CANCELLED"]]))
    elif status == "open":
        q = q.filter(~RepairRequest.status.in_([ST["CLOSED"], ST["CANCELLED"]]))
    return q


def preview(db: Session, company_id: int, scope: str, types: list, status: str, dfrom: str = "", dto: str = "") -> dict:
    ids_q = _query(db, company_id, scope, types, status, dfrom, dto)
    sub = ids_q.subquery()
    sel = select(sub.c.id)
    out = {"requests": ids_q.count()}
    by_type = {}
    for (t,) in db.query(RepairRequest.request_type).filter(RepairRequest.id.in_(sel)):
        by_type[t] = by_type.get(t, 0) + 1
    out["by_type"] = by_type
    out["photos"] = db.query(Attachment).filter(Attachment.request_id.in_(sel)).count()
    out["history"] = db.query(StatusHistory).filter(StatusHistory.request_id.in_(sel)).count()
    out["parts"] = db.query(PartUsage).filter(PartUsage.request_id.in_(sel)).count()
    out["remaining"] = db.query(RepairRequest).filter(RepairRequest.company_id == company_id).count() - out["requests"]
    rng = db.query(RepairRequest.created_at).filter(RepairRequest.id.in_(sel)).order_by(RepairRequest.created_at)
    first, last = rng.first(), rng.order_by(None).order_by(RepairRequest.created_at.desc()).first()
    out["first"], out["last"] = (first[0] if first else None), (last[0] if last else None)
    return out


def execute(db: Session, company: Company, scope: str, types: list, status: str, dfrom: str = "", dto: str = "",
            delete_placeholder_users=False, delete_orphan_dies=False, reset_pm_dates=False, clear_notifications=False,
            next_dr: int = None) -> dict:
    ids = [r[0] for r in _query(db, company.id, scope, types, status, dfrom, dto).all()]
    res = {"requests": len(ids), "photos": 0}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for att in db.query(Attachment).filter(Attachment.request_id.in_(chunk)).all():
            images.delete_attachment(db, att)       # removes the files too
            res["photos"] += 1
        from ..models import PMCheckResult
        for model in (Assignment, StatusHistory, ProgressLog, WorkLog, PartUsage, PMCheckResult):
            db.query(model).filter(model.request_id.in_(chunk)).delete(synchronize_session=False)
        db.query(RepairRequest).filter(RepairRequest.id.in_(chunk)).delete(synchronize_session=False)
        db.flush()

    if delete_placeholder_users:
        # inactive accounts created automatically by the history import (password '!') that nothing references any more
        used = set()
        for col in (RepairRequest.requester_id, RepairRequest.foreman_id, RepairRequest.leader_id, RepairRequest.tryout_by_id,
                    RepairRequest.qc_by_id, RepairRequest.closed_by_id):
            used |= {r[0] for r in db.query(col).filter(col != None).distinct()}
        used |= {r[0] for r in db.query(Assignment.user_id).distinct()}
        from ..models import AuditLog
        used |= {r[0] for r in db.query(AuditLog.user_id).filter(AuditLog.user_id != None).distinct()}
        from ..models import ApproverRule
        used |= {r[0] for r in db.query(ApproverRule.user_id).distinct()}
        victims = db.query(User).filter(User.company_id == company.id, User.password_hash == "!", User.is_active == False).all()
        n = 0
        for u in victims:
            if u.id not in used:
                db.delete(u)
                n += 1
        res["placeholder_users"] = n

    if delete_orphan_dies:
        # dies that the import created on the fly (no Die Code in the register) and that no job uses any more
        used_dies = {r[0] for r in db.query(RepairRequest.die_id).filter(RepairRequest.die_id != None).distinct()}
        from ..models import StrokeLog, PMEvent
        used_dies |= {r[0] for r in db.query(StrokeLog.die_id).distinct()} | {r[0] for r in db.query(PMEvent.die_id).distinct()}
        n = 0
        for d in db.query(Die).filter(Die.company_id == company.id, or_(Die.die_code == None, Die.die_code == "")).all():
            if d.id not in used_dies and not d.pm_interval_days:
                db.delete(d)
                n += 1
        res["orphan_dies"] = n

    if reset_pm_dates:
        res["pm_dates_reset"] = (db.query(Die).filter(Die.company_id == company.id)
                                 .update({Die.pm_last_date: None, Die.pm_next_date: None, Die.strokes_at_last_pm: Die.stroke_count},
                                         synchronize_session=False))

    if clear_notifications:
        res["notifications"] = db.query(Notification).filter(Notification.company_id == company.id).delete(synchronize_session=False)

    c = db.get(Company, company.id)
    if scope in ("legacy", "all"):
        # remember the decision so scripts/seed_ah.py --with-history never brings the old jobs back by accident
        c.set_settings({"history_purged": True})
    if next_dr is not None and next_dr >= 1:
        c.dr_seq = next_dr - 1
        res["next_dr"] = next_dr
    db.commit()
    return res
