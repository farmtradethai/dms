"""Approval routing per company: WHO approves each step, for WHICH production line.

Admin › ผังผู้อนุมัติ keeps rules  (step, line, person, backup?).  Every company has its own rules.
Resolution for a ticket (step + line):
    1. rules of that step whose line == ticket line            -> those people
    2. otherwise rules of that step with line blank (= all lines)
    3. otherwise (no rule covers it) the old role-based fallback: users of the step's role whose
       "Line ประจำ" matches, else users of that role without a line, else all users of that role.
The same resolution is used by: who may press the approve buttons, who sees the ticket in
"รอฉันอนุมัติ", who gets the bell / e-mail, the reminders, and the coverage table on the admin page —
so they can never disagree."""
from sqlalchemy import or_
from sqlalchemy.orm import object_session

from .models import ST, ApproverRule, ProdLine, RepairRequest, User

STEPS = {
    "FOREMAN": {"th": "Foreman / Engineer อนุมัติใบแจ้ง", "en": "Foreman / Engineer approval", "role": "foreman",
                "statuses": [ST["WAIT_FOREMAN"]], "actions": ["foreman_approve", "foreman_reject"], "order": 1},
    "LEADER":  {"th": "หัวหน้า Die อนุมัติ + มอบหมายช่าง", "en": "Die leader approval + assignment", "role": "die_leader",
                "statuses": [ST["WAIT_LEADER"], ST["RETURNED"], ST["NG_RETURN"]], "actions": ["leader_assign", "leader_reject"], "order": 2},
    "TRYOUT":  {"th": "รับ Try-out (Line Leader / Production)", "en": "Production try-out", "role": None,
                "statuses": [ST["REPAIR_DONE"]], "actions": ["tryout_accept", "tryout_return"], "order": 3},
    "QC":      {"th": "QC ตัดสิน OK / NG", "en": "QC judgement", "role": "qc",
                "statuses": [ST["WAIT_QC"]], "actions": ["qc_ok", "qc_ng"], "order": 4},
}
ACTION_STEP = {a: k for k, v in STEPS.items() for a in v["actions"]}
STATUS_STEP = {s: k for k, v in STEPS.items() for s in v["statuses"]}
# roles that may act on a step they are not designated for ("อนุมัติแทน", recorded in the history)
OVERRIDE_ROLES = ("admin", "manager")


def _key(line):
    return (line or "").strip().lower()


class Routing:
    """All rules + active users of one company, loaded once per web request."""

    def __init__(self, db, company_id):
        self.company_id = company_id
        self.rules = (db.query(ApproverRule).filter(ApproverRule.company_id == company_id).all())
        self.users = {u.id: u for u in db.query(User).filter(User.company_id == company_id, User.is_active == True)}
        self.by_role = {}
        for u in self.users.values():
            self.by_role.setdefault(u.role, []).append(u)
        lines = {p.name for p in db.query(ProdLine).filter(ProdLine.company_id == company_id, ProdLine.is_active == True)}
        lines |= {r[0] for r in db.query(RepairRequest.line).filter(RepairRequest.company_id == company_id,
                                                                    RepairRequest.status.in_(list(STATUS_STEP))).distinct() if r[0]}
        self.lines = sorted(lines, key=lambda s: s.lower())

    # ---------------------------------------------------------------- resolution
    def _role_fallback(self, step, line):
        role = STEPS[step]["role"]
        if step == "TRYOUT":       # production people of that line, then the line's foreman
            prod = [u for u in self.by_role.get("production", []) if _key(u.line) and _key(u.line) == _key(line)]
            return prod + [u for u, _, _ in self.approvers("FOREMAN", line, include_backup=False)]
        users = self.by_role.get(role, [])
        exact = [u for u in users if _key(u.line) and _key(u.line) == _key(line)]
        if exact:
            return exact
        general = [u for u in users if not _key(u.line)]
        return general or users

    def approvers(self, step, line, include_backup=True):
        """[(user, is_backup, source)] source = 'rule' | 'all-lines rule' | 'auto'."""
        rules = [r for r in self.rules if r.step == step and r.user_id in self.users]
        exact = [r for r in rules if _key(r.line) and _key(r.line) == _key(line)]
        general = [r for r in rules if not _key(r.line)]
        chosen, src = (exact, "rule") if exact else ((general, "all-lines rule") if general else (None, "auto"))
        if chosen is None:
            return [(u, False, "auto") for u in self._role_fallback(step, line)]
        out = [(self.users[r.user_id], bool(r.is_backup), src) for r in sorted(chosen, key=lambda r: (r.is_backup, r.id))]
        return out if include_backup else [x for x in out if not x[1]]

    def is_approver(self, user, step, line):
        return any(u.id == user.id for u, _, _ in self.approvers(step, line))

    def my_lines(self, user, step):
        """Lines (lower-cased keys, '' = tickets without a line) for which this user approves `step`.
        Returns None when he approves every line."""
        keys = [""] + [_key(l) for l in self.lines]
        mine = {k for k in keys if self.is_approver(user, step, k)}
        has_rule_any_line = any(r.step == step and r.user_id == user.id and not _key(r.line) for r in self.rules)
        if mine == set(keys) and (has_rule_any_line or self._auto_all(user, step)):
            return None
        return mine

    def _auto_all(self, user, step):
        """Role fallback with no Line ประจำ -> also covers lines created in the future."""
        return not [r for r in self.rules if r.step == step] and not _key(user.line)


def routing(db, company_id) -> Routing:
    cache = db.info.setdefault("_routing", {})
    if company_id not in cache:
        cache[company_id] = Routing(db, company_id)
    return cache[company_id]


def invalidate(db):
    db.info.pop("_routing", None)


def _line_cond(keys):
    """SQL condition: ticket line is one of the given lower-cased keys."""
    from sqlalchemy import func
    conds = []
    real = [k for k in keys if k]
    if real:
        conds.append(func.lower(func.trim(RepairRequest.line)).in_(real))
    if "" in keys:
        conds.append(or_(RepairRequest.line == None, func.trim(RepairRequest.line) == ""))
    return or_(*conds) if conds else (RepairRequest.id == -1)


def queue_condition(db, user, company_id):
    """SQL condition = tickets currently waiting for THIS person (what 'รอฉันอนุมัติ' shows).
    Returns None for roles that are not approvers (caller uses its own rule)."""
    rt = routing(db, company_id)
    parts = []
    for step, meta in STEPS.items():
        if step == "TRYOUT":
            continue                      # handled below together with the requester
        lines = rt.my_lines(user, step)
        if lines is None:
            parts.append(RepairRequest.status.in_(meta["statuses"]))
        elif lines:
            parts.append(RepairRequest.status.in_(meta["statuses"]) & _line_cond(lines))
    t_lines = rt.my_lines(user, "TRYOUT")
    tryout = RepairRequest.status == ST["REPAIR_DONE"]
    mine = (RepairRequest.requester_id == user.id) & (RepairRequest.request_type != "PM")
    if user.role not in ("production", "foreman"):
        mine = RepairRequest.id == -1
    if t_lines is None:
        parts.append(tryout)
    else:
        parts.append(tryout & (or_(mine, _line_cond(t_lines)) if t_lines else mine))
    return or_(*parts)


def responsible_lines(db, user, company_id):
    """Union of lines over all steps; None = all lines."""
    rt = routing(db, company_id)
    out = set()
    for step in STEPS:
        ls = rt.my_lines(user, step)
        if ls is None:
            if step == "TRYOUT" and user.role == "production":
                continue                  # 'all lines' for try-out alone does not open every ticket to a requester
            return None
        out |= ls
    return out


def can_act(db, user, action, req) -> bool:
    """Designated approver for this ticket's line (or the requester for try-out)."""
    step = ACTION_STEP.get(action)
    if not step:
        return False
    if step == "TRYOUT" and req.requester_id == user.id and requester_tries_out(req):
        return True
    if step in ("FOREMAN", "QC") and req.requester_id == user.id:
        return False            # nobody approves / judges his own request (admin / manager can act on behalf)
    return routing(db or object_session(req), req.company_id).is_approver(user, step, req.line)


def requester_tries_out(req) -> bool:
    """Production / foreman who reported the problem does the try-out. A PM job opened by the die team (or by the
    system) is tried out by the line's try-out approvers instead — the die team must not accept its own work."""
    r = req.requester
    return bool(r and r.is_active and r.role in ("production", "foreman") and req.request_type != "PM")


def coverage(db, company_id):
    """Table for the admin page: every line × step -> people, and warnings."""
    rt = routing(db, company_id)
    rows, warnings = [], []
    for line in rt.lines + [""]:
        cells = {}
        for step in STEPS:
            people = rt.approvers(step, line)
            cells[step] = people
            label = line or "(ไม่ระบุ Line)"
            if not people and step != "TRYOUT":
                warnings.append(f"{label} · {STEPS[step]['th']}: ไม่มีผู้อนุมัติ — งานจะค้าง")
            elif people and step != "TRYOUT" and not any((u.email or "").strip() for u, b, _ in people if not b):
                warnings.append(f"{label} · {STEPS[step]['th']}: ผู้อนุมัติยังไม่มีอีเมล (ได้แค่แจ้งเตือนในระบบ)")
        rows.append((line, cells))
    return rows, warnings


def my_queue(db, user, company_id):
    """'รอฉันดำเนินการ' for any role — the single source used by the list tab, home page and counters."""
    if user.role == "technician":
        return RepairRequest.assignments.any(user_id=user.id) & RepairRequest.status.in_(
            [ST["ASSIGNED"], ST["IN_PROGRESS"], ST["RETURNED"], ST["NG_RETURN"]])
    if user.is_superadmin or user.role in ("admin", "manager", "store"):
        return RepairRequest.status.in_([ST["WAIT_FOREMAN"], ST["WAIT_LEADER"], ST["REPAIR_DONE"], ST["WAIT_QC"]])
    cond = queue_condition(db, user, company_id)
    own_rejected = (RepairRequest.requester_id == user.id) & RepairRequest.status.in_([ST["FOREMAN_REJECTED"], ST["LEADER_REJECTED"]])
    return or_(cond, own_rejected)
