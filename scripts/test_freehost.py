"""Free-host readiness checks (run after smoke_test.py + test_admin_tools.py).
Usage: python scripts/test_freehost.py [base_url]"""
import re
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8790"
fails = []


def check(cond, msg):
    print(("  OK  " if cond else "  FAIL") + " " + msg)
    if not cond:
        fails.append(msg)


def client(user, pw="1234", headers=None):
    c = httpx.Client(base_url=BASE, follow_redirects=True, timeout=120, headers=headers or {})
    c.post("/login", data={"username": user, "password": pw, "next": "/"})
    return c


print("== keep-alive endpoint (cron-job.org) does not need login or DB")
r = httpx.get(BASE + "/healthz")
check(r.status_code == 200 and r.json()["ok"], f"/healthz {r.json()}")

c = client("ah_admin")

print("== very long text is trimmed instead of crashing Postgres")
prod = c
long_name = "VERY LONG PART NAME " * 20      # 400 chars, column is 160
pid = re.search(r'<option value="(\d+)"[^>]*>ครีบรู', prod.get("/requests/new").text).group(1)
r = prod.post("/requests/new", data={"request_type": "REPAIR", "die_id": "", "part_no": "LONG-TEST-" + "X" * 100,
                                     "part_name": long_name, "die_process": "OP10 " + "P" * 200, "line": "Line3",
                                     "required_day": "3", "problem_id": pid, "requester_phone": "0" * 50})
check(r.status_code == 200 and "/requests/" in str(r.url), f"request with 400-char fields saved -> {r.url}")

print("== audit log search")
r = c.get("/admin/audit", params={"action": "login"})
check(r.status_code == 200 and "login" in r.text, "audit filter by action")
r = c.get("/admin/audit", params={"q": "ah_admin"})
check(r.status_code == 200, "audit text search")

print("== chart PNG export script present on dashboard")
check("png-btn" in c.get("/static/js/app.js").text, "PNG export JS served")

print("== gzip on HTML (saves free-host bandwidth)")
r = httpx.get(BASE + "/login", headers={"Accept-Encoding": "gzip"})
check(r.headers.get("content-encoding") == "gzip", "login page gzipped")

print("== BASE_URL learned from proxy headers (Render/Koyeb send X-Forwarded-*) — LAN http login still works")
hdr = {"X-Forwarded-Proto": "https", "X-Forwarded-Host": "die-test.onrender.com"}
r = httpx.post(BASE + "/login", data={"username": "ah_admin", "password": "1234", "next": "/"}, headers=hdr)
check("secure" in r.headers.get("set-cookie", "").lower(), "https connection -> Secure cookie")
check(httpx.get(BASE + "/healthz").json()["base_url"] == "https://die-test.onrender.com", "BASE_URL auto-detected")
lan = client("ah_admin")
check("logout" in lan.get("/").text.lower(), "plain http (LAN) login still works afterwards")

print("\nRESULT:", "ALL PASSED" if not fails else f"{len(fails)} FAILED -> {fails}")
sys.exit(1 if fails else 0)
