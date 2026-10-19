"""Backups — one ZIP containing database.json (every table, portable between SQLite and Postgres) and,
optionally, the images.

Where backups are kept:
  * S3/R2 configured  -> bucket prefix  backups/<COMPANY>/   (survives restarts of a free host)  + local copy
  * otherwise          -> data/backups/                       (download it! free-host disks are wiped)
They can also be pushed to Telegram / Discord / e-mail channels that subscribe to the "backup" event."""
import io
import json
import zipfile
from datetime import date, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import BACKUP_DIR, UPLOAD_DIR
from ..database import Base, engine, now_local
from . import images

# tables whose rows belong to a company only through their parent job
CHILD_OF_REQUEST = ("assignments", "status_history", "progress_logs", "work_logs", "pm_check_results")


def _ser(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def dump_tables(db: Session, company_id: int = None) -> dict:
    out = {}
    req_ids = None
    if company_id:
        req_ids = [r[0] for r in db.execute(text("SELECT id FROM requests WHERE company_id = :c"), {"c": company_id})]
    for table in Base.metadata.sorted_tables:
        cols = [c.name for c in table.columns]
        q = db.query(table)
        if company_id:
            if table.name == "companies":
                q = q.filter(table.c.id == company_id)
            elif "company_id" in cols:
                q = q.filter(table.c.company_id == company_id)
            elif table.name == "pm_template_items":
                tids = [r[0] for r in db.execute(text("SELECT id FROM pm_templates WHERE company_id = :c"), {"c": company_id})]
                if not tids:
                    out[table.name] = []
                    continue
                q = q.filter(table.c.template_id.in_(tids))
            elif table.name in CHILD_OF_REQUEST:
                if not req_ids:
                    out[table.name] = []
                    continue
                q = q.filter(table.c.request_id.in_(req_ids))
        out[table.name] = [{c: _ser(getattr(row, c)) for c in cols} for row in q.all()]
    return out


def _zip_bytes(db, company_id, include_images) -> bytes:
    data = dump_tables(db, company_id)
    meta = {"app": "Die Management System", "created": now_local().isoformat(timespec="seconds"),
            "company_id": company_id, "counts": {k: len(v) for k, v in data.items()}}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("database.json", json.dumps(data, ensure_ascii=False))
        z.writestr("meta.json", json.dumps(meta, ensure_ascii=False, indent=1))
        z.writestr("README.txt", "Die Management System backup\n"
                                 "Restore: Admin > Backup > Restore (superadmin). Works on SQLite and Postgres.\n"
                                 "images/ folder (if present) holds the photos.\n")
        keys = []
        if include_images:
            from ..models import Attachment
            q = db.query(Attachment).filter(Attachment.is_archived == False)
            if company_id:
                q = q.filter(Attachment.company_id == company_id)
            keys = [k for att in q.all() for k in (att.path, att.thumb_path)]
        # staff photos are tiny (~20 KB) -> always inside every backup, so a wiped free-host disk loses nothing
        from ..models import User
        uq = db.query(User.photo_path).filter(User.photo_path != None)
        if company_id:
            uq = uq.filter(User.company_id == company_id)
        keys += [r[0] for r in uq]
        for key in keys:
            try:
                z.writestr(f"images/{key}", images.read_bytes(key))
            except Exception:
                pass
    return buf.getvalue()


def make_backup(db: Session, company_id: int = None, include_images: bool = False, auto: bool = False,
                company_code: str = "ALL") -> str:
    stamp = now_local().strftime("%Y%m%d_%H%M%S")
    name = f"dms_{'auto' if auto else 'backup'}_{company_code}_{stamp}.zip"
    blob = _zip_bytes(db, company_id, include_images)
    (BACKUP_DIR / name).write_bytes(blob)
    if images.using_s3():
        try:
            images.put_object(f"backups/{company_code}/{name}", blob)
        except Exception as e:
            print(f"[backup] upload to bucket failed: {e}")
    return name


def get_backup_bytes(name: str, company_code: str) -> bytes:
    p = BACKUP_DIR / name
    if p.exists():
        return p.read_bytes()
    if images.using_s3():
        return images.read_bytes(f"backups/{company_code}/{name}")
    raise FileNotFoundError(name)


def list_backups(company_code: str = None):
    """Local files + (if configured) the bucket, newest first. Each item: name, mb, at, where."""
    items = {}
    for p in BACKUP_DIR.glob("*.zip"):
        if company_code and f"_{company_code}_" not in p.name:
            continue
        items[p.name] = {"name": p.name, "mb": round(p.stat().st_size / 1048576, 2), "where": "server",
                         "ts": p.stat().st_mtime}
    if images.using_s3() and company_code:
        try:
            for o in images.list_objects(f"backups/{company_code}/"):
                n = o["Key"].rsplit("/", 1)[-1]
                it = items.setdefault(n, {"name": n, "mb": round(o["Size"] / 1048576, 2), "ts": o["LastModified"].timestamp(), "where": ""})
                it["where"] = (it["where"] + " + bucket").strip(" +") if it["where"] else "bucket"
        except Exception as e:
            print(f"[backup] list bucket failed: {e}")
    out = sorted(items.values(), key=lambda x: x["ts"], reverse=True)
    for it in out:
        it["at"] = datetime.fromtimestamp(it["ts"]).strftime("%d-%m-%Y %H:%M")
    return out


def delete_backup(name: str, company_code: str):
    (BACKUP_DIR / name).unlink(missing_ok=True)
    if images.using_s3():
        images.delete_object(f"backups/{company_code}/{name}")


def prune_auto_backups(company_code: str, keep: int):
    autos = [b for b in list_backups(company_code) if b["name"].startswith(f"dms_auto_{company_code}_")]
    for b in autos[keep:]:
        delete_backup(b["name"], company_code)
    return max(0, len(autos) - keep)


def last_backup_age_hours(company_code: str):
    items = list_backups(company_code)
    if not items:
        return None
    return round((datetime.now().timestamp() - items[0]["ts"]) / 3600, 1)


def _insert_rows(conn, table, rows) -> tuple:
    """Fast batch insert; if the batch hits a unique/foreign-key conflict, fall back to row-by-row with a
    SAVEPOINT each so one bad row (e.g. username 'admin' already exists) never aborts the whole restore."""
    ok = skipped = 0
    for i in range(0, len(rows), 500):
        batch = rows[i:i + 500]
        sp = conn.begin_nested()
        try:
            conn.execute(table.insert(), batch)
            sp.commit()
            ok += len(batch)
            continue
        except Exception:
            sp.rollback()
        for r in batch:
            sp = conn.begin_nested()
            try:
                conn.execute(table.insert(), [r])
                sp.commit()
                ok += 1
            except Exception:
                sp.rollback()
                skipped += 1
    return ok, skipped


def _update_rows(conn, table, rows) -> tuple:
    """Overwrite existing rows IN PLACE (UPDATE, never DELETE) so child rows that point to them stay valid."""
    ok = skipped = 0
    for r in rows:
        vals = {k: v for k, v in r.items() if k != "id"}
        if not vals:
            continue
        sp = conn.begin_nested()
        try:
            conn.execute(table.update().where(table.c.id == r["id"]).values(**vals))
            sp.commit()
            ok += 1
        except Exception:
            sp.rollback()
            skipped += 1
    return ok, skipped


IDENTITY = {"requests": ("company_id", "dr_no"), "dies": ("company_id", "part_no", "die_process"),
            "pm_templates": ("company_id", "name")}


def _different(conn, table, rows) -> set:
    """ids whose row in the database is a DIFFERENT record than in the backup (same id reused, e.g. a new ticket
    created after a purge). Only for tables with a natural identity; children of those ids are then not restored."""
    keys = IDENTITY.get(table.name)
    if not keys:
        return set()
    byid = {r["id"]: r for r in rows}
    out, ids = set(), list(byid)
    for i in range(0, len(ids), 500):
        for cur in conn.execute(table.select().where(table.c.id.in_(ids[i:i + 500]))):
            m = cur._mapping
            b = byid[m["id"]]
            if any(str(b.get(k)) != str(m[k]) for k in keys):
                out.add(m["id"])
    return out


def restore_backup(db: Session, zip_bytes: bytes, replace: bool = False) -> dict:
    """Restore a backup zip. New rows are inserted; rows whose id already exists are skipped, or updated in place
    when replace=True. Works on an empty database (disaster recovery / moving host) and on a live one (merge),
    and across engines (backup from Postgres -> restore on SQLite and vice-versa)."""
    z = zipfile.ZipFile(io.BytesIO(zip_bytes))
    data = json.loads(z.read("database.json"))
    counts, skipped_total = {}, 0
    conflicts = {}          # table -> ids whose existing row is a DIFFERENT record (e.g. ids reused after a purge)
    conn = db.connection()
    for table in Base.metadata.sorted_tables:          # parents before children
        rows = data.get(table.name) or []
        if not rows:
            continue
        # child rows pointing at a parent that clashed with a different record must not be attached to it
        fk_cols = [(c.name, fk.column.table.name) for c in table.columns for fk in c.foreign_keys]
        bad_parents = [(col, conflicts[parent]) for col, parent in fk_cols if conflicts.get(parent)]
        if bad_parents:
            before = len(rows)
            rows = [r for r in rows if not any(r.get(col) in ids_ for col, ids_ in bad_parents)]
            skipped_total += before - len(rows)
            if not rows:
                continue
        coltypes = {c.name: str(c.type).upper() for c in table.columns}
        clean_rows = []
        for row in rows:
            clean = {}
            for k, v in row.items():
                if k not in coltypes:
                    continue        # column removed in this version -> ignore
                t = coltypes[k]
                if v and isinstance(v, str) and (t.startswith("DATETIME") or t.startswith("TIMESTAMP")):
                    v = datetime.fromisoformat(v)
                elif v and isinstance(v, str) and t.startswith("DATE"):
                    v = date.fromisoformat(v[:10])
                clean[k] = v
            clean_rows.append(clean)
        ids = [r["id"] for r in clean_rows if r.get("id") is not None]
        existing = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            existing |= {r[0] for r in conn.execute(table.select().with_only_columns(table.c.id).where(table.c.id.in_(chunk)))}
        new_rows = [r for r in clean_rows if r.get("id") not in existing]
        if existing and not replace:
            conflicts[table.name] = _different(conn, table, [r for r in clean_rows if r.get("id") in existing])
        upd, sk2 = (0, 0)
        if replace and existing:
            # update first: frees unique values (e.g. username 'admin' of a fresh install) before the inserts
            upd, sk2 = _update_rows(conn, table, [r for r in clean_rows if r.get("id") in existing])
        ins, sk1 = _insert_rows(conn, table, new_rows)
        counts[table.name] = ins + upd
        skipped_total += sk1 + sk2
    db.commit()
    if skipped_total:
        counts["skipped_conflicts"] = skipped_total
    n_conf = sum(len(v) for v in conflicts.values())
    if n_conf:
        counts["conflicting_records"] = n_conf      # same id, different record: kept the current one, its children were not mixed in
    from ..auth import ensure_default_admin
    ensure_default_admin(db)          # never leave the system without a super admin
    if engine.dialect.name == "postgresql":     # move id sequences past the restored ids
        for table in Base.metadata.sorted_tables:
            if "id" in table.columns:
                db.execute(text(f"SELECT setval(pg_get_serial_sequence('{table.name}','id'), "
                                f"GREATEST(COALESCE((SELECT MAX(id) FROM {table.name}), 1), 1))"))
        db.commit()
    n_img = 0
    for info in z.infolist():
        if info.filename.startswith("images/") and not info.is_dir():
            key = info.filename[len("images/"):]
            try:
                if images.using_s3():
                    images.put_object(key, z.read(info), "image/jpeg")
                else:
                    target = UPLOAD_DIR / key
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(z.read(info))
                n_img += 1
            except Exception:
                pass
    if n_img:
        counts["images"] = n_img
    return counts
