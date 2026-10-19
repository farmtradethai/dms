"""Multi-channel notifications.
Channels (per company, configured by Admin):
  line      : LINE Messaging API push  {channel_access_token, to}           (text + image; LINE Notify was shut down 31-03-2025)
  telegram  : Bot API                   {bot_token, chat_id}                 (text + photo + document)
  discord   : Webhook                   {webhook_url}                        (text + file)
  email     : Brevo transactional API   {api_key, sender_email, sender_name, to: "a@x.com,b@y.com"} (html + attachments)
  webhook   : Generic JSON POST         {url, secret}                        (JSON payload)
Sending runs in a background thread so the UI never waits for a slow network."""
import base64
import json
import threading
from datetime import datetime

import httpx
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal, now_local
from ..models import Notification, NotificationChannel, RepairRequest

EVENTS = {
    "submit": ("ใบแจ้งซ่อมใหม่", ["foreman", "die_leader", "manager"]),
    "emergency": ("งานด่วน (Emergency)", ["foreman", "die_leader", "technician", "manager"]),
    "foreman_approve": ("Foreman อนุมัติแล้ว", ["die_leader"]),
    "foreman_reject": ("Foreman ไม่อนุมัติ", ["production"]),
    "leader_assign": ("มอบหมายงานช่าง", ["technician"]),
    "leader_reject": ("หัวหน้า Die ไม่อนุมัติ", ["production", "foreman"]),
    "receive": ("ช่างรับงานแล้ว", ["production"]),
    "complete": ("ซ่อมเสร็จ รอ Try-out", ["production", "foreman"]),
    "tryout_accept": ("Try-out ผ่าน รอ QC", ["qc"]),
    "tryout_return": ("Try-out ไม่ผ่าน ส่งกลับซ่อม", ["die_leader", "technician"]),
    "qc_ok": ("QC OK ปิดงาน", ["production", "die_leader"]),
    "qc_ng": ("QC NG ส่งกลับซ่อม", ["die_leader", "technician", "production"]),
    "cancel": ("ยกเลิกใบงาน", ["die_leader"]),
    "reopen": ("เปิดงานใหม่", ["die_leader", "qc"]),
    "overdue": ("งานเกินกำหนด", ["die_leader", "foreman", "manager"]),
    "pm_due": ("PM ถึงกำหนด", ["die_leader", "manager"]),
    "low_stock": ("อะไหล่ต่ำกว่า Min", ["die_leader", "store"]),
    "spare_issued": ("เบิกอะไหล่แล้ว", ["die_leader"]),
    "storage_warning": ("พื้นที่เก็บรูปใกล้เต็ม", ["admin", "manager"]),
    "data_anomaly": ("พบข้อมูลผิดปกติ", ["die_leader", "manager"]),
    "daily_report": ("รายงานประจำวัน (แนบ Excel)", []),
    "backup": ("ไฟล์ Backup อัตโนมัติ (แนบ ZIP)", []),
}
# Events that carry company data as a file: sent ONLY to channels that tick them explicitly.
OPT_IN_EVENTS = {"backup", "daily_report"}
LEVELS = {"emergency": "danger", "qc_ng": "danger", "tryout_return": "warn", "overdue": "warn", "pm_due": "warn",
          "low_stock": "warn", "storage_warning": "warn", "qc_ok": "success", "complete": "success"}


def _req_summary(req: RepairRequest) -> str:
    lines = [f"{req.dr_no} • {req.request_type}" + (" • ด่วน" if req.is_emergency else ""),
             f"Die: {req.part_no} / {req.die_process}",
             f"ลูกค้า: {req.customer or '-'}  Line: {req.line or '-'}",
             f"ปัญหา: {req.problem_text or '-'}"]
    if req.technician_names:
        lines.append(f"ช่าง: {req.technician_names}")
    if req.target_date:
        lines.append(f"กำหนดเสร็จ: {req.target_date:%d-%m-%Y}")
    lines.append(f"สถานะ: {req.status}")
    return "\n".join(lines)


def emit(db: Session, company, event: str, req: RepairRequest = None, title: str = None, body: str = None,
         link: str = None, image_url: str = None, attachment: tuple = None, actor_id: int = None):
    """Creates in-app notifications and fans out to external channels. Safe to call anywhere.
    Ticket events go to the PEOPLE who must act next (bell + personal e-mail); other events go to roles."""
    label, roles = EVENTS.get(event, (event, ["manager"]))
    title = title or f"[{company.code}] {label}"
    body = body or (_req_summary(req) if req else "")
    link = link or (f"/requests/{req.id}" if req else "/")
    if req is not None and event in PERSONAL_EVENTS:
        try:
            route_to_people(db, company, event, req, title, body, link, actor_id)
        except Exception as e:          # notifications must never break the workflow
            print(f"[notify] personal routing failed: {e}")
    else:
        for role in roles if event not in OPT_IN_EVENTS else []:
            db.add(Notification(company_id=company.id, role=role, title=title, body=body[:400], link=link,
                                level=LEVELS.get(event, "info")))
    channels = db.query(NotificationChannel).filter(NotificationChannel.company_id == company.id,
                                                    NotificationChannel.is_active == True).all()
    full_link = link if link.startswith("http") else settings.BASE_URL + link
    payload = {"event": event, "company": company.code, "title": title, "text": body, "link": full_link,
               "image_url": image_url, "dr_no": req.dr_no if req else None, "status": req.status if req else None,
               "at": now_local().isoformat(timespec="seconds")}
    def wants(c):
        if event in OPT_IN_EVENTS:
            return event in c.events
        return (not c.events or event in c.events
                or (event == "submit" and req is not None and req.is_emergency and "emergency" in c.events))
    targets = [(c.type, c.config, c.events) for c in channels if wants(c)]
    if targets:
        threading.Thread(target=_fanout, args=(targets, payload, attachment), daemon=True).start()
    return len(targets)


MAX_ATTACH = {"telegram": 49 * 1024 * 1024, "discord": 9 * 1024 * 1024, "email": 9 * 1024 * 1024}


def _fanout(targets, payload, attachment):
    for typ, cfg, _ in targets:
        att, pl = attachment, payload
        if att and len(att[1]) > MAX_ATTACH.get(typ, 0):
            pl = dict(payload, text=payload["text"] + f"\n(ไฟล์ {att[0]} ใหญ่เกินกว่าช่องทางนี้รับได้ — ดาวน์โหลดจากระบบ)")
            att = None
        try:
            send(typ, cfg, pl, att)
        except Exception as e:  # never crash the app because of a chat API
            print(f"[notify] {typ} failed: {e}")


def send(typ: str, cfg: dict, payload: dict, attachment=None) -> str:
    text = f"{payload['title']}\n{payload['text']}\n{payload['link']}"
    with httpx.Client(timeout=15) as c:
        if typ == "line":
            msgs = [{"type": "text", "text": text[:4900]}]
            if payload.get("image_url") and payload["image_url"].startswith("https://"):
                msgs.append({"type": "image", "originalContentUrl": payload["image_url"],
                             "previewImageUrl": payload["image_url"]})
            r = c.post("https://api.line.me/v2/bot/message/push",
                       headers={"Authorization": f"Bearer {cfg.get('channel_access_token', '')}"},
                       json={"to": cfg.get("to", ""), "messages": msgs})
        elif typ == "telegram":
            base = f"https://api.telegram.org/bot{cfg.get('bot_token', '')}"
            r = c.post(f"{base}/sendMessage", json={"chat_id": cfg.get("chat_id"), "text": text[:4000]})
            if payload.get("image_url"):
                c.post(f"{base}/sendPhoto", json={"chat_id": cfg.get("chat_id"), "photo": payload["image_url"]})
            if attachment:
                name, data, mime = attachment
                c.post(f"{base}/sendDocument", data={"chat_id": cfg.get("chat_id")}, files={"document": (name, data, mime)})
        elif typ == "discord":
            content = text[:1900]
            if payload.get("image_url"):
                content += f"\n{payload['image_url']}"
            if attachment:
                name, data, mime = attachment
                r = c.post(cfg.get("webhook_url", ""), data={"payload_json": json.dumps({"content": content})},
                           files={"file": (name, data, mime)})
            else:
                r = c.post(cfg.get("webhook_url", ""), json={"content": content})
        elif typ == "email":
            html = "<p>" + payload["text"].replace("\n", "<br>") + f"</p><p><a href='{payload['link']}'>เปิดในระบบ</a></p>"
            if payload.get("image_url"):
                html += f"<p><img src='{payload['image_url']}' style='max-width:480px'></p>"
            body = {"sender": {"email": cfg.get("sender_email"), "name": cfg.get("sender_name", "Die Management")},
                    "to": [{"email": e.strip()} for e in cfg.get("to", "").split(",") if e.strip()],
                    "subject": payload["title"], "htmlContent": html}
            if attachment:
                name, data, mime = attachment
                body["attachment"] = [{"name": name, "content": base64.b64encode(data).decode()}]
            r = c.post("https://api.brevo.com/v3/smtp/email", headers={"api-key": cfg.get("api_key", "")}, json=body)
        elif typ == "webhook":
            r = c.post(cfg.get("url", ""), json=payload, headers={"X-DMS-Secret": cfg.get("secret", "")})
        else:
            return "unknown channel"
    return f"{r.status_code}"


def test_channel(typ: str, cfg: dict) -> str:
    return send(typ, cfg, {"event": "test", "title": "Die Management System — ทดสอบการแจ้งเตือน",
                           "text": "ถ้าได้รับข้อความนี้ แปลว่าตั้งค่าถูกต้อง ✅", "link": settings.BASE_URL})


# ============================================================== per-person routing (who must act next)
PERSONAL_EVENTS = {"submit", "foreman_approve", "foreman_reject", "leader_assign", "leader_reject", "receive", "complete",
                   "tryout_accept", "tryout_return", "qc_ok", "qc_ng", "cancel", "reopen", "spare_issued"}
WHY = {"foreman": "คุณเป็น Foreman/Engineer ผู้อนุมัติขั้นที่ 2", "die_leader": "คุณเป็นหัวหน้า Die Maintenance ผู้อนุมัติ/มอบหมายงาน",
       "technician": "คุณเป็นช่างที่ได้รับมอบหมายงานนี้", "requester": "คุณเป็นผู้แจ้งใบงานนี้",
       "qc": "คุณเป็น QC ผู้ตัดสินผล OK/NG", "tryout": "คุณเป็นผู้รับ Try-out ของ Line นี้ (ตามผังผู้อนุมัติ)", "manager": "คุณเป็นผู้จัดการ (งานด่วน)"}
ACTION_TEXT = {
    "submit": "รอคุณอนุมัติ (Foreman/Engineer)", "foreman_approve": "รอคุณอนุมัติและมอบหมายช่าง",
    "foreman_reject": "ไม่อนุมัติ — กรุณาแก้ไขแล้วส่งใหม่", "leader_reject": "หัวหน้า Die ไม่อนุมัติ — กรุณาแก้ไขแล้วส่งใหม่",
    "leader_assign": "คุณได้รับมอบหมายงานซ่อม", "complete": "ซ่อมเสร็จแล้ว — รอคุณ Try-out",
    "tryout_accept": "Try-out ผ่าน — รอคุณตรวจ QC", "tryout_return": "Try-out ไม่ผ่าน — ต้องซ่อมใหม่",
    "qc_ng": "QC NG — ต้องซ่อมใหม่", "qc_ok": "QC OK — ปิดงานเรียบร้อย", "receive": "ช่างรับงานและเริ่มซ่อมแล้ว",
    "cancel": "ใบงานถูกยกเลิก", "reopen": "ใบงานถูกเปิดใหม่ — รอตรวจ QC", "spare_issued": "มีการเบิกอะไหล่เข้าใบงาน",
}


def _company_users(db, company_id, role):
    from ..models import User
    return db.query(User).filter(User.company_id == company_id, User.role == role, User.is_active == True).all()


def _by_line(users, line):
    """Approvers responsible for the job's Line first; people without a Line cover all lines."""
    if not users:
        return []
    exact = [u for u in users if (u.line or "").strip() and (u.line or "").strip().lower() == (line or "").strip().lower()]
    if exact:
        return exact
    general = [u for u in users if not (u.line or "").strip()]
    return general or users


def recipients(db, company, event, req, reminder=False):
    """Returns [(user, why_key, must_act)] for a ticket event, based on the status the ticket is now in.
    Approvers come from the company's approval routing (Admin › ผังผู้อนุมัติ); backups only on reminders."""
    from ..models import ST
    from .. import approvals
    rt = approvals.routing(db, company.id)
    out = []
    techs = [a.user for a in req.assignments if a.user and a.user.is_active]
    requester = req.requester if (req.requester and req.requester.is_active) else None

    def appr(step):
        return [u for u, backup, _ in rt.approvers(step, req.line, include_backup=reminder)]

    leaders = appr("LEADER")

    def add(users, why, act=True):
        for u in users:
            if u is not None:
                out.append((u, why, act))

    if event == "submit":
        if req.status == ST["WAIT_LEADER"]:                     # PM that skips the foreman step
            add(leaders, "die_leader")
        else:
            add(appr("FOREMAN"), "foreman")
        if req.is_emergency:
            add(leaders, "die_leader", act=req.status == ST["WAIT_LEADER"])
            add(_company_users(db, company.id, "manager"), "manager", act=False)
    elif event == "foreman_approve":
        add(leaders, "die_leader")
    elif event in ("foreman_reject", "leader_reject"):
        add([requester], "requester")
    elif event == "leader_assign":
        add(techs, "technician")
        add([requester], "requester", act=False)
    elif event == "receive":
        add([requester], "requester", act=False)
    elif event == "complete":
        tryout_rules = any(r.step == "TRYOUT" for r in rt.rules)
        if not approvals.requester_tries_out(req):
            requester = None
        if requester:
            add([requester], "requester")
        if tryout_rules or not requester:
            add(appr("TRYOUT") if tryout_rules else appr("FOREMAN"), "tryout" if tryout_rules else "foreman")
    elif event == "tryout_accept":
        add(appr("QC"), "qc")
    elif event in ("tryout_return", "qc_ng"):
        add(leaders, "die_leader")
        add(techs, "technician")
        if event == "qc_ng":
            add([requester], "requester", act=False)
    elif event == "qc_ok":
        add([requester], "requester", act=False)
        add(leaders, "die_leader", act=False)
    elif event == "cancel":
        add(leaders, "die_leader", act=False)
        add(techs, "technician", act=False)
        add([requester], "requester", act=False)
    elif event == "reopen":
        add(appr("QC"), "qc")
        add(leaders, "die_leader", act=False)
    elif event == "spare_issued":
        add(leaders, "die_leader", act=False)
    # one entry per person (the strongest reason wins)
    seen = {}
    for u, why, act in out:
        if u.id not in seen or (act and not seen[u.id][2]):
            seen[u.id] = (u, why, act)
    return list(seen.values())


def email_config(company, db) -> dict:
    """Personal e-mails use the Brevo key from Admin › ช่องทางแจ้งเตือน (personal e-mail box),
    falling back to the first active e-mail channel."""
    s = company.settings
    cfg = {"api_key": s.get("brevo_api_key", ""), "sender_email": s.get("brevo_sender_email", ""),
           "sender_name": s.get("brevo_sender_name", "") or "Die Management System"}
    if not cfg["api_key"]:
        ch = (db.query(NotificationChannel).filter(NotificationChannel.company_id == company.id, NotificationChannel.type == "email",
                                                   NotificationChannel.is_active == True).first())
        if ch:
            c = ch.config
            cfg.update(api_key=c.get("api_key", ""), sender_email=c.get("sender_email", ""),
                       sender_name=c.get("sender_name") or cfg["sender_name"])
    cfg["enabled"] = bool(cfg["api_key"] and cfg["sender_email"] and s.get("personal_email", True))
    return cfg


def route_to_people(db, company, event, req, title, body, link, actor_id=None, reminder=False):
    people = [p for p in recipients(db, company, event, req, reminder=reminder) if p[0].id != actor_id]
    head = ACTION_TEXT.get(event, title)
    level = "danger" if event in ("qc_ng", "tryout_return") else ("success" if event == "qc_ok" else ("warn" if any(a for _, _, a in people) else "info"))
    for u, why, act in people:
        db.add(Notification(company_id=company.id, user_id=u.id, title=f"[{company.code}] {req.dr_no} · {head}",
                            body=(("⚡ ต้องดำเนินการ · " if act else "") + body)[:400], link=link, level=level))
    cfg = email_config(company, db)
    if not cfg["enabled"]:
        return 0
    mails, used = [], set()
    for u, why, act in people:
        addr = (u.email or "").strip().lower()
        if not addr or "@" not in addr or addr in used:
            continue
        if not act and event not in ("qc_ok", "foreman_reject", "leader_reject", "qc_ng", "cancel"):
            continue            # FYI-only steps stay in the app bell; e-mail is for things the person must do / must know
        used.add(addr)
        mails.append((addr, u.full_name, WHY.get(why, ""), act))
    if mails:
        url = (link if link.startswith("http") else settings.BASE_URL + link)
        subject_base = f"[{company.code}] {head} • {req.dr_no} • {req.part_no} {req.die_process or ''}"
        rows = [("เลขที่ใบงาน", req.dr_no), ("ประเภท", req.request_type), ("ความเร่งด่วน", ("🔥 ด่วน " if req.is_emergency else "") + f"{req.required_day} วัน"),
                ("ลูกค้า / Model", f"{req.customer or '-'} / {req.model or '-'}"), ("Part No.", req.part_no), ("Part Name", req.part_name or "-"),
                ("Die Process", req.die_process or "-"), ("Line", req.line or "-"), ("ปัญหา", req.problem_text or "-"),
                ("ผู้แจ้ง", req.requester_name or "-"), ("ช่าง", req.technician_names or "-"),
                ("กำหนดเสร็จ", req.target_date.strftime("%d-%m-%Y") if req.target_date else "-")]
        threading.Thread(target=_send_personal, args=(cfg, mails, subject_base, head, rows, url, company.code), daemon=True).start()
    return len(mails)


def _send_personal(cfg, mails, subject_base, head, rows, url, code):
    import html as _h
    table = "".join(f"<tr><td style='padding:6px 10px;color:#6b7686;white-space:nowrap'>{_h.escape(k)}</td>"
                    f"<td style='padding:6px 10px;font-weight:600'>{_h.escape(str(v))}</td></tr>" for k, v in rows)
    with httpx.Client(timeout=15) as c:
        for addr, name, why, act in mails:
            btn = "เปิดใบงานเพื่อดำเนินการ" if act else "เปิดดูใบงาน"
            html = f"""<div style="font-family:Tahoma,Sarabun,sans-serif;max-width:620px;margin:auto;border:1px solid #d9e0ea;border-radius:12px;overflow:hidden">
<div style="background:#0f2c5c;color:#fff;padding:14px 18px"><b style="font-size:18px">AAPICO {code} · Die Management</b></div>
<div style="padding:16px 18px"><p style="margin:0 0 6px">เรียน คุณ{_h.escape(name)}</p>
<h2 style="margin:6px 0 4px;color:{'#d13a3a' if act else '#1f5fbf'}">{_h.escape(head)}</h2>
<p style="margin:0 0 12px;color:#6b7686;font-size:13px">ส่งถึงคุณเพราะ{_h.escape(why)}</p>
<table style="border-collapse:collapse;width:100%;background:#f3f6fb;border-radius:8px">{table}</table>
<p style="text-align:center;margin:18px 0"><a href="{url}" style="background:#1f5fbf;color:#fff;padding:12px 26px;border-radius:10px;text-decoration:none;font-weight:700">{btn}</a></p>
<p style="color:#6b7686;font-size:12px">ลิงก์นี้ต้องเข้าสู่ระบบด้วยบัญชีของคุณ และเปิดได้เฉพาะผู้ที่มีสิทธิ์ในใบงานนี้เท่านั้น — กรุณาอย่าส่งต่ออีเมลนี้</p></div></div>"""
            try:
                r = c.post("https://api.brevo.com/v3/smtp/email", headers={"api-key": cfg["api_key"]},
                           json={"sender": {"email": cfg["sender_email"], "name": cfg["sender_name"]},
                                 "to": [{"email": addr, "name": name}], "subject": subject_base[:200], "htmlContent": html})
                if r.status_code >= 300:
                    print(f"[mail] {addr} -> {r.status_code} {r.text[:120]}")
            except Exception as e:
                print(f"[mail] {addr} failed: {e}")
