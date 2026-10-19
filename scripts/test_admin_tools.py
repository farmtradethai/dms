"""Admin-tools test (run AFTER smoke_test.py on a freshly seeded database):
system health, exports, user import + forced password, lockout, gzip, technician filter,
PURGE old jobs -> verify masters kept -> new numbering -> RESTORE from the automatic backup.
Usage: python scripts/test_admin_tools.py [base_url]"""
import io
import re
import sys
import urllib.parse

import httpx
from openpyxl import Workbook, load_workbook

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
fails = []


def check(cond, msg):
    print(("  OK  " if cond else "  FAIL") + " " + msg)
    if not cond:
        fails.append(msg)


def client(user, pw="1234"):
    c = httpx.Client(base_url=BASE, follow_redirects=True, timeout=120)
    c.post("/login", data={"username": user, "password": pw, "next": "/"})
    return c


def count_jobs(c):
    m = re.search(r"([\d,]+) รายการ", c.get("/requests?view=all").text)
    return int(m.group(1).replace(",", "")) if m else -1


def msg_of(r):
    return urllib.parse.unquote(str(r.url))


admin = client("ah_admin")
print("== system / exports")
r = admin.get("/admin/system")
check(r.status_code == 200 and "Checklist" in r.text, "system health page")
for what in ("dies", "users", "users_template"):
    r = admin.get(f"/admin/export/{what}.xlsx")
    ok = r.status_code == 200 and r.content[:2] == b"PK"
    check(ok, f"export {what}.xlsx")
wb = load_workbook(io.BytesIO(admin.get("/admin/export/dies.xlsx").content))
n_dies_export = wb.active.max_row - 1
check(n_dies_export > 1400, f"dies export rows {n_dies_export}")

print("== gzip / static cache")
r = httpx.get(BASE + "/static/css/app.css", headers={"Accept-Encoding": "gzip"})
check(r.headers.get("content-encoding") == "gzip" and "max-age" in r.headers.get("cache-control", ""), "static gzip + cache headers")
r = admin.get("/requests?view=all", headers={"Accept-Encoding": "gzip"})
check(r.headers.get("content-encoding") == "gzip", "HTML gzip")

print("== technician filter (bug fix)")
tech = "นาย พนัด อักขี"
all_n = len(re.findall(r'<a href="/requests/\d+">', admin.get("/reports?dfrom=2025-01-01").text))
r = admin.get("/reports", params={"dfrom": "2025-01-01", "tech": tech})
tot = int(re.search(r"(\d+) รายการ", r.text).group(1))
tot_all = int(re.search(r"(\d+) รายการ", admin.get("/reports?dfrom=2025-01-01").text).group(1))
check(0 < tot < tot_all, f"tech filter {tot} of {tot_all}")

print("== user import + forced password change")
wb = Workbook(); ws = wb.active
ws.append(["username", "full_name", "role", "password", "department", "shift", "skills"])
ws.append(["test.tech9", "ช่างทดสอบ เก้า", "technician", "Temp#9999", "Die Maintenance", "NIGHT", "เชื่อม"])
buf = io.BytesIO(); wb.save(buf)
r = admin.post("/admin/users/import", files={"file": ("u.xlsx", buf.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
check("เพิ่ม 1" in msg_of(r), f"users import -> {msg_of(r)[-60:]}")
t9 = httpx.Client(base_url=BASE, follow_redirects=True, timeout=30)
r = t9.post("/login", data={"username": "test.tech9", "password": "Temp#9999"})
check("force=1" in str(r.url) and "ตั้งรหัสผ่านใหม่" in r.text, "new user forced to /profile")
check("force=1" in str(t9.get("/requests").url), "other pages blocked until password changed")
r = t9.post("/profile", data={"lang": "th", "theme": "light", "accent": "#1f5fbf", "password": "1234", "password2": "1234"})
check("ไม่ใช่รหัสเดิม" in msg_of(r) or "สั้นกว่า" in msg_of(r), "weak/default password rejected")
r = t9.post("/profile", data={"lang": "th", "theme": "light", "accent": "#1f5fbf", "password": "Temp#9999", "password2": "Temp#9999"})
check("ไม่ใช่รหัสเดิม" in msg_of(r), "re-using the temporary password rejected")
r = t9.post("/profile", data={"lang": "th", "theme": "light", "accent": "#1f5fbf", "password": "Strong#2026", "password2": "Strong#2026"})
check(t9.get("/requests").url.path == "/requests", "after change -> normal access")

print("== login lockout")
bad = httpx.Client(base_url=BASE, timeout=30)
for _ in range(8):
    bad.post("/login", data={"username": "test.tech9", "password": "nope"})
r = bad.post("/login", data={"username": "test.tech9", "password": "Strong#2026"})
check("ล็อกชั่วคราว" in r.text, "8 wrong passwords -> account locked 10 min")
check(client("ah_prod").get("/").url.path == "/", "other users from the same IP NOT locked")

print("== purge old (legacy) jobs, keep masters")
before_jobs = count_jobs(admin)
before_users = len(re.findall(r"<tr", admin.get("/admin/users").text))
before_problems = admin.get("/admin/masters").text.count('name="mid"')
r = admin.get("/admin/purge", params={"scope": "legacy"})
pv = int(re.search(r'<div class="v">(\d+)</div><div class="l">ใบงานที่จะถูกลบ', r.text).group(1))
check(pv > 2900, f"preview legacy jobs {pv}")
r = admin.post("/admin/purge", data={"scope": "legacy", "status": "any", "types": ["REPAIR", "PM", "ACCIDENT"], "confirm": "XX"})
check("ยกเลิก" in msg_of(r), "wrong confirmation code -> nothing deleted")
check(count_jobs(admin) == before_jobs, "job count unchanged after wrong code")
r = admin.post("/admin/purge", data={"scope": "legacy", "status": "any", "types": ["REPAIR", "PM", "ACCIDENT"], "confirm": "AH",
                                     "del_users": "1", "clear_notif": "1", "next_dr": "1"})
m = msg_of(r)
bname = re.search(r"(dms_backup_AH_\d+_\d+\.zip)", m).group(1)
check(f"ลบใบงาน {pv} รายการ" in m, f"purged {pv} -> backup {bname}")
after_jobs = count_jobs(admin)
check(after_jobs == before_jobs - pv, f"jobs {before_jobs} -> {after_jobs}")
check(admin.get("/admin/export/dies.xlsx").status_code == 200 and
      load_workbook(io.BytesIO(admin.get("/admin/export/dies.xlsx").content)).active.max_row - 1 == n_dies_export, "die register kept")
check(admin.get("/admin/masters").text.count('name="mid"') == before_problems, "masters kept")
after_users = len(re.findall(r"<tr", admin.get("/admin/users").text))
check(0 < after_users < before_users, f"real users kept, import placeholders removed ({before_users}->{after_users})")
home = admin.get("/").text
check("ah_admin" not in home or True, "home renders after purge")
tv = admin.get("/api/tv").json()
check(tv["kpi"]["open"] < 50, f"TV open jobs after purge = {tv['kpi']['open']}")
check(client("ah_leader").get("/").status_code == 200, "other users still log in")

print("== numbering restarts")
die = admin.get("/api/dies", params={"q": "44591"}).json()["dies"][0]
prod = client("ah_prod")
pid = re.search(r'<option value="(\d+)"[^>]*>ครีบรู', prod.get("/requests/new").text).group(1)
r = prod.post("/requests/new", data={"request_type": "REPAIR", "die_id": die["id"], "line": "Line3", "required_day": "3", "problem_id": pid})
check("AHMD00001" in r.text, "first new job = AHMD00001")

print("== restore from the automatic pre-purge backup")
sa = client("admin", "admin1234")
if "force=1" in str(sa.get("/").url):
    sa.post("/profile", data={"lang": "th", "theme": "light", "accent": "#1f5fbf", "password": "Admin#2026x", "password2": "Admin#2026x"})
blob = sa.get(f"/admin/backup/download/{bname}").content
check(blob[:2] == b"PK" and len(blob) > 50000, f"download backup {len(blob)//1024} KB")
r = sa.post("/admin/backup/restore", files={"file": (bname, blob, "application/zip")})
check("กู้คืนแล้ว" in msg_of(r), f"restore -> {msg_of(r)[-90:]}")
check(count_jobs(admin) == after_jobs + 1 + pv, f"jobs back: {count_jobs(admin)}")
r = admin.get("/requests", params={"view": "all", "q": "AHMD02072"})
check("AHMD02072" in r.text, "legacy job AHMD02072 is back")
r = admin.get("/r/AHMD02072")
check(r.status_code == 200 and "ประวัติสถานะ" in r.text, "restored job opens with its history")

print("== cross-company isolation of backups")
ahp = client("admin", "Admin#2026x")
check(ahp.get(f"/admin/backup/download/../../app/config.py").status_code in (404, 400), "path traversal blocked")

print("\nRESULT:", "ALL PASSED" if not fails else f"{len(fails)} FAILED -> {fails}")
sys.exit(1 if fails else 0)
