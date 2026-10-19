"""Status machine. Every change of status goes through `apply()` so that history,
permissions and notifications are always consistent."""
from datetime import date, timedelta

from sqlalchemy.orm import Session, object_session

from .database import now_local, today_local
from .models import ST, Assignment, RepairRequest, StatusHistory, User, WorkLog, OPEN_STATUSES
from .services.kpi import net_minutes_between
from . import approvals


class WorkflowError(Exception):
    pass


class WorkflowInputError(WorkflowError):
    """Something the user must fill in first — shown as a message on the ticket instead of an error page."""


# action -> (allowed roles, allowed from-statuses, to-status)
ACTIONS = {
    "submit":        (["production", "foreman", "die_leader", "admin", "manager"], [ST["DRAFT"], ST["FOREMAN_REJECTED"], ST["LEADER_REJECTED"]], ST["WAIT_FOREMAN"]),
    "foreman_approve": (["foreman", "admin", "manager"], [ST["WAIT_FOREMAN"]], ST["WAIT_LEADER"]),
    "foreman_reject":  (["foreman", "admin", "manager"], [ST["WAIT_FOREMAN"]], ST["FOREMAN_REJECTED"]),
    "leader_assign":   (["die_leader", "admin"], [ST["WAIT_LEADER"], ST["ASSIGNED"], ST["IN_PROGRESS"], ST["RETURNED"], ST["NG_RETURN"]], ST["ASSIGNED"]),
    "leader_reject":   (["die_leader", "admin"], [ST["WAIT_LEADER"]], ST["LEADER_REJECTED"]),
    "receive":         (["technician", "die_leader", "admin"], [ST["ASSIGNED"], ST["RETURNED"], ST["NG_RETURN"]], ST["IN_PROGRESS"]),
    "complete":        (["technician", "die_leader", "admin"], [ST["IN_PROGRESS"]], ST["REPAIR_DONE"]),
    "tryout_accept":   (["production", "foreman", "admin", "manager"], [ST["REPAIR_DONE"]], ST["WAIT_QC"]),
    "tryout_return":   (["production", "foreman", "admin", "manager"], [ST["REPAIR_DONE"]], ST["RETURNED"]),
    "qc_ok":           (["qc", "admin"], [ST["WAIT_QC"]], ST["CLOSED"]),
    "qc_ng":           (["qc", "admin"], [ST["WAIT_QC"]], ST["NG_RETURN"]),
    "cancel":          (["production", "foreman", "die_leader", "admin", "manager"], OPEN_STATUSES + [ST["FOREMAN_REJECTED"], ST["LEADER_REJECTED"]], ST["CANCELLED"]),
    "reopen":          (["admin", "qc", "manager"], [ST["CLOSED"], ST["CANCELLED"]], ST["WAIT_QC"]),
}


def can(user: User, action: str, req: RepairRequest) -> bool:
    roles, froms, _ = ACTIONS[action]
    if req.status not in froms:
        return False
    if user.is_superadmin:
        return True
    if user.company_id != req.company_id:
        return False
    if action in approvals.ACTION_STEP:
        # approval steps: ONLY the people in the company's approval routing for this ticket's line
        # (Admin › ผังผู้อนุมัติ), the requester for try-out, or admin/manager acting on behalf ("อนุมัติแทน")
        if approvals.can_act(None, user, action, req):
            return True
        return user.role in approvals.OVERRIDE_ROLES and user.role in roles
    if user.role not in roles:
        return False
    if action in ("receive", "complete") and user.role == "technician":
        return any(a.user_id == user.id for a in req.assignments)
    if action == "submit" and user.role == "production":
        return req.requester_id == user.id or req.status == ST["DRAFT"]
    if action == "cancel" and user.role in ("production", "foreman") and not user.is_admin:
        # the requester side may withdraw its own ticket only before the die team has started on it
        early = (ST["WAIT_FOREMAN"], ST["WAIT_LEADER"], ST["FOREMAN_REJECTED"], ST["LEADER_REJECTED"])
        if user.role == "production":
            return req.requester_id == user.id and req.status in early
        if req.status not in early:
            return False
        return req.requester_id == user.id or approvals.routing(object_session(req), req.company_id).is_approver(user, "FOREMAN", req.line)
    return True


def available_actions(user: User, req: RepairRequest):
    return [a for a in ACTIONS if can(user, a, req)]


def apply(db: Session, req: RepairRequest, action: str, user: User, comment: str = "", **kw) -> str:
    """Returns the notification event name."""
    if not can(user, action, req):
        raise WorkflowError(f"Action '{action}' not allowed from status {req.status} for role {user.role}")
    _, _, to_status = ACTIONS[action]
    now = now_local()
    company_settings = kw.get("company_settings") or {}
    from_status = req.status

    if action == "submit":
        # PM jobs may skip the production foreman step (configurable per company)
        if req.request_type == "PM" and company_settings.get("pm_skip_foreman"):
            to_status = ST["WAIT_LEADER"]
        if not req.target_date:
            days = {"<1": 1, "3": 3, "5": 5, ">7": 7}.get(req.required_day, 3)
            req.target_date = today_local() + timedelta(days=days)
        req.is_emergency = req.required_day == company_settings.get("emergency_required_day", "<1") or bool(kw.get("emergency"))
    elif action == "foreman_approve":
        req.foreman_id, req.foreman_at, req.foreman_comment = user.id, now, comment
    elif action == "foreman_reject":
        req.foreman_id, req.foreman_at, req.foreman_comment = user.id, now, comment
    elif action == "leader_assign":
        tech_ids = [int(x) for x in kw.get("tech_ids", []) if str(x).strip()]
        if not tech_ids:
            raise WorkflowInputError("กรุณาเลือกช่างอย่างน้อย 1 คน")
        req.leader_id, req.leader_at, req.leader_comment = user.id, now, comment
        # validate: only active technicians of THIS company can be assigned
        valid = {u.id for u in db.query(User).filter(User.id.in_(tech_ids), User.company_id == req.company_id,
                                                     User.is_active == True)}
        tech_ids = [t for t in tech_ids if t in valid]
        if not tech_ids:
            raise WorkflowInputError("กรุณาเลือกช่างที่ยังใช้งานอยู่อย่างน้อย 1 คน")
        # go through the relationship so req.technician_names is correct in the notification sent right after
        existing = {a.user_id for a in req.assignments}
        for a in list(req.assignments):
            if a.user_id not in tech_ids:
                req.assignments.remove(a)
        for i, tid in enumerate(tech_ids):
            if tid not in existing:
                req.assignments.append(Assignment(user_id=tid, is_lead=(i == 0)))
        if from_status in (ST["RETURNED"], ST["NG_RETURN"]):
            to_status = ST["ASSIGNED"]
        if from_status == ST["IN_PROGRESS"]:
            # re-assigned while working (technician sick / moved): stop the running timers; the new team presses "receive"
            _close_open_worklogs(db, req, now, company_settings)
            req.net_repair_minutes = sum(w.net_minutes or 0 for w in req.work_logs)
    elif action == "leader_reject":
        req.leader_id, req.leader_at, req.leader_comment = user.id, now, comment
    elif action == "receive":
        if not req.tech_received_at:
            req.tech_received_at = now
        req.work_logs.append(WorkLog(user_id=user.id, user_name=user.full_name, start_at=now))
        req.progress_pct = max(req.progress_pct or 0, 10)
    elif action == "complete":
        if req.request_type == "PM" and company_settings.get("pm_require_checklist", True) and req.pm_checks:
            left = sum(1 for c in req.pm_checks if not c.result)
            if left:
                raise WorkflowInputError(f"กรุณาบันทึก PM Checklist ให้ครบก่อนกดซ่อมเสร็จ (ยังเหลือ {left} รายการ)")
        _close_open_worklogs(db, req, now, company_settings)
        req.tech_completed_at = now
        req.progress_pct = 100
        req.root_cause_id = kw.get("root_cause_id") or req.root_cause_id
        req.root_cause_text = kw.get("root_cause_text") or req.root_cause_text
        req.action_text = kw.get("action_text") or req.action_text
        req.punch_length_after = kw.get("punch_length_after") or req.punch_length_after
        if not req.action_text:
            raise WorkflowInputError("กรุณาบันทึกวิธีแก้ไข (Action) ก่อนกดซ่อมเสร็จ")
        req.net_repair_minutes = sum(w.net_minutes or 0 for w in req.work_logs)
    elif action == "tryout_accept":
        req.tryout_count = (req.tryout_count or 0) + 1
        req.tryout_result = comment or "OK"
        req.tryout_by_id, req.tryout_at = user.id, now
        if kw.get("downtime_minutes") is not None:
            req.downtime_minutes = kw["downtime_minutes"]
    elif action == "tryout_return":
        req.tryout_count = (req.tryout_count or 0) + 1
        req.tryout_result = comment or "Returned"
        req.tryout_by_id, req.tryout_at = user.id, now
        req.progress_pct = 60
    elif action == "qc_ok":
        req.qc_result, req.qc_by_id, req.qc_at, req.qc_comment = "OK", user.id, now, comment
        req.closed_at, req.closed_by_id = now, user.id
        if req.request_type == "PM" and req.die:
            req.die.pm_last_date = now.date()
            if req.die.pm_interval_days:
                req.die.pm_next_date = now.date() + timedelta(days=req.die.pm_interval_days)
            from .services import pm as pm_service
            pm_service.on_pm_closed(db, req, user)
    elif action == "qc_ng":
        req.qc_result, req.qc_by_id, req.qc_at, req.qc_comment = "NG", user.id, now, comment
        req.ng_count = (req.ng_count or 0) + 1
        req.progress_pct = 60
    elif action == "cancel":
        _close_open_worklogs(db, req, now, company_settings)
        req.closed_at, req.closed_by_id = now, user.id
    elif action == "reopen":
        if from_status == ST["CANCELLED"] and not req.tech_completed_at:
            to_status = ST["WAIT_FOREMAN"]          # cancelled before any repair -> back to the start, not to QC
        req.closed_at, req.closed_by_id, req.qc_result = None, None, None

    if action in approvals.ACTION_STEP and not user.is_superadmin and not approvals.can_act(db, user, action, req):
        comment = f"(อนุมัติแทน · {user.role}) " + (comment or "")
    req.status = to_status
    db.add(StatusHistory(request_id=req.id, from_status=from_status, to_status=to_status, user_id=user.id,
                         user_name=user.full_name, comment=comment))
    db.flush()
    return action


def pause_work(db: Session, req: RepairRequest, user: User, company_settings: dict):
    _close_open_worklogs(db, req, now_local(), company_settings, user_id=user.id)
    req.net_repair_minutes = sum(w.net_minutes or 0 for w in req.work_logs)


def resume_work(db: Session, req: RepairRequest, user: User):
    open_logs = [w for w in req.work_logs if w.end_at is None and w.user_id == user.id]
    if not open_logs:
        req.work_logs.append(WorkLog(user_id=user.id, user_name=user.full_name, start_at=now_local()))


def _close_open_worklogs(db, req, now, company_settings, user_id=None):
    for w in req.work_logs:
        if w.end_at is None and (user_id is None or w.user_id == user_id):
            w.end_at = now
            w.net_minutes = net_minutes_between(w.start_at, now, company_settings.get("shifts") or {})
