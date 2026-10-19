"""End-to-end smoke test against a running server: full workflow with every role,
photo upload, parts, API, exports, admin pages. Run: python scripts/smoke_test.py [base_url]"""
import io
import re
import urllib.parse
import sys

import httpx
from PIL import Image

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
fails = []


def client(user, pw="1234"):
    c = httpx.Client(base_url=BASE, follow_redirects=True, timeout=60)
    r = c.post("/login", data={"username": user, "password": pw, "next": "/"})
    assert "logout" in r.text.lower(), f"login failed for {user}"
    return c


def check(cond, msg):
    print(("  OK  " if cond else "  FAIL") + " " + msg)
    if not cond:
        fails.append(msg)


def img_bytes(color):
    b = io.BytesIO()
    Image.new("RGB", (2400, 1800), color).save(b, "JPEG")
    return b.getvalue()


def pages(c, paths):
    for p in paths:
        r = c.get(p)
        check(r.status_code == 200, f"GET {p} -> {r.status_code}")


print("== pages")
admin = client("ah_admin")
pages(admin, ["/", "/requests", "/requests?view=approve", "/requests/new", "/pm", "/dies", "/dashboard", "/tv", "/api/tv", "/reports",
              "/reports/anomaly", "/scan", "/profile", "/notifications", "/admin", "/admin/users", "/admin/masters", "/admin/settings",
              "/admin/channels", "/admin/import", "/admin/storage", "/admin/backup", "/admin/audit", "/admin/import/template.xlsx",
              "/reports?fmt=xlsx", "/reports?fmt=csv", "/reports?fmt=pdf&dfrom=2026-10-01"])

print("== cascading lookups")
d = admin.get("/api/dies", params={"customer": "TOYOTA"}).json()
check(len(d.get("models", [])) > 3, f"models for TOYOTA: {len(d.get('models', []))}")
model = next(m for m in d["models"] if m)
d = admin.get("/api/dies", params={"customer": "TOYOTA", "model": model}).json()
check(len(d.get("parts", [])) >= 1, f"parts for {model}: {len(d.get('parts', []))}")
part = d["parts"][0]["part_no"]
d = admin.get("/api/dies", params={"customer": "TOYOTA", "model": model, "part_no": part}).json()
check(len(d.get("dies", [])) >= 1, f"processes for {part}: {[x['die_process'] for x in d['dies']]}")
die_id = d["dies"][0]["id"]
d = admin.get("/api/dies", params={"q": "44591"}).json()
check(len(d.get("dies", [])) >= 1, "quick search 44591")

print("== workflow")
prod = client("ah_prod")
problem_id = re.search(r'<option value="(\d+)"[^>]*>ครีบรู', prod.get("/requests/new").text).group(1)
r = prod.post("/requests/new", data={"request_type": "REPAIR", "die_id": die_id, "line": "Line3", "required_day": "<1", "stock_oh": "580",
                                     "problem_id": problem_id, "other_details": "smoke test", "requester_phone": "0812345678"},
              files=[("photos", ("a.jpg", img_bytes("red"), "image/jpeg")), ("photos", ("b.jpg", img_bytes("blue"), "image/jpeg"))])
m = re.search(r"/requests/(\d+)", str(r.url))
check(m is not None and re.search(r"AHMD0[5-9]\d{3}|AHMD049[6-9]\d", r.text), f"created request -> {r.url}")
rid = m.group(1)
check("รอ Foreman" in r.text, "status WAIT_FOREMAN")
check(r.text.count("_t.jpg") >= 2, "2 photos compressed + thumbnails")

foreman = client("ah_foreman")
r = foreman.post(f"/requests/{rid}/action/foreman_approve", data={"comment": "ok"})
check("รอหัวหน้า Die" in r.text, "foreman approved -> WAIT_LEADER")
check(prod.post(f"/requests/{rid}/action/foreman_approve", data={}).status_code == 400, "production cannot approve (403/400)")

leader = client("ah_leader")
html = leader.get(f"/requests/{rid}").text
all_ids = re.findall(r'name="tech_ids" value="(\d+)"', html)
check(len(all_ids) >= 2, f"technician board shows {len(all_ids)} techs")
tech_ids = [re.search(r'value="(\d+)"[^>]*>\s*<span>(?:<span[^>]*></span>\s*)?' + n, html).group(1) for n in ("นาย ณภัทร ปารสูตร", "นาย สำเนา จันทร")]
r = leader.post(f"/requests/{rid}/action/leader_assign", data={"tech_ids": [tech_ids[0], tech_ids[1]], "comment": "ด่วน"})
check("มอบหมายช่างแล้ว" in r.text, "leader assigned -> ASSIGNED")

# find which login the assigned tech is
tech = client("ah_tech1")
r = tech.get(f"/requests/{rid}")
if "act_receive" in r.text or "รับงาน" in r.text:
    pass
r = tech.post(f"/requests/{rid}/action/receive", data={}, files=[("photos", ("before.jpg", img_bytes("green"), "image/jpeg"))])
if "กำลังซ่อม" not in r.text:
    tech = client("ah_tech2")
    r = tech.post(f"/requests/{rid}/action/receive", data={}, files=[("photos", ("before.jpg", img_bytes("green"), "image/jpeg"))])
check("กำลังซ่อม" in r.text, "technician received -> IN_PROGRESS (timer started)")
r = tech.post(f"/requests/{rid}/progress", data={"pct": 50, "note": "เชื่อมเสร็จ รอเจียร"})
check("50%" in r.text, "progress 50%")
tech.post(f"/requests/{rid}/pause")
tech.post(f"/requests/{rid}/resume")
tech.post(f"/requests/{rid}/parts", data={"part_no": "SP-000458", "part_name": "Lower Insert", "qty": 2, "unit_cost": 1850, "ref_no": "ISS-01"})
cause_id = re.search(r'name="root_cause_id"[^>]*>.*?<option value="(\d+)"', tech.get(f"/requests/{rid}").text, re.S).group(1)
r = tech.post(f"/requests/{rid}/action/complete", data={"root_cause_id": cause_id, "action_text": "รับพั้นช์ ชิม button 0.4", "punch_length_after": "-2.0"},
              files=[("photos", ("after.jpg", img_bytes("white"), "image/jpeg"))])
check("รอ Try-out" in r.text, "technician complete -> REPAIR_DONE")
check("3,700.00" in r.text, "parts cost summed 3,700")

r = prod.post(f"/requests/{rid}/action/tryout_return", data={"comment": "ยังมีครีบ"})
check("ส่งกลับ" in r.text, "try-out return -> RETURNED")
r = leader.post(f"/requests/{rid}/action/leader_assign", data={"tech_ids": [tech_ids[0], tech_ids[1]]})
r = tech.post(f"/requests/{rid}/action/receive", data={})
r = tech.post(f"/requests/{rid}/action/complete", data={"root_cause_id": cause_id, "action_text": "เจียรเพิ่ม"})
check("รอ Try-out" in r.text, "re-repair loop works")
r = prod.post(f"/requests/{rid}/action/tryout_accept", data={"comment": "OK", "downtime_minutes": "45"})
check("รอ QC" in r.text, "try-out accept -> WAIT_QC")
qc = client("ah_qc")
r = qc.post(f"/requests/{rid}/action/qc_ng", data={"comment": "Burr found"})
check("QC NG" in r.text, "QC NG -> NG_RETURN")
leader.post(f"/requests/{rid}/action/leader_assign", data={"tech_ids": [tech_ids[0], tech_ids[1]]})
tech.post(f"/requests/{rid}/action/receive", data={})
tech.post(f"/requests/{rid}/action/complete", data={"root_cause_id": cause_id, "action_text": "แก้ไขรอบ 3"})
prod.post(f"/requests/{rid}/action/tryout_accept", data={"comment": "OK"})
r = qc.post(f"/requests/{rid}/action/qc_ok", data={"comment": "QC OK"}, files=[("photos", ("qc.jpg", img_bytes("gray"), "image/jpeg"))])
check("ปิดงาน" in r.text and "NG 1" not in r.text, "QC OK -> CLOSED")
check("ประวัติสถานะ" in r.text, "timeline rendered")
r = admin.get(f"/requests/{rid}/print")
check(r.status_code == 200 and "Die Correction Action Request" in r.text and "TF-05.4-5" in r.text and r.text.count("#3 Date") == 1, "official TF-05.4-5 print view")
check(admin.get(f"/requests/{rid}/qr.png").headers["content-type"] == "image/png", "QR png")
check(admin.get("/r/AHMD04996").status_code == 200, "QR deep link by DR no")
check(admin.get("/api/lookup", params={"code": "AHMD02072"}).json()["type"] == "request", "lookup legacy id")

print("== PM")
r = leader.post(f"/dies/{die_id}", data={"die_code": "DIE-TEST", "customer": "TOYOTA", "model": model, "part_name": "x", "die_process": d["dies"][0]["die_process"],
                                         "line": "Line3", "machine_no": "", "pm_interval_days": 30, "pm_last_date": "2026-08-01", "is_active": "1"})
r = leader.get("/pm")
check("เกินกำหนด" in r.text and (f"/pm/create/{die_id}" in r.text or "มีใบงานแล้ว" in r.text), "PM overdue listed")
r = leader.post(f"/pm/create/{die_id}", data={"target_date": "2026-10-20"})
pm_id = re.search(r"/requests/(\d+)", str(r.url)).group(1)   # run on a freshly seeded database
pm_dr = re.search(r"(AHMD\d{5})", r.text).group(1)
check("PM" in r.text and pm_dr > "AHMD04995", f"PM request created {pm_dr}")

print("== notifications / anomaly / edit")
n = foreman.get("/api/notifications/unread").json()
check(n["count"] >= 1, f"foreman unread notifications {n['count']}")
r = admin.post(f"/requests/{pm_id}/edit", data={"line": "Line2", "required_day": ">7", "other_details": "edited", "stock_oh": "10"})
check("Line2" in r.text, "edit request works + audited")
r = admin.get("/reports/anomaly")
check("แม่พิมพ์ที่แจ้งซ่อมซ้ำ" in r.text or "ตรวจข้อมูล" in r.text, "anomaly page")

print("== API for spare part app")
r = admin.post("/admin/apikeys", data={"name": "test"})
key = re.search(r"dms_[A-Za-z0-9_\-]+", r.text).group(0)
api = httpx.Client(base_url=BASE, headers={"X-API-Key": key}, timeout=30)
check(api.get("/api/v1/ping").json()["ok"], "api ping")
r = api.post("/api/v1/spare-issue", json={"dr_no": pm_dr, "part_no": "SP-000123", "part_name": "Guide Post", "qty": 1, "unit_cost": 950, "ref_no": "ISS-77"})
check(r.status_code == 200 and r.json()["parts_cost"] == 950, f"spare-issue linked cost {r.json()}")
r = api.post("/api/v1/low-stock", json={"items": [{"part_no": "PART-125", "part_name": "Spring", "on_hand": 3, "min_stock": 10}]})
check(r.json()["sent"] == 1, "low-stock alert")
check(httpx.get(BASE + "/api/v1/ping").status_code == 401, "api rejects missing key")
check(api.get("/api/v1/requests/" + pm_dr).json()["dr_no"] == pm_dr, "api get request")

print("== backup / storage / theme / i18n")
r = admin.post("/admin/backup", data={"scope": "company"})
name = re.search(r"dms_backup_AH_\d+_\d+\.zip", urllib.parse.unquote(str(r.url))).group(0)
check(admin.get(f"/admin/backup/download/{name}").status_code == 200, "backup zip download")
r = admin.post("/admin/storage/archive", data={"days": 30})
check("Archive" in r.text or "ลบรูป" in r.text, "archive endpoint")
r = prod.post("/profile", data={"lang": "en", "theme": "dark", "accent": "#d13a3a"})
r = prod.get("/")
check('data-theme="dark"' in r.text and "Open jobs" in r.text, "theme dark + English UI")
prod.post("/profile", data={"lang": "th", "theme": "light", "accent": "#1f5fbf"})
check(httpx.get(BASE + "/requests").status_code in (302, 307) or "login" in httpx.get(BASE + "/requests", follow_redirects=True).url.path, "anonymous redirected to login")
r = admin.get("/dashboard?dfrom=2025-03-01&dto=2026-10-16")
check("MTTR" in r.text and "c_month" in r.text, "dashboard charts")
tv = admin.get("/api/tv").json()
check(tv["kpi"]["mttr_h"] is not None and tv["kpi"]["open"] > 0, f"tv kpis mttr={tv['kpi']['mttr_h']} open={tv['kpi']['open']}")
check(tv["team"] and all(k in tv["team"][0] for k in ("name", "photo", "init", "state")), "tv team cards with photo/initials")
check(len(tv["jobs"]) > 14, f"tv lists all open jobs for scrolling ({len(tv['jobs'])})")

print("\nRESULT:", "ALL PASSED" if not fails else f"{len(fails)} FAILED -> {fails}")
sys.exit(1 if fails else 0)
