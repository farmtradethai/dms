"""Deep analytics for die maintenance — everything computed from the ticket time-stamps.

Questions it answers (what a Die Maintenance manager actually asks):
  1. WHAT breaks?         Pareto of problems & root causes, problem x cause matrix
  2. WHICH dies?          reliability table: repairs, MTBF, repeat-within-7-days (ineffective repair), cost, risk score
  3. WHERE is the time?   lead time per workflow stage per month (the bottleneck), on-time vs required day
  4. HOW good is repair?  first-time-pass (no try-out return / QC NG), NG rate by cause & technician
  5. WHO / WHEN?          technician load & speed, line x month heat-map, hour-of-day / weekday pattern
  6. WHAT is waiting?     backlog aging by status
  7. Auto insights        plain-language findings with links"""
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from statistics import mean, median

from sqlalchemy.orm import Session, selectinload

from ..models import ST, OPEN_STATUSES, Assignment, RepairRequest, StatusHistory

STAGES = [("แจ้ง → Foreman", "created_at", "foreman_at"), ("Foreman → หัวหน้า Die", "foreman_at", "leader_at"),
          ("หัวหน้า → ช่างรับงาน", "leader_at", "tech_received_at"), ("ช่างรับ → ซ่อมเสร็จ", "tech_received_at", "tech_completed_at"),
          ("ซ่อมเสร็จ → Try-out", "tech_completed_at", "tryout_at"), ("Try-out → QC", "tryout_at", "qc_at")]
OPEN_GROUP = {ST["WAIT_FOREMAN"]: "รอ Foreman", ST["WAIT_LEADER"]: "รอหัวหน้า Die", ST["ASSIGNED"]: "รอช่างรับ",
              ST["IN_PROGRESS"]: "กำลังซ่อม", ST["RETURNED"]: "ส่งกลับซ่อม", ST["NG_RETURN"]: "ส่งกลับซ่อม",
              ST["REPAIR_DONE"]: "รอ Try-out", ST["WAIT_QC"]: "รอ QC"}
AGE_BUCKETS = [("≤ 1 วัน", 1), ("1-3 วัน", 3), ("3-7 วัน", 7), ("7-30 วัน", 30), ("> 30 วัน", 10 ** 6)]
REQ_DAYS = {"<1": 1, "3": 3, "5": 5, ">7": 7}


def _h(a, b):
    if not a or not b:
        return None
    v = (b - a).total_seconds() / 3600
    return v if v >= 0 else None


def _med(vals):
    vals = [v for v in vals if v is not None]
    return round(median(vals), 1) if vals else None


def _pareto(counter: Counter, top=12):
    total = sum(counter.values()) or 1
    out, cum = [], 0
    for k, n in counter.most_common(top):
        cum += n
        out.append({"name": k, "n": n, "pct": round(100 * n / total, 1), "cum": round(100 * cum / total, 1)})
    return out


def _first_pass(r):
    return (r.ng_count or 0) == 0 and (r.tryout_count or 0) <= 1


def _on_time(r):
    """Repair finished (technician done) by the end of the target date."""
    if not r.target_date or not r.tech_completed_at:
        return None
    return r.tech_completed_at <= datetime.combine(r.target_date, time.max)


def load(db: Session, company_id: int, start, end, line="", customer="", rtype="", user=None):
    q = (db.query(RepairRequest).options(selectinload(RepairRequest.assignments).selectinload(Assignment.user))
         .filter(RepairRequest.company_id == company_id, RepairRequest.is_deleted == False,
                 RepairRequest.created_at >= datetime.combine(start, time.min),
                 RepairRequest.created_at <= datetime.combine(end, time.max)))
    if line:
        q = q.filter(RepairRequest.line == line)
    if customer:
        q = q.filter(RepairRequest.customer == customer)
    if rtype:
        q = q.filter(RepairRequest.request_type == rtype)
    if user is not None:
        from .. import perms
        q = perms.scope_requests(q, user)          # approvers in "own lines" mode analyse their lines only
    return q.order_by(RepairRequest.created_at).all()


def analyse(db: Session, company_id: int, start, end, line="", customer="", rtype="", now=None, user=None) -> dict:
    now = now or datetime.now()
    rows = load(db, company_id, start, end, line, customer, rtype, user)
    repairs = [r for r in rows if r.request_type in ("REPAIR", "ACCIDENT")]
    done = [r for r in rows if r.tech_completed_at]
    closed = [r for r in rows if r.status == ST["CLOSED"]]
    judged = [r for r in rows if r.qc_at or r.status == ST["CLOSED"]]

    # ---------------- headline
    nets = [r.net_repair_minutes for r in done if r.net_repair_minutes]
    ontime = [o for o in (_on_time(r) for r in done) if o is not None]
    by_die = defaultdict(list)
    for r in repairs:
        by_die[(r.part_no, r.die_process)].append(r)
    gaps = []
    for lst in by_die.values():
        ts = sorted(x.created_at for x in lst)
        gaps += [(b - a).total_seconds() / 86400 for a, b in zip(ts, ts[1:])]
    repeat_dies = {k for k, v in by_die.items() if len(v) >= 3}
    head = {
        "jobs": len(rows), "repairs": len(repairs), "pm": sum(1 for r in rows if r.request_type == "PM"),
        "closed": len(closed), "open": sum(1 for r in rows if r.status in OPEN_STATUSES),
        "mttr_min": round(mean(nets)) if nets else None, "mtbf_days": round(mean(gaps), 1) if gaps else None,
        "ontime_pct": round(100 * sum(ontime) / len(ontime)) if ontime else None, "ontime_n": len(ontime),
        "ftp_pct": round(100 * sum(1 for r in judged if _first_pass(r)) / len(judged)) if judged else None, "ftp_n": len(judged),
        "repeat_die_pct": round(100 * sum(len(by_die[k]) for k in repeat_dies) / len(repairs)) if repairs else None,
        "repeat_dies": len(repeat_dies),
        "lead_done_h": _med([_h(r.created_at, r.tech_completed_at) for r in done]),
        "emergency_pct": round(100 * sum(1 for r in rows if r.is_emergency) / len(rows)) if rows else None,
        "cost": round(sum(r.parts_cost or 0 for r in rows), 2), "downtime": sum(r.downtime_minutes or 0 for r in rows),
        "other_pct": round(100 * sum(1 for r in rows if (r.problem_text or "").startswith("อื่น")) / len(rows)) if rows else None,
    }

    # ---------------- 1. what breaks
    pareto_problem = _pareto(Counter(r.problem_text or "-" for r in rows))
    pareto_cause = _pareto(Counter(r.root_cause_text or "(ไม่ระบุ)" for r in done))
    top_p = [p["name"] for p in pareto_problem[:8]]
    top_c = [c["name"] for c in pareto_cause[:8]]
    mx = Counter((r.problem_text or "-", r.root_cause_text or "(ไม่ระบุ)") for r in done)
    matrix = {"rows": top_p, "cols": top_c, "cells": [[mx.get((p, c), 0) for c in top_c] for p in top_p],
              "max": max([mx.get((p, c), 0) for p in top_p for c in top_c] or [1])}

    # ---------------- 2. which dies
    die_rows = []
    for (pn, proc), lst in by_die.items():
        lst.sort(key=lambda x: x.created_at)
        ts = [x.created_at for x in lst]
        g = [(b - a).total_seconds() / 86400 for a, b in zip(ts, ts[1:])]
        # repeat within 7 days after the previous repair was finished = repair did not hold
        rep7 = 0
        for a, b in zip(lst, lst[1:]):
            fin = a.tech_completed_at or a.created_at
            if (b.created_at - fin).total_seconds() <= 7 * 86400:
                rep7 += 1
        n_ng = sum((x.ng_count or 0) + max((x.tryout_count or 0) - 1, 0) for x in lst)
        cost = sum(x.parts_cost or 0 for x in lst)
        net = [x.net_repair_minutes for x in lst if x.net_repair_minutes]
        top_problem = Counter(x.problem_text or "-" for x in lst).most_common(1)[0][0]
        risk = len(lst) * 2 + rep7 * 3 + n_ng * 2 + (5 if g and mean(g) < 14 else 0)
        die_rows.append({"part_no": pn, "process": proc, "die_id": lst[-1].die_id, "customer": lst[-1].customer, "line": lst[-1].line,
                         "repairs": len(lst), "mtbf": round(mean(g), 1) if g else None, "last": ts[-1], "rep7": rep7, "ng": n_ng,
                         "mttr": round(mean(net)) if net else None, "cost": round(cost, 2), "top_problem": top_problem, "risk": risk})
    die_rows.sort(key=lambda d: (-d["risk"], -d["repairs"]))
    head["rep7_dies"] = sum(1 for d in die_rows if d["rep7"])
    head["rep7_top"] = max(die_rows, key=lambda d: d["rep7"]) if die_rows else None
    head["legacy_pct"] = round(100 * sum(1 for r in rows if r.legacy_id) / len(rows)) if rows else 0

    # ---------------- 3. where is the time
    months = sorted({r.created_at.strftime("%Y-%m") for r in rows})
    stage_month = {m: [] for m in months}
    for m in months:
        mr = [r for r in rows if r.created_at.strftime("%Y-%m") == m]
        stage_month[m] = [_med([_h(getattr(r, a), getattr(r, b)) for r in mr]) or 0 for _, a, b in STAGES]
    stage_all = [{"name": n, "median_h": _med([_h(getattr(r, a), getattr(r, b)) for r in rows]),
                  "n": sum(1 for r in rows if getattr(r, a) and getattr(r, b))} for n, a, b in STAGES]
    ontime_month, sla_by_req = [], []
    for m in months:
        o = [x for x in (_on_time(r) for r in done if r.created_at.strftime("%Y-%m") == m) if x is not None]
        ontime_month.append(round(100 * sum(o) / len(o)) if o else None)
    for k, d in REQ_DAYS.items():
        lst = [r for r in rows if r.required_day == k]
        o = [x for x in (_on_time(r) for r in lst) if x is not None]
        actual = _med([_h(r.created_at, r.tech_completed_at) for r in lst])
        sla_by_req.append({"req": k, "target_h": d * 24, "n": len(lst), "ontime": round(100 * sum(o) / len(o)) if o else None,
                           "actual_h": actual})

    # ---------------- 4. quality of repair
    ftp_month = []
    for m in months:
        j = [r for r in judged if r.created_at.strftime("%Y-%m") == m]
        ftp_month.append(round(100 * sum(1 for r in j if _first_pass(r)) / len(j)) if j else None)
    ng_by_cause = defaultdict(lambda: [0, 0])
    for r in judged:
        c = r.root_cause_text or "(ไม่ระบุ)"
        ng_by_cause[c][0] += 1
        ng_by_cause[c][1] += 0 if _first_pass(r) else 1
    ng_cause = sorted([{"name": k, "n": v[0], "ng": v[1], "pct": round(100 * v[1] / v[0])} for k, v in ng_by_cause.items() if v[0] >= 3],
                      key=lambda x: -x["pct"])[:10]

    # ---------------- 5. who / when
    tech = defaultdict(lambda: {"jobs": 0, "closed": 0, "net": [], "recv": [], "judged": 0, "pass": 0, "open": 0})
    for r in rows:
        for a in r.assignments:
            if not a.user:
                continue
            t = tech[a.user.full_name]
            t["jobs"] += 1
            t["closed"] += r.status == ST["CLOSED"]
            t["open"] += r.status in OPEN_STATUSES
            if r.net_repair_minutes:
                t["net"].append(r.net_repair_minutes / max(len(r.assignments), 1))
            rv = _h(r.leader_at, r.tech_received_at)
            if rv is not None:
                t["recv"].append(rv)
            if r in judged:
                t["judged"] += 1
                t["pass"] += _first_pass(r)
    tech_rows = sorted([{"name": k, "jobs": v["jobs"], "closed": v["closed"], "open": v["open"],
                         "avg_net": round(mean(v["net"])) if v["net"] else None, "recv_h": _med(v["recv"]),
                         "ftp": round(100 * v["pass"] / v["judged"]) if v["judged"] else None} for k, v in tech.items()],
                       key=lambda x: -x["jobs"])
    lines = [l for l, _ in Counter(r.line or "-" for r in rows).most_common(12)]
    lm = Counter((r.line or "-", r.created_at.strftime("%Y-%m")) for r in rows)
    heat = {"rows": lines, "cols": months, "cells": [[lm.get((l, m), 0) for m in months] for l in lines],
            "max": max(lm.values()) if lm else 1}
    hours = [0] * 24
    for r in rows:
        hours[r.created_at.hour] += 1
    wd = [0] * 7
    for r in rows:
        wd[r.created_at.weekday()] += 1
    cust = defaultdict(lambda: {"n": 0, "net": []})
    for r in rows:
        c = cust[r.customer or "-"]
        c["n"] += 1
        if r.net_repair_minutes:
            c["net"].append(r.net_repair_minutes)
    cust_rows = sorted([{"name": k, "n": v["n"], "mttr": round(mean(v["net"])) if v["net"] else None} for k, v in cust.items()],
                       key=lambda x: -x["n"])[:12]
    downtime_line = sorted([(l, sum(r.downtime_minutes or 0 for r in rows if (r.line or "-") == l)) for l in lines], key=lambda x: -x[1])
    cost_month = [round(sum(r.parts_cost or 0 for r in rows if r.created_at.strftime("%Y-%m") == m), 2) for m in months]
    vol_month = [{"m": m, "repair": sum(1 for r in rows if r.created_at.strftime("%Y-%m") == m and r.request_type != "PM"),
                  "pm": sum(1 for r in rows if r.created_at.strftime("%Y-%m") == m and r.request_type == "PM")} for m in months]

    # ---------------- 6. backlog aging (always "now", regardless of period start)
    open_rows = [r for r in rows if r.status in OPEN_STATUSES]
    groups = list(dict.fromkeys(OPEN_GROUP.values()))
    aging = {g: [0] * len(AGE_BUCKETS) for g in groups}
    for r in open_rows:
        age = (now - r.created_at).total_seconds() / 86400
        for i, (_, lim) in enumerate(AGE_BUCKETS):
            if age <= lim:
                aging[OPEN_GROUP.get(r.status, "อื่น ๆ")][i] += 1
                break

    res = {"head": head, "pareto_problem": pareto_problem, "pareto_cause": pareto_cause, "matrix": matrix, "dies": die_rows[:25],
           "months": months, "stage_names": [s[0] for s in STAGES], "stage_month": stage_month, "stage_all": stage_all,
           "ontime_month": ontime_month, "sla_by_req": sla_by_req, "ftp_month": ftp_month, "ng_cause": ng_cause,
           "tech": tech_rows, "heat": heat, "hours": hours, "weekday": wd, "customers": cust_rows, "downtime_line": downtime_line,
           "cost_month": cost_month, "vol_month": vol_month, "aging": aging, "age_labels": [a[0] for a in AGE_BUCKETS],
           "n_rows": len(rows)}
    res["insights"] = insights(res)
    return res


def insights(a: dict) -> list:
    """Plain-language findings, most actionable first: (level, text, link)."""
    out = []
    h = a["head"]
    pp = a["pareto_problem"]
    if pp:
        top3 = sum(p["n"] for p in pp[:3])
        total = sum(p["n"] for p in pp) or 1
        out.append(("info", f"ปัญหา 3 อันดับแรก ({', '.join(p['name'] for p in pp[:3])}) คิดเป็น {round(100 * top3 / max(total, 1))}% ของใบงาน "
                            f"— แก้ 3 เรื่องนี้ได้ จะลดงานซ่อมได้มากที่สุด", "#pareto"))
    if h["other_pct"] and h["other_pct"] >= 15:
        out.append(("warn", f"ปัญหา 'อื่นๆ' สูงถึง {h['other_pct']}% — วิเคราะห์ไม่ได้ ควรเพิ่มตัวเลือกปัญหาใน Master ให้ตรงหน้างาน", "/admin/masters"))
    st = [s for s in a["stage_all"] if s["median_h"] is not None]
    if st:
        worst = max(st, key=lambda s: s["median_h"])
        out.append(("danger" if worst["median_h"] >= 24 else "warn",
                    f"คอขวดคือขั้น '{worst['name']}' ใช้เวลา median {worst['median_h']} ชม. (จาก {worst['n']} ใบ) "
                    f"— เวลารอ ไม่ใช่เวลาซ่อม; ตั้งเตือนผู้รับผิดชอบขั้นนี้", "#stages"))
    if h["ontime_pct"] is not None:
        lvl = "success" if h["ontime_pct"] >= 85 else ("warn" if h["ontime_pct"] >= 60 else "danger")
        out.append((lvl, f"ซ่อมเสร็จทันวันที่ต้องการ {h['ontime_pct']}% (จาก {h['ontime_n']} ใบ)", "#sla"))
    sla_urgent = next((s for s in a["sla_by_req"] if s["req"] == "<1" and s["n"]), None)
    if sla_urgent and h["emergency_pct"] and h["emergency_pct"] >= 50:
        out.append(("warn", f"งาน 'ด่วน < 1 วัน' มีถึง {h['emergency_pct']}% ของทั้งหมด — เมื่อทุกงานด่วน ก็ไม่มีงานไหนด่วนจริง "
                            f"(ทำได้ทัน {sla_urgent['ontime'] if sla_urgent['ontime'] is not None else '-'}%) ควรกำหนดเกณฑ์ความเร่งด่วนจาก Stock OH", "#sla"))
    top = h.get("rep7_top")
    if h.get("rep7_dies") and top:
        out.append(("danger", f"{h['rep7_dies']} แม่พิมพ์เสียซ้ำภายใน 7 วันหลังซ่อม (มากสุด {top['part_no']} {top['process']} ซ้ำ {top['rep7']} ครั้ง) "
                              f"— การซ่อมไม่ถาวร ควรหาสาเหตุราก/วางแผน PM", "#dies"))
    if h["repeat_dies"]:
        out.append(("warn", f"{h['repeat_dies']} แม่พิมพ์ถูกแจ้งซ่อม ≥ 3 ครั้งในช่วงนี้ รวม {h['repeat_die_pct']}% ของงานซ่อม — เป้าหมายแรกของแผน PM", "#dies"))
    if h["ftp_pct"] is not None:
        note = (" — หมายเหตุ: ข้อมูลเก่าจาก Power Apps ไม่ได้บันทึกการส่งกลับ ค่านี้จะสะท้อนจริงเมื่อใช้ระบบใหม่"
                if h.get("legacy_pct", 0) >= 50 else "")
        out.append(("success" if h["ftp_pct"] >= 90 and not note else "info",
                    f"ซ่อมผ่านในครั้งเดียว (ไม่ถูกส่งกลับจาก Try-out/QC) {h['ftp_pct']}%{note}", "#quality"))
    if a["ng_cause"] and a["ng_cause"][0]["pct"] >= 20:
        c = a["ng_cause"][0]
        out.append(("warn", f"สาเหตุ '{c['name']}' ถูกส่งกลับซ่อม {c['pct']}% ({c['ng']}/{c['n']}) — ทบทวนวิธีซ่อม/มาตรฐานงาน", "#quality"))
    tr = [t for t in a["tech"] if t["jobs"] >= 5]
    if len(tr) >= 2 and tr[0]["jobs"] >= 2 * tr[-1]["jobs"]:
        out.append(("info", f"ภาระงานช่างไม่สมดุล: {tr[0]['name']} {tr[0]['jobs']} งาน vs {tr[-1]['name']} {tr[-1]['jobs']} งาน", "#tech"))
    hrs = a["hours"]
    night = sum(hrs[20:]) + sum(hrs[:8])
    total = sum(hrs) or 1
    out.append(("info", f"แจ้งซ่อมกะดึก (20:00-08:00) {round(100 * night / total)}% · ชั่วโมงที่แจ้งมากสุด {hrs.index(max(hrs)):02d}:00 น.", "#when"))
    old = sum(a["aging"].get(g, [0] * 5)[4] for g in a["aging"])
    if old:
        out.append(("warn", f"มีงานค้างเกิน 30 วัน {old} ใบ — ปิด/ยกเลิก หรือใช้ 'ปิดงานเก่าที่ค้าง'", "/requests?view=open&flag=overdue"))
    return out
