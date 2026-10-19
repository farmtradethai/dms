"""Offline unit test of WHO gets the personal e-mail at each step (no real e-mail is sent).
Usage: DATA_DIR=... [DATABASE_URL=...] python scripts/test_email_routing.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.database import SessionLocal  # noqa: E402
from app.models import ST, Company, RepairRequest, User  # noqa: E402
from app.services import notify  # noqa: E402

sent = []
notify._send_personal = lambda cfg, mails, subj, head, rows, url, code: sent.append((head, [m[0] for m in mails]))
import threading  # noqa: E402
threading.Thread = type("T", (), {"__init__": lambda self, target=None, args=(), daemon=None: setattr(self, "f", (target, args)),
                                  "start": lambda self: self.f[0](*self.f[1])})
db = SessionLocal()
c = db.query(Company).filter_by(code="AH").first()
c.set_settings({"brevo_api_key": "test", "brevo_sender_email": "die@test.local", "personal_email": True})
db.flush()
fails = []
fm = db.query(User).filter_by(username="ah_foreman").first()
fm.email, fm.line = "foreman.line3@test.local", "Line3"
other_fm = db.query(User).filter(User.company_id == c.id, User.role == "foreman", User.id != fm.id).first()
other_fm.email, other_fm.line = "foreman.line1@test.local", "Line1"
for u in db.query(User).filter(User.company_id == c.id, User.role.in_(["die_leader", "qc", "technician", "production"])):
    u.email = f"{u.username}@test.local"
db.flush()
req = (db.query(RepairRequest).join(User, RepairRequest.requester_id == User.id)
       .filter(RepairRequest.company_id == c.id, RepairRequest.line == "Line3", User.role == "production", User.is_active == True)
       .order_by(RepairRequest.id.desc()).first())


def run(event, status):
    sent.clear()
    req.status = status
    notify.route_to_people(db, c, event, req, "t", "b", f"/requests/{req.id}")
    return sent[0][1] if sent else []


def check(cond, msg):
    print(("  OK  " if cond else "  FAIL") + " " + msg)
    if not cond:
        fails.append(msg)


to = run("submit", ST["WAIT_FOREMAN"])
check(to == ["foreman.line3@test.local"], f"submit -> only the Line3 foreman {to}")
to = run("foreman_approve", ST["WAIT_LEADER"])
check(to and all("leader" in a or "ahdie" in a for a in to), f"foreman_approve -> die leaders {to}")
to = run("complete", ST["REPAIR_DONE"])
check(to == [f"{req.requester.username}@test.local"] if req.requester else to, f"complete -> requester only {to}")
to = run("tryout_accept", ST["WAIT_QC"])
check(to and all("qc" in a for a in to), f"tryout_accept -> QC {to}")
to = run("receive", ST["IN_PROGRESS"])
check(to == [], f"receive -> no e-mail (bell only) {to}")
db.rollback()
print("\nRESULT:", "ALL PASSED" if not fails else f"{len(fails)} FAILED -> {fails}")
sys.exit(1 if fails else 0)
