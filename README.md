# Die Management System (Die Repair + PM Die) — AAPICO

เว็บแอประบบแจ้งซ่อม / PM แม่พิมพ์ แทน Power Apps "Die Maintenance Request" รองรับหลายบริษัท (AH / AHP / AHR / AHD / AL …) ข้อมูลแยกกันสิ้นเชิง
Workflow: **Production แจ้ง → Foreman/Eng อนุมัติ → หัวหน้า Die อนุมัติ+มอบหมายช่าง → ช่างรับงาน/ซ่อม (จับเวลาหักพัก) → Production Try-out → QC OK ปิด / NG ส่งกลับ**

Stack: Python 3.12 · FastAPI · SQLAlchemy (SQLite หรือ Postgres) · Jinja2 · Chart.js — ไม่มี build step, ไฟล์เดียวรันได้

---
## 1. รันบนเครื่องตัวเอง (ทดลอง 2 นาที)
**Windows: ดับเบิลคลิก `run.bat`** — ครั้งแรกจะ (1) สร้าง .venv (2) ติดตั้งแพ็กเกจ (3) seed ข้อมูล ~1-3 นาที **อย่าปิดหน้าต่างจนกว่าจะขึ้น DONE** แล้วเบราว์เซอร์เปิดเอง
ถ้าเคยปิดกลางคัน / login `ah_admin` ไม่ได้ → รัน `reset_data.bat` (ลบ data/ แล้ว seed ใหม่) · ข้อความ `Cache entry deserialization failed` ของ pip ไม่ใช่ error (แก้ให้ไม่ขึ้นแล้ว)
```bash
# Linux/Mac: ./run.sh
# หรือ
pip install -r requirements.txt
python scripts/seed_ah.py --with-history      # สร้างบริษัท AH/AHP/AHR + ทะเบียนแม่พิมพ์ 2,773 ตัว + ประวัติ 2,919 งานจาก Power Apps
uvicorn app.main:app --port 8790
```
เปิด http://localhost:8790

| บทบาท | Username | Password |
|---|---|---|
| Super admin (ทุกบริษัท) | `admin` | `admin1234` |
| Admin AH | `ah_admin` | `1234` |
| Manager | `ah_manager` | `1234` |
| Production (แจ้งซ่อม / Try-out) | `ah_prod` | `1234` |
| Foreman/Eng | `ah_foreman` | `1234` |
| หัวหน้า Die | `ah_leader` | `1234` |
| ช่าง | `ah_tech1`, `ah_tech2` | `1234` |
| QC | `ah_qc` | `1234` |

ช่าง/Foreman/QC ตัวจริงจาก CSV ถูกสร้างเป็นผู้ใช้แล้ว (รหัส `1234`) — เปลี่ยนที่ Admin › ผู้ใช้
ทดสอบระบบทั้งหมด: `python scripts/smoke_test.py` (70+ เช็ค ครบทุก workflow)

---
## 2. โครงสร้างโปรเจกต์
```
app/
  main.py            เริ่มแอป, /media, งานตรวจรายชั่วโมง (เกินกำหนด / PM due / พื้นที่รูป)
  config.py          ตั้งค่าจาก ENV ทั้งหมด
  models.py          ตาราง: companies, users, dies, requests, assignments, work_logs, attachments, part_usages, ...
  workflow.py        State machine — ทุกการเปลี่ยนสถานะผ่านที่นี่ (สิทธิ์ + history + เวลา)
  auth.py / i18n.py  Login/สิทธิ์ · พจนานุกรม TH/EN
  routers/           auth, requests, pm, dashboard(+TV), reports(+anomaly), admin, api(v1 สำหรับโปรแกรมอะไหล่)
  services/          kpi (MTTR/MTBF/หักเวลาพัก), images (ย่อ/thumbnail/archive), notify (LINE/Telegram/Discord/Brevo/Webhook),
                     export (Excel/CSV/PDF), backup, import_data (Power Apps CSV / Template)
  templates/, static/
scripts/
  seed_ah.py              seed จาก CSV ของ AH    smoke_test.py   ทดสอบ end-to-end
  spare_part_client_example.py   โค้ดตัวอย่างใส่ในโปรแกรม Die Spare Part
  powerapps_csv/          Master lists + DieRepairList.csv ที่ใช้ seed
data/                   die_management.db · uploads/ · backups/   (ไม่ commit)
```

---
## 3. Deploy ขึ้น Host ฟรี (แนะนำ)

ปัญหาของ host ฟรีคือ **ดิสก์ไม่ถาวร** และ **หลับเมื่อไม่มีคนใช้** โครงสร้างนี้แยก 3 ชั้นเพื่อให้ใช้ยาวๆ ได้ฟรี:

| ชั้น | บริการฟรีที่แนะนำ | ทำไม |
|---|---|---|
| แอป | **Render Free** (หรือ Koyeb / Fly.io) | Python ตรงๆ, deploy จาก GitHub, มี `render.yaml` ให้แล้ว |
| ฐานข้อมูล | **Neon Postgres Free** (0.5 GB) หรือ Supabase | ข้อมูลไม่หายตอน redeploy; ตัวหนังสือ 3,000 งาน/ปี ≈ 10 MB |
| รูปภาพ | **Cloudflare R2** (10 GB ฟรี, ไม่มีค่า egress) | รูปอยู่นอก DB และนอกดิสก์แอป |

### ขั้นตอน
1. Push โฟลเดอร์นี้ขึ้น GitHub (private)
2. **Neon**: สร้าง project → copy connection string (`postgresql://...sslmode=require`)
3. **Cloudflare R2**: สร้าง bucket `dms-images` → สร้าง API token (Object Read & Write) → เปิด Public access (หรือ custom domain) ได้ URL สาธารณะ
4. **Render**: New → Blueprint → เลือก repo → ใส่ ENV:
   - `DATABASE_URL` = จาก Neon · `BASE_URL` = `https://<app>.onrender.com` · `ADMIN_PASSWORD`
   - `S3_ENDPOINT` = `https://<account>.r2.cloudflarestorage.com` · `S3_BUCKET` · `S3_ACCESS_KEY` · `S3_SECRET_KEY` · `S3_PUBLIC_URL`
5. Deploy เสร็จ → login `admin` → Admin › บริษัท สร้าง AH → Admin › นำเข้าข้อมูล: อัปโหลด `DieProductionProcessList.csv` แล้ว `DieRepairList.csv`
   (หรือรัน `DATABASE_URL=... python scripts/seed_ah.py --with-history` จากเครื่องตัวเองครั้งเดียว)
6. กันแอปหลับ (Render free หลับหลัง 15 นาที): ตั้ง **cron-job.org** (ฟรี) ยิง `GET https://<app>.onrender.com/healthz` ทุก 10 นาที
7. Backup: Admin › Backup สร้าง ZIP แล้วดาวน์โหลดทุกสัปดาห์ (Neon มี point-in-time restore 7 วันอยู่แล้ว)

**ทางเลือกในโรงงาน**: รันบน mini PC/NAS ด้วย `run.bat` + SQLite แล้วเปิดออกเน็ตด้วย **Cloudflare Tunnel** (ฟรี, มี domain เอง, ไม่หลับ, รูปอยู่บนดิสก์จริง) — เหมาะที่สุดถ้ามีเครื่องเปิดทิ้งไว้ได้

### ไม่ให้ฐานข้อมูล/พื้นที่เต็มจากรูป
- ย่อทุกรูปเหลือ 1,600 px JPEG 78% (~200 KB) + thumbnail 360 px; มือถือย่อก่อนส่งอีกชั้น
- รูปไม่เก็บใน DB (เก็บ path เท่านั้น) → DB โตช้ามาก
- Admin › รูปภาพ: ดูการใช้ตามเดือน, **Export ZIP เพื่อ backup → Archive** ลบต้นฉบับของงานที่ปิดเกิน N วัน (คง thumbnail)
- เกินเพดาน (ค่าเริ่มต้น 400 MB) → แจ้งเตือน Admin/Manager อัตโนมัติ

---
## 4. เชื่อมกับโปรแกรม Die Spare Part Inventory
ดู `scripts/spare_part_client_example.py`
- **โปรแกรมอะไหล่ → DMS**: `POST /api/v1/spare-issue` (header `X-API-Key`) ตอนยืนยันใบเบิกโดยอ้าง DR No. → ต้นทุนผูกเข้าใบงาน/แม่พิมพ์อัตโนมัติ และแจ้งหัวหน้า Die
  `POST /api/v1/low-stock` ส่งรายการต่ำกว่า Min → แจ้งเตือนทุกช่องทาง · `GET /api/v1/requests/open` ให้ dropdown DR ในหน้าเบิก
- **DMS → โปรแกรมอะไหล่**: หน้า "สแกน QR" สแกน Part ID แล้วเรียก `GET {spare_app_url}/api/parts/{code}` เพื่อกรอกชื่อ/ราคาให้ (ตั้ง URL+Key ที่ Admin › ตั้งค่า)
- QR ใบงาน = `{BASE_URL}/r/{DR No}` (รองรับ AHMDxxxxx เดิม) · เอกสาร API: `/api/docs`

---
## 5. แผน PM
ตั้ง **รอบ PM (วัน)** + วันที่ PM ล่าสุด ให้แม่พิมพ์ที่ "ทะเบียนแม่พิมพ์" (หรือคอลัมน์ `PM Interval Days` ตอนนำเข้า) → หน้า "แผน PM" แสดงเกินกำหนด/ถึงกำหนด, สร้างใบงาน PM ทีละตัวหรือทั้งหมด, แจ้งเตือนอัตโนมัติ, เมื่อ QC OK ระบบเลื่อนวัน PM ถัดไปให้ · PM ข้ามขั้น Foreman ได้ (ตั้งค่าบริษัท)

## 6. KPI ที่คำนวณ
- **เวลาซ่อมสุทธิ**: ช่างกด รับงาน/พัก/ทำต่อ/เสร็จ → หักช่วงพักของโรงงานอัตโนมัติ (10:00-10:10, 12:00-13:00, 15:00-15:10, OT 17:00-17:30, 22:00-22:10, 00:00-01:00, 03:00-03:10, OT 05:00-05:30 — แก้ได้ที่ตั้งค่า)
- **MTTR** = เฉลี่ยเวลาซ่อมสุทธิ · **MTBF** = วันเฉลี่ยระหว่างการเสียของแม่พิมพ์เดียวกัน · **PM Achievement** = PM ปิดทันกำหนด/PM ทั้งหมด
- **Lead time แต่ละขั้น** (median) หาคอขวด: แจ้ง→Foreman→หัวหน้า→ช่างรับ→เสร็จ→Try-out→QC · Downtime เครื่อง · ต้นทุนอะไหล่/แม่พิมพ์
- ข้อมูลเก่า: ระบบอ่าน "20.00-22.00" ท้ายช่อง HowToFix ของ Power Apps เป็นเวลาซ่อมจริง → MTTR ย้อนหลังใช้ได้

## 7. การแจ้งเตือน
Admin › ช่องทางแจ้งเตือน: **LINE Messaging API** (รูปได้), **Telegram** (รูป+ไฟล์), **Discord** (รูป+ไฟล์), **Email ผ่าน Brevo** (แนบไฟล์), **Webhook JSON** — เลือกเหตุการณ์ต่อช่องทาง, ปุ่มทดสอบ · ในแอปมีกระดิ่งแจ้งเตือนตามบทบาทเสมอ
เหตุการณ์: ใบงานใหม่ / งานด่วน / อนุมัติ / มอบหมาย / รับงาน / ซ่อมเสร็จ / Try-out / QC OK-NG / เกินกำหนด / PM due / อะไหล่ต่ำ Min / เบิกอะไหล่ / พื้นที่รูปใกล้เต็ม

---
## 8. Admin, Backup, ล้างใบงานเก่า, ย้ายเครื่อง
ดู `docs/คู่มือติดตั้งและใช้งาน.md` ข้อ F (ล้างใบงานเก่า — เก็บ master ครบ, Backup อัตโนมัติก่อนลบ), G (เครื่องมือ Admin ทั้งหมด), H (checklist host ฟรี + ย้ายข้อมูล SQLite ↔ Postgres)

ทดสอบอัตโนมัติ (รันได้ทั้ง SQLite และ Postgres): `smoke_test.py` → `test_admin_tools.py` → `test_freehost.py` และ `test_disaster_recovery.py <server เดิม> <server ว่าง>`

## 8.1 v1.5
- จอ TV ใหม่: ปรับขนาดตามจอ, เลื่อนอัตโนมัติ/ล้อเมาส์/ปุ่ม, รูปและชื่อทีมช่าง, รอใคร, ค้างที่ผู้อนุมัติ
- 🚀 เริ่มต้นบริษัท: Template Excel 10 ชีต + ตรวจไฟล์ก่อนนำเข้า + เช็กความพร้อมก่อนใช้งานจริง
- 🧭 ผังผู้อนุมัติแยกบริษัท/Line/ขั้นตอน + ผู้อนุมัติเห็นเฉพาะของตัวเอง
- คู่มือหัวข้อ N, O, P

## 8.2 v1.6
- ระบบ PM เต็มรูปแบบ: แผนตามวัน/Stroke, Checklist, ปฏิทิน, แผนประจำปี (Excel), แนะนำรอบจาก MTBF, เลื่อน PM, เปิดใบงานอัตโนมัติ, API stroke
- แก้จุดจากการตรวจทั้งระบบ (สิทธิ์, workflow, restore, import, PDF, SECRET_KEY) — คู่มือหัวข้อ R, S

## 9. Checklist ข้อกำหนด 31 ข้อ
| # | ข้อกำหนด | อยู่ที่ |
|---|---|---|
| 1 | ปุ่มภาษาไทย/อังกฤษ | โปรไฟล์ (จำต่อผู้ใช้) + สลับที่แถบบน |
| 2 | เวลาพัก 2 กะ | ตั้งค่าบริษัท › กะ & เวลาพัก → หักใน MTTR อัตโนมัติ |
| 3 | หลายบริษัท แยกบริหาร | Admin › บริษัท; ทุกตารางมี company_id; Superadmin สลับบริษัท |
| 4 | Logo AAPICO มุมซ้ายบน | Sidebar (แก้ข้อความได้ต่อบริษัท) |
| 5, 19 | Dashboard ผู้บริหาร Real-time บน TV | `/tv` รีเฟรช 30 วิ + `/dashboard` กราฟแนวโน้ม |
| 6 | หน้าสรุปข้อมูลผิดปกติ/Cleaning/เตือน part-line | `/reports/anomaly` (12 กฎ) |
| 7, 18 | โครงสร้างขึ้น host ฟรี + แนะนำติดตั้ง + หลังบ้าน backup | ข้อ 3 ข้างบน, render.yaml, Admin › Backup/Storage |
| 8, 20 | รายงาน + Export รูป/Excel/CSV/PDF | `/reports` (Excel มี 7 ชีต), ปุ่มพิมพ์/บันทึกภาพ |
| 9 | เชื่อมโปรแกรม Die Spare Part ด้วย QR | ข้อ 4 + หน้าสแกน |
| 10, 17 | Brevo email + LINE/Telegram/Discord/Webhook แนบรูป/ไฟล์ | Admin › ช่องทางแจ้งเตือน |
| 11 | วันที่ DD-MM-YYYY | ทุกหน้า (filter `d`/`dt`) |
| 12 | UI สวย เน้นจุดสำคัญ | ธีม 6 แบบ + สีหลักเลือกเอง, การ์ดงานด่วน/เกินกำหนดเน้นสี |
| 13 | Login แยกคน แยกสิทธิ์เมนู | 9 บทบาท, สิทธิ์บังคับทั้งที่ UI และ workflow.py |
| 14 | ผู้ใช้เปลี่ยน Theme/สี | โปรไฟล์ |
| 15 | Admin ตั้งค่าเอง | Admin › ตั้งค่า / Master / ผู้ใช้ / ช่องทาง / API key |
| 16 | Responsive มือถือ | Bottom nav, การ์ดแทนตาราง, กล้องถ่ายรูปตรง |
| 21 | Template กรอกข้อมูลย้อนหลัง | Admin › นำเข้า › Template Excel + รับ CSV Power Apps ตรง |
| 22 | แก้ไขข้อมูลผิด | ปุ่มแก้ไขในใบงาน (บันทึก Audit ว่าแก้อะไร) + Master rename ทั้งระบบ |
| 23 | คำนวณถูกต้อง Real-time | services/kpi.py คำนวณสดทุกครั้ง ไม่ cache |
| 24 | แนบรูป + จัดการไม่ให้ DB เต็ม + เตือน backup | ย่อรูป/thumbnail/archive/เตือนเพดาน |
| 25, 26 | กรอกน้อย + สแกน | Cascade ลูกค้า→Model→Part→Process, ค้นหาเร็ว, QR ใบงาน/แม่พิมพ์/อะไหล่, auto-fill จาก user |
| 27 | บทบาท หน้างาน/leader/foreman/supervisor/engineer/manager | production, foreman(=eng), die_leader, technician, qc, manager, admin |
| 28, 29 | วิเคราะห์ย้อนหลัง กราฟหลากหลาย performance/แนวโน้ม | Dashboard 8 กราฟ + รายงาน + หน้าแม่พิมพ์รายตัว (MTBF/MTTR/ต้นทุน) |
| 30 | แทน Power Apps (2000 แถว/ช้า/ไม่มี PM) | ไม่จำกัดแถว, แบ่งหน้า, PM ในตัว |
| 31 | ศึกษากระบวนการ | สถานะ 13 แบบตรงกับ flow จริง + Reject/Return ทุกจุด |
