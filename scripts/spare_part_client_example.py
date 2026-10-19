"""Drop-in snippet for the existing Die Spare Part Inventory program (FastAPI/SQLite).
Call `push_issue()` right after an issue slip is confirmed, and `push_low_stock()` from the reorder-alert job.
Also exposes the endpoint the DMS calls when a technician scans a part QR:  GET /api/parts/{code}"""
import os

import httpx

DMS_URL = os.getenv("DMS_URL", "https://die-management.onrender.com")
DMS_KEY = os.getenv("DMS_API_KEY", "dms_xxxxxxxx")   # Admin > Settings > API keys in the DMS


def push_issue(dr_no: str, part_no: str, part_name: str, qty: float, unit_cost: float, ref_no: str, issued_by: str):
    """Link an issue slip to a DR. The DMS adds it to the job's parts list and parts cost and notifies the Die Leader."""
    r = httpx.post(f"{DMS_URL}/api/v1/spare-issue", headers={"X-API-Key": DMS_KEY}, timeout=10,
                   json={"dr_no": dr_no, "part_no": part_no, "part_name": part_name, "qty": qty, "unit_cost": unit_cost,
                         "ref_no": ref_no, "issued_by": issued_by})
    r.raise_for_status()
    return r.json()


def push_low_stock(items):
    """items = [{"part_no": .., "part_name": .., "on_hand": .., "min_stock": .., "location": ..}, ...]"""
    r = httpx.post(f"{DMS_URL}/api/v1/low-stock", headers={"X-API-Key": DMS_KEY}, timeout=10, json={"items": items})
    r.raise_for_status()
    return r.json()


def open_requests():
    """Dropdown of open DR numbers for the issue form in the spare part program."""
    r = httpx.get(f"{DMS_URL}/api/v1/requests/open", headers={"X-API-Key": DMS_KEY}, timeout=10)
    return [x["dr_no"] for x in r.json()["requests"]]


# ---- add to the spare part program's FastAPI app so the DMS scan page can look parts up ----
# from fastapi import APIRouter, Header, HTTPException
# router = APIRouter()
# @router.get("/api/parts/{code}")
# def part_lookup(code: str, x_api_key: str = Header(default="")):
#     if x_api_key != os.getenv("SPARE_API_KEY"): raise HTTPException(401)
#     p = db.query(Part).filter((Part.part_id == code) | (Part.part_no == code)).first()
#     if not p: raise HTTPException(404)
#     return {"part_no": p.part_no, "part_name": p.name, "on_hand": p.on_hand, "unit_cost": p.unit_cost, "location": p.location, "uom": p.uom}
