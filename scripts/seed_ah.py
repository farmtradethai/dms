"""Seed the AH company from the Power Apps CSV exports.
Usage:  python scripts/seed_ah.py [--with-history]
Creates: company AH (+ AHP/AHR shells), masters, die register, named users from the people lists,
and optionally imports DieRepairList.csv (2,900+ historic jobs)."""
import csv
import io
import re
import sys
import time
from pathlib import Path

# Windows consoles (cp874/cp1252) cannot print Thai by default -> force UTF-8 so the script never crashes on a print()
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.auth import hash_password  # noqa: E402
from app.database import Base, SessionLocal, engine  # noqa: E402
from app.models import Company, Customer, Die, Problem, ProdLine, RootCause, User  # noqa: E402
from app.routers.admin import seed_company_masters  # noqa: E402
from app.services import import_data  # noqa: E402

HERE = Path(__file__).resolve().parent / "powerapps_csv"


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def slug(name, prefix):
    base = re.sub(r"[^a-z0-9]+", "", prefix.lower()) or "u"
    return f"{base}{abs(hash(name)) % 100000:05d}"


def log(msg):
    print(f"[seed {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main(with_history=False):
    t0 = time.time()
    Base.metadata.create_all(engine)
    db = SessionLocal()
    from app.config import DATA_DIR
    marker = DATA_DIR / ".seeded"
    log("start — this takes about 1-3 minutes on Windows, please wait")
    companies = {}
    for code, name, prefix in [("AH", "AAPICO Hitech (AH)", "AHMD"), ("AHP", "AAPICO Hitech Parts (AHP)", "AHPMD"),
                               ("AHR", "AAPICO Hitech Rayong (AHR)", "AHRMD")]:
        c = db.query(Company).filter_by(code=code).first()
        if not c:
            c = Company(code=code, name=name, dr_prefix=prefix, dr_style="running", logo_text="AAPICO")
            db.add(c)
            db.flush()
            seed_company_masters(db, c)
            log(f"created company {code}")
        companies[code] = c
    db.commit()

    # lines / customers
    for row in csv.DictReader(open(HERE / "LineList_1.csv", encoding="utf-8-sig")):
        c = companies.get(clean(row["Company"]))
        if c and not db.query(ProdLine).filter_by(company_id=c.id, name=clean(row["LineName"])).first():
            db.add(ProdLine(company_id=c.id, name=clean(row["LineName"])))
    for row in csv.DictReader(open(HERE / "CustomerNameList.csv", encoding="utf-8-sig")):
        for c in companies.values():
            n = clean(row["CustomerName"]).upper() if clean(row["CustomerName"]).lower() != "vinfast" else "Vinfast"
            if not db.query(Customer).filter_by(company_id=c.id, name=n).first():
                db.add(Customer(company_id=c.id, name=n))
    db.commit()

    # die register
    for fn in ("DieProductionProcessList.csv", "DieProductionProcessList2.csv"):
        rows_by_company = {}
        for row in csv.DictReader(open(HERE / fn, encoding="utf-8-sig")):
            rows_by_company.setdefault(clean(row.get("Company")), []).append(row)
        for code, rows in rows_by_company.items():
            c = companies.get(code)
            if not c:
                continue
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
            res = import_data.import_dies(db, c, "dies.csv", buf.getvalue().encode("utf-8"))
            log(f"die register {code}: {res}")

    # people -> users (password = 1234 for the demo, force change later)
    def add_user(company_code, name, role, email="", position="", emp=""):
        c = companies.get(company_code)
        name = clean(name)
        if not c or not name:
            return
        if db.query(User).filter_by(company_id=c.id, full_name=name).first():
            return
        uname = slug(name, company_code + role[:3])
        while db.query(User).filter_by(username=uname).first():
            uname += "x"
        db.add(User(company_id=c.id, username=uname, password_hash=hash_password("1234"), full_name=name, role=role,
                    email=clean(email), position=clean(position), emp_code=clean(emp),
                    department={"technician": "Die Maintenance", "die_leader": "Die Maintenance", "qc": "QC",
                                "foreman": "Production"}.get(role, "Production")))

    for row in csv.DictReader(open(HERE / "DMTechnicianList.csv", encoding="utf-8-sig")):
        if clean(row.get("TechFullName")):
            role = "die_leader" if "LEADER" in (row.get("Position") or "").upper() else "technician"
            add_user(clean(row["Company"]), row["TechFullName"], role, row.get("Email"), row.get("Position"), row.get("EmpCode"))
    for row in csv.DictReader(open(HERE / "ForemanList.csv", encoding="utf-8-sig")):
        if clean(row.get("ForemanFullName")):
            add_user(clean(row["Company"]), row["ForemanFullName"], "foreman", row.get("Email"), row.get("Position"), row.get("EmpCode"))
    for row in csv.DictReader(open(HERE / "QCList.csv", encoding="utf-8-sig")):
        if clean(row.get("QCFullName")):
            add_user(clean(row["Company"]), row["QCFullName"], "qc", row.get("Email"), row.get("Position"), row.get("EmpCode"))
    db.commit()

    # friendly demo accounts for AH (one per role)
    ah = companies["AH"]
    demo = [("ah_admin", "AH Admin", "admin", "IT"), ("ah_manager", "ผู้จัดการ AH", "manager", "Management"),
            ("ah_prod", "AH Press (Production)", "production", "Production"), ("ah_foreman", "Surapin Takengsook", "foreman", "Production"),
            ("ah_leader", "Panat Akshee", "die_leader", "Die Maintenance"), ("ah_tech1", "นาย ณภัทร ปารสูตร", "technician", "Die Maintenance"),
            ("ah_tech2", "นาย สำเนา จันทร", "technician", "Die Maintenance"), ("ah_qc", "Tanakrit Intarasung", "qc", "QC")]
    for uname, name, role, dept in demo:
        existing = db.query(User).filter_by(company_id=ah.id, full_name=name).first()
        if existing:
            existing.username, existing.role, existing.password_hash, existing.is_active = uname, role, hash_password("1234"), True
            continue
        by_uname = db.query(User).filter_by(username=uname).first()
        if by_uname:
            by_uname.password_hash, by_uname.is_active, by_uname.role = hash_password("1234"), True, role
        else:
            db.add(User(company_id=ah.id, username=uname, password_hash=hash_password("1234"), full_name=name, role=role,
                        department=dept, line="Line3" if role == "production" else None, shift="DAY" if role == "technician" else None,
                        skills="เชื่อม, เจียร, Progressive" if role == "technician" else None))
    from app.auth import ensure_default_admin
    ensure_default_admin(db)
    db.commit()
    log(f"users ready: {db.query(User).count()} accounts")

    from app.models import RepairRequest
    if with_history:
        if ah.settings.get("history_purged") and "--force-history" not in sys.argv:
            log("history was purged by an admin — not importing it again (use --force-history to override)")
        elif db.query(RepairRequest).filter_by(company_id=ah.id).count() >= 2900:
            log("history already imported — skipping")
        else:
            data = (HERE / "DieRepairList.csv").read_bytes()
            admin = db.query(User).filter_by(username="ah_admin").first()
            res = import_data.import_requests(db, ah, "DieRepairList.csv", data, admin,
                                              progress=lambda n, total: log(f"history import {n}/{total} ..."))
            log("history import: " + str({k: v for k, v in res.items() if k != 'errors'}) + " " + str(res['errors'][:5]))
        ah = db.get(Company, ah.id)
        # continue numbering after the last legacy id
        last = max((int(r[0][4:]) for r in db.query(__import__("app.models", fromlist=["RepairRequest"]).RepairRequest.legacy_id)
                    .filter_by(company_id=ah.id) if r[0] and r[0].startswith("AHMD") and r[0][4:].isdigit()), default=0)
        ah.dr_seq = max(ah.dr_seq or 0, last)
        db.commit()
        log(f"AH dr_seq set to {ah.dr_seq}")
    db.close()
    marker.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
    log(f"DONE in {time.time() - t0:.0f}s. demo logins (password 1234): " + ", ".join(u for u, *_ in demo) + " | superadmin: admin / admin1234")


if __name__ == "__main__":
    main("--with-history" in sys.argv)
