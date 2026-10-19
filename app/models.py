import json
from datetime import datetime, date

from sqlalchemy import (Boolean, Column, Date, DateTime, Float, ForeignKey, Integer, String, Text,
                        UniqueConstraint, Index)
from sqlalchemy.orm import relationship

from .database import Base, now_local, today_local

# ---------------------------------------------------------------- enumerations
ROLES = ["superadmin", "admin", "manager", "production", "foreman", "die_leader", "technician", "qc", "store"]

REQUEST_TYPES = ["REPAIR", "PM", "ACCIDENT"]
REQUIRED_DAYS = ["<1", "3", "5", ">7"]           # matches the current "Required Day" practice
ATTACH_KINDS = ["PROBLEM", "BEFORE", "AFTER", "TRYOUT", "QC", "OTHER"]

# Status codes (order = flow). Kept as short strings so reports stay readable.
ST = dict(
    DRAFT="DRAFT",
    WAIT_FOREMAN="WAIT_FOREMAN",
    FOREMAN_REJECTED="FOREMAN_REJECTED",
    WAIT_LEADER="WAIT_LEADER",
    LEADER_REJECTED="LEADER_REJECTED",
    ASSIGNED="ASSIGNED",            # leader approved + technician(s) assigned, not started
    IN_PROGRESS="IN_PROGRESS",      # technician received / working
    REPAIR_DONE="REPAIR_DONE",      # technician completed -> waiting production try-out
    RETURNED="RETURNED",            # production returned (try-out failed) -> back to die maintenance
    WAIT_QC="WAIT_QC",              # production accepted -> waiting QC judgement
    NG_RETURN="NG_RETURN",          # QC NG -> back to die maintenance
    CLOSED="CLOSED",
    CANCELLED="CANCELLED",
)
OPEN_STATUSES = [ST["WAIT_FOREMAN"], ST["WAIT_LEADER"], ST["ASSIGNED"], ST["IN_PROGRESS"], ST["REPAIR_DONE"],
                 ST["RETURNED"], ST["WAIT_QC"], ST["NG_RETURN"]]
DIE_SIDE_STATUSES = [ST["ASSIGNED"], ST["IN_PROGRESS"], ST["RETURNED"], ST["NG_RETURN"]]


def _json_get(raw, default):
    try:
        return json.loads(raw) if raw else default
    except Exception:
        return default


# ---------------------------------------------------------------- tenancy
class Company(Base):
    __tablename__ = "companies"
    id = Column(Integer, primary_key=True)
    code = Column(String(16), unique=True, nullable=False)        # AH, AHR, AHD, AL ...
    name = Column(String(120), nullable=False)
    dr_prefix = Column(String(16), default="DR")                  # e.g. AHMD  or  AH-DR
    dr_style = Column(String(16), default="running")               # running: PREFIX00001 | monthly: PREFIX-YYMM-001
    dr_seq = Column(Integer, default=0)
    logo_text = Column(String(60), default="AAPICO")
    settings_json = Column(Text, default="{}")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=now_local)

    users = relationship("User", back_populates="company")

    DEFAULT_SETTINGS = {
        "personal_email": True, "brevo_api_key": "", "brevo_sender_email": "", "brevo_sender_name": "",
        "approval_reminder_hours": 8,
        "approver_visibility": "own",     # own: approvers see only tickets of the lines they approve | all: whole company
        "pm_skip_foreman": False,
        "emergency_required_day": "<1",
        "overdue_hours_after_target": 0,
        "spare_app_url": "",
        "spare_api_key": "",
        "shifts": {
            "DAY": {"start": "08:00", "end": "20:00",
                     "breaks": [["10:00", "10:10"], ["12:00", "13:00"], ["15:00", "15:10"], ["17:00", "17:30"]]},
            "NIGHT": {"start": "20:00", "end": "08:00",
                       "breaks": [["22:00", "22:10"], ["00:00", "01:00"], ["03:00", "03:10"], ["05:00", "05:30"]]},
        },
        "image_archive_after_days": 180,
        "storage_warn_mb": 400,
        # automatic jobs (run by the hourly scheduler)
        "auto_backup": True,              # daily backup of this company's data
        "auto_backup_hour": 2,            # local hour (0-23) after which the daily backup runs
        "backup_keep": 14,                # keep the newest N automatic backups
        "daily_report": False,            # send open-job summary + Excel to channels subscribed to 'daily_report'
        "daily_report_hour": 7,
        "tv_working_hours": "",
        "pm_auto_create_days": 0,         # >0: open PM jobs automatically N days before they are due (daily job)
        "pm_due_stroke_pct": 90,          # a stroke-based PM is "due" from this % of its stroke interval
        "pm_require_checklist": True,     # a PM job cannot be completed until every checklist line is answered           # e.g. "07:30-20:30" -> TV board pauses polling outside these hours
    }

    @property
    def settings(self) -> dict:
        s = dict(self.DEFAULT_SETTINGS)
        s.update(_json_get(self.settings_json, {}))
        return s

    def set_settings(self, data: dict):
        s = self.settings
        s.update(data)
        self.settings_json = json.dumps(s, ensure_ascii=False)


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=True)  # None = superadmin (all companies)
    username = Column(String(64), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    full_name = Column(String(120), nullable=False)
    emp_code = Column(String(32))
    email = Column(String(160))
    phone = Column(String(32))
    role = Column(String(24), default="production")
    position = Column(String(80))
    department = Column(String(80))
    line = Column(String(60))             # default line for production users
    shift = Column(String(16))            # DAY / A / B / C
    skills = Column(String(240))          # free text tags: welding, grinding, progressive
    is_active = Column(Boolean, default=True)
    lang = Column(String(4), default="th")
    theme = Column(String(24), default="light")
    accent = Column(String(12), default="#1f5fbf")
    created_at = Column(DateTime, default=now_local)
    last_login = Column(DateTime)
    last_seen = Column(DateTime)                       # updated while using the app -> "online" dot
    must_change_pw = Column(Boolean, default=False)    # forced password change at next login
    photo_path = Column(String(300))                   # staff photo (TV board, team cards) — storage key

    company = relationship("Company", back_populates="users")

    @property
    def is_online(self):
        from datetime import timedelta
        return bool(self.last_seen and now_local() - self.last_seen < timedelta(minutes=10))

    @property
    def is_superadmin(self):
        return self.role == "superadmin"

    @property
    def is_admin(self):
        return self.role in ("superadmin", "admin")


class ApproverRule(Base):
    """Approval routing of ONE company: step (FOREMAN/LEADER/TRYOUT/QC) + line ('' = all lines) -> person.
    is_backup = acts / is reminded only when the main approver does not respond. See app/approvals.py."""
    __tablename__ = "approver_rules"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    step = Column(String(12), nullable=False)
    line = Column(String(60), default="")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_backup = Column(Boolean, default=False)
    created_at = Column(DateTime, default=now_local)
    user = relationship("User")
    __table_args__ = (Index("ix_approver_company_step", "company_id", "step"),)


# ---------------------------------------------------------------- PM: checklists, strokes, plan changes
class PMTemplate(Base):
    """PM checklist of one company. `match` = words in Die Process it applies to automatically
    (e.g. 'Progressive'); empty = default template. A die can also point to a template explicitly."""
    __tablename__ = "pm_templates"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name = Column(String(120), nullable=False)
    match = Column(String(120), default="")
    is_active = Column(Boolean, default=True)
    items = relationship("PMTemplateItem", cascade="all, delete-orphan", order_by="PMTemplateItem.sort")


class PMTemplateItem(Base):
    __tablename__ = "pm_template_items"
    id = Column(Integer, primary_key=True)
    template_id = Column(Integer, ForeignKey("pm_templates.id"), nullable=False)
    sort = Column(Integer, default=0)
    section = Column(String(60), default="")          # e.g. Upper die / Lower die / Safety
    text = Column(String(200), nullable=False)
    kind = Column(String(8), default="check")          # check = OK/NG/NA | value = measured number
    unit = Column(String(16), default="")
    spec_min = Column(Float)
    spec_max = Column(Float)


class PMCheckResult(Base):
    """One answered checklist line of one PM job (text copied, so editing the template later keeps history)."""
    __tablename__ = "pm_check_results"
    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey("requests.id"), nullable=False)
    item_id = Column(Integer)
    sort = Column(Integer, default=0)
    section = Column(String(60), default="")
    text = Column(String(200))
    kind = Column(String(8), default="check")
    unit = Column(String(16), default="")
    spec_min = Column(Float)
    spec_max = Column(Float)
    result = Column(String(4))                         # OK / NG / NA
    value = Column(Float)
    note = Column(String(200))
    user_name = Column(String(120))
    at = Column(DateTime, default=now_local)


class StrokeLog(Base):
    """Stroke (shot) counts reported by production / counters / Excel; drives stroke-based PM."""
    __tablename__ = "stroke_logs"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    die_id = Column(Integer, ForeignKey("dies.id"), nullable=False)
    at = Column(DateTime, default=now_local)
    work_date = Column(Date)
    strokes = Column(Integer, default=0)               # added this time
    total_after = Column(Integer, default=0)
    source = Column(String(12), default="MANUAL")      # MANUAL / EXCEL / API / SCAN
    user_name = Column(String(120))
    note = Column(String(200))
    die = relationship("Die")
    __table_args__ = (Index("ix_stroke_die", "company_id", "die_id"),)


class PMEvent(Base):
    """Changes to a die's PM plan: postponed, plan set / changed, PM done — shown on the annual plan."""
    __tablename__ = "pm_events"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    die_id = Column(Integer, ForeignKey("dies.id"), nullable=False)
    kind = Column(String(12), nullable=False)          # POSTPONE / PLAN / DONE
    from_date = Column(Date)
    to_date = Column(Date)
    reason = Column(String(240))
    request_id = Column(Integer)
    user_name = Column(String(120))
    at = Column(DateTime, default=now_local)
    __table_args__ = (Index("ix_pmevent_die", "company_id", "die_id"),)


# ---------------------------------------------------------------- masters
class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name = Column(String(80), nullable=False)
    is_active = Column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("company_id", "name"),)


class ProdLine(Base):
    __tablename__ = "lines"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name = Column(String(60), nullable=False)
    is_active = Column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("company_id", "name"),)


class Die(Base):
    """One row = one physical die (Part No + Process). DieCode comes from the die register."""
    __tablename__ = "dies"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    die_code = Column(String(40))
    customer = Column(String(80))
    model = Column(String(80))
    part_no = Column(String(80), nullable=False)
    part_name = Column(String(160))
    die_process = Column(String(120), nullable=False)
    line = Column(String(60))
    machine_no = Column(String(60))
    pm_interval_days = Column(Integer, default=0)       # 0 = no plan
    pm_interval_strokes = Column(Integer, default=0)
    pm_last_date = Column(Date)
    pm_next_date = Column(Date)
    stroke_count = Column(Integer, default=0)            # cumulative shots/strokes of this die
    strokes_at_last_pm = Column(Integer, default=0)      # stroke_count when the last PM was closed
    pm_template_id = Column(Integer, ForeignKey("pm_templates.id"))   # checklist used for this die (None = auto by process)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=now_local)
    __table_args__ = (Index("ix_die_lookup", "company_id", "part_no", "die_process"),)

    @property
    def strokes_since_pm(self):
        return max(0, (self.stroke_count or 0) - (self.strokes_at_last_pm or 0))

    @property
    def stroke_pct(self):
        """% of the stroke-based PM interval already used (None when the die has no stroke plan)."""
        if not self.pm_interval_strokes:
            return None
        return round(100 * self.strokes_since_pm / self.pm_interval_strokes)

    @property
    def label(self):
        return f"{self.part_no} / {self.die_process}"


class Problem(Base):
    """Symptom chosen by Production when requesting (TH/EN)."""
    __tablename__ = "problems"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name_th = Column(String(120), nullable=False)
    name_en = Column(String(120))
    sort = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)


class RootCause(Base):
    """Root cause chosen by Die Maintenance when completing."""
    __tablename__ = "root_causes"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name_th = Column(String(120), nullable=False)
    name_en = Column(String(120))
    sort = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)


# ---------------------------------------------------------------- the job itself
class RepairRequest(Base):
    __tablename__ = "requests"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    dr_no = Column(String(32), nullable=False)
    legacy_id = Column(String(32))                      # old Power Apps AHMDxxxxx
    request_type = Column(String(12), default="REPAIR")
    die_id = Column(Integer, ForeignKey("dies.id"))
    customer = Column(String(80))
    model = Column(String(80))
    part_no = Column(String(80))
    part_name = Column(String(160))
    die_process = Column(String(120))
    die_code = Column(String(40))
    line = Column(String(60))
    machine_no = Column(String(60))
    required_day = Column(String(4), default="3")
    is_emergency = Column(Boolean, default=False)
    target_date = Column(Date)
    stock_oh = Column(Integer)
    sample_part = Column(Boolean, default=False)
    problem_id = Column(Integer, ForeignKey("problems.id"))
    problem_text = Column(String(120))
    other_details = Column(Text)
    department = Column(String(80))
    requester_id = Column(Integer, ForeignKey("users.id"))
    requester_name = Column(String(120))
    requester_phone = Column(String(32))
    status = Column(String(24), default="WAIT_FOREMAN")
    created_at = Column(DateTime, default=now_local)
    # approvals
    foreman_id = Column(Integer, ForeignKey("users.id"))
    foreman_at = Column(DateTime)
    foreman_comment = Column(String(240))
    leader_id = Column(Integer, ForeignKey("users.id"))
    leader_at = Column(DateTime)
    leader_comment = Column(String(240))
    # execution
    tech_received_at = Column(DateTime)
    tech_completed_at = Column(DateTime)
    progress_pct = Column(Integer, default=0)
    root_cause_id = Column(Integer, ForeignKey("root_causes.id"))
    root_cause_text = Column(String(120))
    action_text = Column(Text)
    net_repair_minutes = Column(Integer, default=0)      # break-time deducted, from work logs
    punch_length_after = Column(String(40))              # TF-05.4-5 note: punch length after grinding
    # production try-out
    tryout_count = Column(Integer, default=0)
    tryout_result = Column(String(240))
    tryout_by_id = Column(Integer, ForeignKey("users.id"))
    tryout_at = Column(DateTime)
    # qc
    qc_result = Column(String(4))                         # OK / NG
    qc_by_id = Column(Integer, ForeignKey("users.id"))
    qc_at = Column(DateTime)
    qc_comment = Column(String(240))
    ng_count = Column(Integer, default=0)
    # closing / cost
    closed_at = Column(DateTime)
    closed_by_id = Column(Integer, ForeignKey("users.id"))
    downtime_minutes = Column(Integer, default=0)         # machine downtime (optional, production fills)
    parts_cost = Column(Float, default=0.0)
    pm_plan_note = Column(String(240))
    is_deleted = Column(Boolean, default=False)
    updated_at = Column(DateTime, default=now_local, onupdate=now_local)

    die = relationship("Die")
    requester = relationship("User", foreign_keys=[requester_id])
    foreman = relationship("User", foreign_keys=[foreman_id])
    leader = relationship("User", foreign_keys=[leader_id])
    tryout_by = relationship("User", foreign_keys=[tryout_by_id])
    qc_by = relationship("User", foreign_keys=[qc_by_id])
    problem = relationship("Problem")
    root_cause = relationship("RootCause")
    assignments = relationship("Assignment", back_populates="request", cascade="all, delete-orphan")
    attachments = relationship("Attachment", back_populates="request", cascade="all, delete-orphan")
    history = relationship("StatusHistory", back_populates="request", cascade="all, delete-orphan",
                           order_by="StatusHistory.at")
    progress_logs = relationship("ProgressLog", cascade="all, delete-orphan", order_by="ProgressLog.at")
    work_logs = relationship("WorkLog", cascade="all, delete-orphan", order_by="WorkLog.start_at")
    parts = relationship("PartUsage", cascade="all, delete-orphan")
    pm_checks = relationship("PMCheckResult", cascade="all, delete-orphan", order_by="PMCheckResult.sort")
    __table_args__ = (UniqueConstraint("company_id", "dr_no"), Index("ix_req_status", "company_id", "status"))

    @property
    def technician_names(self):
        return ", ".join(a.user.full_name for a in self.assignments if a.user)

    @property
    def is_open(self):
        return self.status in OPEN_STATUSES

    @property
    def is_overdue(self):
        if not self.target_date or not self.is_open:
            return False
        return today_local() > self.target_date


class Assignment(Base):
    __tablename__ = "assignments"
    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey("requests.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    is_lead = Column(Boolean, default=False)
    assigned_at = Column(DateTime, default=now_local)
    request = relationship("RepairRequest", back_populates="assignments")
    user = relationship("User")


class StatusHistory(Base):
    __tablename__ = "status_history"
    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey("requests.id"), nullable=False)
    from_status = Column(String(24))
    to_status = Column(String(24))
    user_id = Column(Integer, ForeignKey("users.id"))
    user_name = Column(String(120))
    at = Column(DateTime, default=now_local)
    comment = Column(Text)
    request = relationship("RepairRequest", back_populates="history")


class ProgressLog(Base):
    __tablename__ = "progress_logs"
    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey("requests.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"))
    user_name = Column(String(120))
    at = Column(DateTime, default=now_local)
    pct = Column(Integer, default=0)
    note = Column(Text)


class WorkLog(Base):
    """Actual hands-on time. A technician can start / pause / resume; net minutes exclude breaks."""
    __tablename__ = "work_logs"
    id = Column(Integer, primary_key=True)
    request_id = Column(Integer, ForeignKey("requests.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"))
    user_name = Column(String(120))
    start_at = Column(DateTime, nullable=False)
    end_at = Column(DateTime)
    net_minutes = Column(Integer, default=0)


class Attachment(Base):
    __tablename__ = "attachments"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    request_id = Column(Integer, ForeignKey("requests.id"))
    kind = Column(String(12), default="PROBLEM")
    path = Column(String(300), nullable=False)       # storage key (relative) or URL
    thumb_path = Column(String(300))
    size_bytes = Column(Integer, default=0)
    width = Column(Integer)
    height = Column(Integer)
    note = Column(String(200))
    uploaded_by = Column(String(120))
    at = Column(DateTime, default=now_local)
    is_archived = Column(Boolean, default=False)      # original removed, thumbnail kept
    request = relationship("RepairRequest", back_populates="attachments")


class PartUsage(Base):
    __tablename__ = "part_usages"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    request_id = Column(Integer, ForeignKey("requests.id"), nullable=False)
    part_no = Column(String(80), nullable=False)
    part_name = Column(String(160))
    qty = Column(Float, default=1)
    uom = Column(String(12), default="PCS")
    unit_cost = Column(Float, default=0)
    total_cost = Column(Float, default=0)
    source = Column(String(16), default="MANUAL")      # MANUAL / SPARE_APP / SCAN
    ref_no = Column(String(60))                        # issue slip no. from the spare part app
    user_name = Column(String(120))
    at = Column(DateTime, default=now_local)


class NotificationChannel(Base):
    __tablename__ = "notification_channels"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name = Column(String(80), nullable=False)
    type = Column(String(16), nullable=False)          # line / telegram / discord / email / webhook
    config_json = Column(Text, default="{}")
    events_json = Column(Text, default="[]")           # [] = all events
    is_active = Column(Boolean, default=True)

    @property
    def config(self):
        return _json_get(self.config_json, {})

    @property
    def events(self):
        return _json_get(self.events_json, [])


class Notification(Base):
    """In-app bell. user_id=None + role=X means everyone with that role in the company."""
    __tablename__ = "notifications"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"))
    role = Column(String(24))
    title = Column(String(160))
    body = Column(String(400))
    link = Column(String(200))
    level = Column(String(12), default="info")         # info / warn / danger / success
    at = Column(DateTime, default=now_local)
    read_by_json = Column(Text, default="[]")

    @property
    def read_by(self):
        return _json_get(self.read_by_json, [])


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"))
    user_id = Column(Integer, ForeignKey("users.id"))
    user_name = Column(String(120))
    action = Column(String(40))
    entity = Column(String(40))
    entity_id = Column(String(40))
    detail = Column(Text)
    at = Column(DateTime, default=now_local)


class ApiKey(Base):
    """Keys used by the Die Spare Part Inventory program to push issue slips / low stock alerts."""
    __tablename__ = "api_keys"
    id = Column(Integer, primary_key=True)
    company_id = Column(Integer, ForeignKey("companies.id"), nullable=False)
    name = Column(String(80))
    key = Column(String(80), unique=True, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=now_local)
