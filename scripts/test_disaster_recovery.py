"""Disaster recovery / moving host: full backup on server A -> restore on an EMPTY server B.
Usage: python scripts/test_disaster_recovery.py http://source http://empty_target"""
import re
import sys

import httpx

SRC, DST = sys.argv[1], sys.argv[2]
fails = []


def check(cond, msg):
    print(("  OK  " if cond else "  FAIL") + " " + msg)
    if not cond:
        fails.append(msg)


def login(base):
    c = httpx.Client(base_url=base, follow_redirects=True, timeout=600)
    c.post("/login", data={"username": "admin", "password": "admin1234", "next": "/"})
    return c


def jobs(c):
    m = re.search(r"([\d,]+) รายการ", c.get("/requests?view=all").text)
    return int(m.group(1).replace(",", "")) if m else -1


src = login(SRC)
r = src.post("/admin/backup", data={"scope": "all", "include_images": "1"})
name = re.search(r"(dms_backup_[\w\-]+\.zip)", r.text).group(1)
blob = src.get(f"/admin/backup/download/{name}").content
check(len(blob) > 10000, f"full backup {name} {len(blob) / 1048576:.2f} MB")
n_src, dies_src = jobs(src), re.search(r"([\d,]+) แม่พิมพ์", src.get("/dies").text).group(1)

dst = login(DST)
check(dst.get("/").status_code == 200, "empty server: home redirects to Companies instead of crashing")
r = dst.get("/admin/backup")
check(r.status_code == 200 and "ยังไม่มีข้อมูล" in r.text, "empty server: Backup page opens with restore guidance")
check(dst.get("/admin/users").status_code == 200, "empty server: other admin pages redirect safely")
r = dst.post("/admin/backup/restore", files={"file": (name, blob, "application/zip")}, data={"replace": "1"})
msg = re.search(r"กู้คืนแล้ว[^<]{0,200}", r.text)
check(msg is not None and "/login" in str(r.url), f"restore then forced re-login: {msg.group(0)[:150] if msg else r.text[:200]}")
dst = login(DST)
check(jobs(dst) == n_src, f"jobs restored {jobs(dst)} == source {n_src}")
dies_dst = re.search(r"([\d,]+) แม่พิมพ์", dst.get("/dies").text)
check(dies_dst and dies_dst.group(1) == dies_src, f"dies restored {dies_dst.group(1) if dies_dst else None} == {dies_src}")
ah = httpx.Client(base_url=DST, follow_redirects=True)
ah.post("/login", data={"username": "ah_admin", "password": "1234"})
check("logout" in ah.get("/").text.lower(), "company users can log in on the new server")
r = dst.get("/requests/new")
check(r.status_code == 200, "new request form works after restore")
print("\nRESULT:", "ALL PASSED" if not fails else f"{len(fails)} FAILED -> {fails}")
sys.exit(1 if fails else 0)
