"""v1.4 checks: login page, role matrix, ticket visibility, page permissions, per-person notifications,
status chips, clickable KPIs, TV back button, users form, official TF-05.4-5 print (single + batch), analytics.
Usage: python scripts/test_v14.py [base_url]   (run after smoke_test.py on a seeded DB)"""
import re
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
fails = []


def check(cond, msg):
    print(("  OK  " if cond else "  FAIL") + " " + msg)
    if not cond:
        fails.append(msg)


def client(user, pw="1234"):
    c = httpx.Client(base_url=BASE, follow_redirects=True, timeout=180)
    c.post("/login", data={"username": user, "password": pw, "next": "/"})
    return c


def unread(c):
    return c.get("/api/notifications/unread").json()


print("== login page")
r = httpx.get(BASE + "/login")
check("บัญชีทดลอง" not in r.text and "ah_admin" not in r.text, "demo-account link removed from login page")

print("== page permissions (matrix)")
prod, tech1, tech2, fore, lead, qc, adm = (client(u) for u in ("ah_prod", "ah_tech1", "ah_tech2", "ah_foreman", "ah_leader", "ah_qc", "ah_admin"))
check(prod.get("/roles").status_code == 200 and "ตารางสิทธิ์" in prod.get("/roles").text, "/roles matrix visible to everyone")
for path, who, code in [("/reports", prod, 403), ("/analytics", prod, 403), ("/pm", prod, 403), ("/admin", lead, 403),
                        ("/analytics", lead, 200), ("/reports", fore, 200), ("/requests/print", prod, 403), ("/requests/print?dfrom=2026-10-01", lead, 200)]:
    check(who.get(path).status_code == code, f"{path} -> {code}")
html = prod.get("/").text
check('href="/reports"' not in html and 'href="/analytics"' not in html, "production sidebar hides pages it cannot open")

print("== ticket visibility")
pid = re.search(r'<option value="(\d+)"[^>]*>ครีบรู', prod.get("/requests/new").text).group(1)
d = prod.get("/api/dies", params={"q": "44591"}).json()["dies"][0]
r = prod.post("/requests/new", data={"request_type": "REPAIR", "die_id": d["id"], "line": "Line3", "required_day": "3", "problem_id": pid})
rid = re.search(r"/requests/(\d+)", str(r.url)).group(1)
dr = re.search(r"(AHMD\d{5})", r.text).group(1)
check(r.status_code == 200, f"production created {dr}")
other = adm.get("/r/AHMD02087")
oid = re.search(r"/requests/(\d+)", str(other.url)).group(1)
check(prod.get(f"/requests/{oid}").status_code == 403, "production CANNOT open someone else's ticket (also via e-mail link)")
check(prod.get(f"/requests/{rid}").status_code == 200, "production can open own ticket")
lst = prod.get("/requests?view=all").text
check("แสดงเฉพาะใบงานที่คุณเป็นผู้แจ้ง" in lst and "AHMD02087" not in lst, "production list shows only own tickets")
check(tech2.get(f"/requests/{rid}").status_code == 403, "technician cannot open an unassigned ticket")
check(fore.get(f"/requests/{rid}").status_code == 200, "foreman can open (approver)")

print("== per-person notifications follow the workflow")
check(any(dr in i["title"] for i in unread(fore)["items"]), f"foreman notified to approve {dr}")
check(not any(dr in i["title"] for i in unread(tech2)["items"]), "technicians NOT notified at submit")
fore.post(f"/requests/{rid}/action/foreman_approve", data={"comment": "ok"})
check(any(dr in i["title"] for i in unread(lead)["items"]), "die leader notified after foreman approval")
h = lead.get(f"/requests/{rid}").text
t1 = re.search(r'value="(\d+)"[^>]*>\s*<span>(?:<span[^>]*></span>\s*)?นาย ณภัทร ปารสูตร', h).group(1)
lead.post(f"/requests/{rid}/action/leader_assign", data={"tech_ids": [t1]})
check(any(dr in i["title"] for i in unread(tech1)["items"]), "assigned technician notified")
check(not any(dr in i["title"] and "มอบหมาย" in i["title"] for i in unread(tech2)["items"]), "non-assigned technician not notified")
check(tech1.get(f"/requests/{rid}").status_code == 200, "assigned technician can open ticket")
tech1.post(f"/requests/{rid}/action/receive", data={})
cid = re.search(r'name="root_cause_id"[^>]*>.*?<option value="(\d+)"', tech1.get(f"/requests/{rid}").text, re.S).group(1)
tech1.post(f"/requests/{rid}/action/complete", data={"root_cause_id": cid, "action_text": "ชิม button"})
check(any(dr in i["title"] and "Try-out" in i["title"] for i in unread(prod)["items"]), "requester notified to try-out")
prod.post(f"/requests/{rid}/action/tryout_accept", data={"comment": "OK"})
check(any(dr in i["title"] for i in unread(qc)["items"]), "QC notified after try-out")
qc.post(f"/requests/{rid}/action/qc_ok", data={"comment": "QC OK"})
check(any(dr in i["title"] and "ปิดงาน" in i["title"] for i in unread(prod)["items"]), "requester notified when closed")

print("== status chips / clickable KPIs / UI")
r = adm.get("/requests?view=open")
check('class="chip' in r.text and "กรองสถานะ" in r.text, "status filter chips on job list")
r = adm.get("/requests?view=open&flag=overdue")
check(r.status_code == 200 and "chip red on" in r.text, "overdue chip filter works")
r = adm.get("/requests?view=all&status=CLOSED")
check("พิมพ์ใบที่ปิดแล้ว" in r.text, "batch print button on closed list")
home = adm.get("/").text
check(home.count('class="kpi') >= 6 and 'href="/requests?view=open&amp;flag=emergency"' in home or 'href="/requests?view=open&flag=emergency"' in home, "home KPI boxes are links")
check('href="/analytics' in adm.get("/dashboard").text, "dashboard KPIs link to analytics")
check("toggleFS" in adm.get("/tv").text and "กลับ" in adm.get("/tv").text, "TV board has back / home / fullscreen buttons")
check('class="uform"' in adm.get("/admin/users").text, "users form uses aligned grid")
check("อีเมลถึงผู้อนุมัติรายบุคคล" in adm.get("/admin/channels").text, "personal e-mail settings in Admin › channels")

print("== official form print")
r = adm.get(f"/requests/{rid}/print")
check(r.status_code == 200 and "TF-05.4-5" in r.text and "Die Correction Action Request" in r.text and "Issuer by PD" in r.text, "single print = TF-05.4-5 form")
r = lead.get("/requests/print?dfrom=2025-03-10&dto=2025-03-16")
check(r.text.count('class="sheet"') >= 5, f"batch print closed jobs ({r.text.count('class=\"sheet\"')} sheets)")

print("== analytics")
r = lead.get("/analytics?dfrom=2025-02-01")
check(r.status_code == 200 and "ข้อค้นพบอัตโนมัติ" in r.text and "c_pp" in r.text, "analytics page with insights + Pareto")
r = lead.get("/analytics?dfrom=2025-02-01&fmt=xlsx")
check(r.status_code == 200 and len(r.content) > 5000, "analytics Excel export")

print("\nRESULT:", "ALL PASSED" if not fails else f"{len(fails)} FAILED -> {fails}")
sys.exit(1 if fails else 0)
