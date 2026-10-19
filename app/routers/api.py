"""Public JSON API for the Die Spare Part Inventory program (and any other system).
Auth: header  X-API-Key: <key from Admin > Settings > API keys>.  Each key belongs to one company."""
from datetime import datetime

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from ..database import get_db, now_local
from ..models import OPEN_STATUSES, ApiKey, Company, Die, PartUsage, RepairRequest
from ..services import notify

router = APIRouter(prefix="/api/v1")


def api_company(db: Session = Depends(get_db), x_api_key: str = Header(default="")) -> Company:
    k = db.query(ApiKey).filter(ApiKey.key == x_api_key, ApiKey.is_active == True).first()
    if not k:
        raise HTTPException(401, "Invalid API key")
    return db.get(Company, k.company_id)


def _req(db, company, dr_no):
    r = db.query(RepairRequest).filter(RepairRequest.company_id == company.id, RepairRequest.is_deleted == False,
                                       or_(RepairRequest.dr_no == dr_no, RepairRequest.legacy_id == dr_no)).first()
    if not r:
        raise HTTPException(404, f"DR {dr_no} not found")
    return r


def _req_json(r: RepairRequest):
    return {"dr_no": r.dr_no, "legacy_id": r.legacy_id, "type": r.request_type, "status": r.status, "die_code": r.die_code,
            "part_no": r.part_no, "part_name": r.part_name, "die_process": r.die_process, "customer": r.customer, "line": r.line,
            "problem": r.problem_text, "technicians": r.technician_names, "is_emergency": r.is_emergency,
            "target_date": r.target_date.isoformat() if r.target_date else None, "created_at": r.created_at.isoformat(),
            "parts_cost": r.parts_cost, "parts": [{"part_no": p.part_no, "part_name": p.part_name, "qty": p.qty, "uom": p.uom,
                                                    "unit_cost": p.unit_cost, "total_cost": p.total_cost, "ref_no": p.ref_no} for p in r.parts]}


@router.get("/ping")
def ping(company: Company = Depends(api_company)):
    return {"ok": True, "company": company.code}


@router.get("/requests/open")
def open_requests(company: Company = Depends(api_company), db: Session = Depends(get_db)):
    rows = db.query(RepairRequest).filter(RepairRequest.company_id == company.id, RepairRequest.is_deleted == False,
                                          RepairRequest.status.in_(OPEN_STATUSES)).order_by(RepairRequest.created_at.desc()).all()
    return {"requests": [_req_json(r) for r in rows]}


@router.get("/requests/{dr_no}")
def get_request(dr_no: str, company: Company = Depends(api_company), db: Session = Depends(get_db)):
    return _req_json(_req(db, company, dr_no))


@router.get("/dies/{code}")
def get_die(code: str, company: Company = Depends(api_company), db: Session = Depends(get_db)):
    d = db.query(Die).filter(Die.company_id == company.id, or_(Die.die_code == code, Die.part_no == code)).first()
    if not d:
        raise HTTPException(404)
    return {"id": d.id, "die_code": d.die_code, "part_no": d.part_no, "part_name": d.part_name, "die_process": d.die_process,
            "customer": d.customer, "model": d.model, "line": d.line}


class SpareIssue(BaseModel):
    dr_no: str
    part_no: str
    part_name: str = ""
    qty: float = 1
    uom: str = "PCS"
    unit_cost: float = 0
    ref_no: str = ""
    issued_by: str = "Spare Part App"
    issued_at: datetime | None = None


@router.post("/spare-issue")
def spare_issue(body: SpareIssue, company: Company = Depends(api_company), db: Session = Depends(get_db)):
    """Called by the Die Spare Part Inventory program after an issue slip is confirmed.
    Links the part cost to the DR / die automatically."""
    r = _req(db, company, body.dr_no)
    if body.ref_no and db.query(PartUsage).filter(PartUsage.request_id == r.id, PartUsage.ref_no == body.ref_no,
                                                   PartUsage.part_no == body.part_no).first():
        return {"ok": True, "duplicate": True, "dr_no": r.dr_no}
    p = PartUsage(company_id=company.id, request_id=r.id, part_no=body.part_no, part_name=body.part_name, qty=body.qty, uom=body.uom,
                  unit_cost=body.unit_cost, total_cost=round(body.qty * body.unit_cost, 2), source="SPARE_APP", ref_no=body.ref_no,
                  user_name=body.issued_by, at=body.issued_at or now_local())
    db.add(p)
    db.flush()
    r.parts_cost = round(sum(x.total_cost or 0 for x in r.parts), 2)
    db.commit()
    notify.emit(db, company, "spare_issued", r, title=f"[{company.code}] เบิกอะไหล่ {body.part_no} x{body.qty:g} → {r.dr_no}",
                body=f"{body.part_name}\nจำนวน {body.qty:g} {body.uom}  ราคา {body.unit_cost * body.qty:,.2f} บาท\nใบเบิก {body.ref_no}\nDR {r.dr_no} • {r.part_no} / {r.die_process}")
    db.commit()
    return {"ok": True, "dr_no": r.dr_no, "parts_cost": r.parts_cost}


class LowStockItem(BaseModel):
    part_no: str
    part_name: str = ""
    on_hand: float = 0
    min_stock: float = 0
    location: str = ""


class LowStock(BaseModel):
    items: list[LowStockItem] = Field(default_factory=list)


@router.post("/low-stock")
def low_stock(body: LowStock, company: Company = Depends(api_company), db: Session = Depends(get_db)):
    if not body.items:
        return {"ok": True, "sent": 0}
    text = "\n".join(f"{i.part_no} {i.part_name} • คงเหลือ {i.on_hand:g} / Min {i.min_stock:g} {('• ' + i.location) if i.location else ''}" for i in body.items[:20])
    notify.emit(db, company, "low_stock", None, title=f"[{company.code}] อะไหล่ต่ำกว่า Min {len(body.items)} รายการ", body=text,
                link=company.settings.get("spare_app_url") or "/")
    db.commit()
    return {"ok": True, "sent": len(body.items)}


class StrokeIn(BaseModel):
    die_code: str = Field("", description="Die Code (or use part_no + die_process)")
    part_no: str = ""
    die_process: str = ""
    strokes: int = Field(..., ge=0, description="Strokes to ADD (mode=add) or the counter TOTAL (mode=total)")
    mode: str = Field("add", description="add | total")
    note: str = ""


@router.post("/strokes", summary="Report die strokes from a press counter / PLC (drives stroke-based PM)")
def api_strokes(items: list[StrokeIn], company: Company = Depends(api_company), db: Session = Depends(get_db)):
    from ..services import pm as pm_service
    out = []
    for it in items[:500]:
        die = pm_service.find_die(db, company.id, it.die_code, it.part_no, it.die_process)
        if not die:
            out.append({"die_code": it.die_code, "part_no": it.part_no, "ok": False, "error": "die not found"})
            continue
        try:
            pm_service.record_strokes(db, company.id, die, it.strokes, "total" if it.mode == "total" else "add", "API", "api", note=it.note)
            out.append({"die_code": die.die_code, "part_no": die.part_no, "ok": True, "stroke_count": die.stroke_count,
                        "since_pm": die.strokes_since_pm, "pm_pct": die.stroke_pct})
        except ValueError as e:
            out.append({"die_code": die.die_code, "part_no": die.part_no, "ok": False, "error": str(e)})
    db.commit()
    return {"results": out}
