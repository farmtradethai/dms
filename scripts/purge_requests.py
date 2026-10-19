"""Command-line purge (same logic as Admin > ล้างใบงานเก่า). Always makes a backup first.

Examples (Windows: run inside the project folder, venv active):
  python scripts/purge_requests.py AH --preview                       # just count
  python scripts/purge_requests.py AH --scope legacy --yes            # delete jobs imported from Power Apps
  python scripts/purge_requests.py AH --scope all --types PM --yes    # delete every PM job
  python scripts/purge_requests.py AH --scope all --yes --next-dr 1 --reset-pm
Against the online database:  set DATABASE_URL=postgresql://...  before running."""
import argparse
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import migrate  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import AuditLog, Company  # noqa: E402
from app.services import backup, purge  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("company")
ap.add_argument("--scope", default="legacy", choices=list(purge.SCOPES))
ap.add_argument("--types", nargs="*", default=["REPAIR", "PM", "ACCIDENT"])
ap.add_argument("--status", default="any", choices=list(purge.STATUS_FILTERS))
ap.add_argument("--dfrom", default="")
ap.add_argument("--dto", default="")
ap.add_argument("--preview", action="store_true")
ap.add_argument("--yes", action="store_true", help="really delete")
ap.add_argument("--keep-users", action="store_true", help="do not delete placeholder technician accounts")
ap.add_argument("--orphan-dies", action="store_true")
ap.add_argument("--reset-pm", action="store_true")
ap.add_argument("--next-dr", type=int)
a = ap.parse_args()

migrate.run()
db = SessionLocal()
c = db.query(Company).filter(Company.code == a.company.upper()).first()
if not c:
    sys.exit(f"company {a.company} not found")
pv = purge.preview(db, c.id, a.scope, a.types, a.status, a.dfrom, a.dto)
print(f"[{c.code}] จะลบ {pv['requests']} ใบงาน {pv['by_type']} · รูป {pv['photos']} · เหลือ {pv['remaining']} · ช่วง {pv['first']} → {pv['last']}")
if a.preview or not a.yes:
    print("(preview only — add --yes to delete)")
    sys.exit(0)
name = backup.make_backup(db, c.id, include_images=True, company_code=c.code)
print("backup:", name)
res = purge.execute(db, c, a.scope, a.types, a.status, a.dfrom, a.dto, delete_placeholder_users=not a.keep_users,
                    delete_orphan_dies=a.orphan_dies, reset_pm_dates=a.reset_pm, clear_notifications=True, next_dr=a.next_dr)
db.add(AuditLog(company_id=c.id, user_name="cli", action="purge", entity="request", entity_id=str(res["requests"]),
                detail=f"{vars(a)} backup={name} result={res}"))
db.commit()
print("done:", res)
