"""ONE place that says who may open which page and which job tickets.
The sidebar, the page guards, the /roles matrix page and every request query all read from here,
so what users see in the matrix is exactly what the program enforces."""
from fastapi import Depends, HTTPException, Request
from sqlalchemy import or_

from .auth import require_user
from .models import ROLES, RepairRequest, User

ALL = list(ROLES)
SEE_ALL_ROLES = ["foreman", "die_leader", "qc", "manager", "admin", "superadmin", "store"]
# approver roles: with company setting approver_visibility = "own" (default) they see only the tickets of the
# lines they approve (Admin › ผังผู้อนุมัติ) + tickets they requested / acted on.  Someone who approves every line sees all.
APPROVER_ROLES = ["foreman", "die_leader", "qc"]

# key: (Thai label, English label, url, allowed roles)   — superadmin always allowed
PAGES = {
    "home":       ("หน้าหลัก", "Home", "/", ALL),
    "new":        ("แจ้งซ่อม / PM", "New request", "/requests/new", ["production", "foreman", "die_leader", "qc", "manager", "admin"]),
    "requests":   ("รายการงาน / รออนุมัติ", "Requests / approvals", "/requests", ALL),
    "scan":       ("สแกน QR", "Scan QR", "/scan", ALL),
    "pm":         ("แผน PM", "PM plan", "/pm", ["die_leader", "technician", "foreman", "manager", "admin"]),
    "pm_strokes": ("บันทึก Stroke แม่พิมพ์", "Die strokes", "/pm/strokes", ["production", "technician", "die_leader", "foreman", "manager", "admin"]),
    "dies":       ("ทะเบียนแม่พิมพ์", "Die register", "/dies", ALL),
    "dashboard":  ("Dashboard", "Dashboard", "/dashboard", ALL),
    "tv":         ("จอ TV", "TV board", "/tv", ALL),
    "analytics":  ("วิเคราะห์เชิงลึก", "Analytics", "/analytics", ["foreman", "die_leader", "qc", "manager", "admin"]),
    "reports":    ("รายงาน / Export", "Reports / export", "/reports", ["foreman", "die_leader", "qc", "manager", "admin"]),
    "anomaly":    ("ตรวจข้อมูลผิดปกติ", "Data check", "/reports/anomaly", ["foreman", "die_leader", "manager", "admin"]),
    "print_batch": ("พิมพ์ใบงานที่ปิดแล้ว (หลายใบ)", "Batch print closed jobs", "/requests/print", ["die_leader", "manager", "admin", "qc"]),
    "masters":    ("Master data / นำเข้า", "Masters / import", "/admin/masters", ["die_leader", "manager", "admin"]),
    "admin":      ("ผู้ดูแลระบบ (ผู้ใช้, ตั้งค่า, Backup, ล้างข้อมูล)", "Administration", "/admin", ["admin"]),
}

# What each role sees in ticket lists and may open
VISIBILITY = {
    "production": ("เฉพาะใบงานที่ตัวเองเป็นผู้แจ้ง", "Only tickets they requested"),
    "technician": ("เฉพาะใบงานที่ได้รับมอบหมาย", "Only tickets assigned to them"),
}
for _r in SEE_ALL_ROLES:
    VISIBILITY[_r] = ("ทุกใบงานของบริษัท", "All tickets of the company")
for _r in APPROVER_ROLES:
    VISIBILITY[_r] = ("ใบงานของ Line ที่ตนเป็นผู้อนุมัติ (ตามผังผู้อนุมัติ) + ใบงานที่ตนเคยดำเนินการ — ถ้าอนุมัติทุก Line จะเห็นทั้งหมด",
                      "Tickets of the lines they approve + tickets they acted on (all if they approve every line)")


def page_allowed(user: User, key: str) -> bool:
    if user is None:
        return False
    if user.is_superadmin:
        return True
    return user.role in PAGES[key][3]


def require_page(key: str):
    def dep(request: Request, user: User = Depends(require_user)) -> User:
        if not page_allowed(user, key):
            raise HTTPException(403, f"บทบาท '{user.role}' ไม่มีสิทธิ์เข้าหน้านี้ — ดูตารางสิทธิ์ที่เมนู โปรไฟล์ › สิทธิ์การใช้งาน")
        return user
    return dep


def _own_mode(user):
    return (user.role in APPROVER_ROLES and user.company is not None
            and (user.company.settings.get("approver_visibility") or "own") == "own")


def _lines(user, db):
    from . import approvals
    return approvals.responsible_lines(db, user, user.company_id)


def sees_all(user: User, db=None) -> bool:
    if user.is_superadmin or user.role in ("manager", "admin", "store"):
        return True
    if user.role not in SEE_ALL_ROLES:
        return False
    if not _own_mode(user):
        return True
    if db is None:
        from sqlalchemy.orm import object_session
        db = object_session(user)
    return db is not None and _lines(user, db) is None


def _touched(user):
    return or_(RepairRequest.requester_id == user.id, RepairRequest.foreman_id == user.id, RepairRequest.leader_id == user.id,
               RepairRequest.tryout_by_id == user.id, RepairRequest.qc_by_id == user.id, RepairRequest.closed_by_id == user.id)


def scope_requests(query, user: User):
    """Apply ticket visibility to any SQLAlchemy query over RepairRequest."""
    db = query.session
    if sees_all(user, db):
        return query
    if user.role == "technician":
        return query.filter(RepairRequest.assignments.any(user_id=user.id))
    if user.role in APPROVER_ROLES:
        from .approvals import _line_cond
        return query.filter(or_(_line_cond(_lines(user, db)), _touched(user)))
    from .approvals import _line_cond, routing
    t_lines = routing(db, user.company_id).my_lines(user, "TRYOUT") if user.company_id else set()
    if t_lines is None:    # try-out approver for every line
        return query.filter(or_(RepairRequest.requester_id == user.id, RepairRequest.status == "REPAIR_DONE", _touched(user)))
    if t_lines:            # production line leader designated for try-out of some lines
        return query.filter(or_(RepairRequest.requester_id == user.id,
                                (RepairRequest.status == "REPAIR_DONE") & _line_cond(t_lines), _touched(user)))
    return query.filter(RepairRequest.requester_id == user.id)


def can_view(user: User, req: RepairRequest) -> bool:
    from sqlalchemy.orm import object_session
    db = object_session(req)
    if sees_all(user, db):
        return True
    if user.role == "technician":
        return any(a.user_id == user.id for a in req.assignments)
    if user.id in (req.requester_id, req.foreman_id, req.leader_id, req.tryout_by_id, req.qc_by_id, req.closed_by_id):
        return True
    from . import approvals
    rt = approvals.routing(db, req.company_id)
    if user.role in APPROVER_ROLES:
        lines = _lines(user, db)
        return lines is None or approvals._key(req.line) in lines
    return req.status == "REPAIR_DONE" and rt.is_approver(user, "TRYOUT", req.line)
