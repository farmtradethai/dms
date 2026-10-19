"""Image handling that keeps a free host alive:
 - every upload is re-encoded to JPEG, resized to IMAGE_MAX_SIDE and a small thumbnail is made
 - files live under data/uploads/<company>/<yyyy>/<mm>/ (or in an S3-compatible bucket when S3_* is set)
 - storage usage is tracked so Admin gets a warning before the disk / bucket fills up
 - `archive_old()` deletes full-size originals of closed jobs older than N days, keeps the thumbnail"""
import io
import shutil
import uuid
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageOps
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..config import UPLOAD_DIR, settings
from ..models import ST, Attachment, RepairRequest
from ..database import today_local

_s3 = None
try:  # optional: iPhone HEIC photos
    from pillow_heif import register_heif_opener
    register_heif_opener()
except Exception:
    pass


def _s3_client():
    global _s3
    if _s3 is None and settings.S3_ENDPOINT and settings.S3_BUCKET:
        import boto3  # optional dependency
        from botocore.config import Config
        # path-style + explicit region works for Cloudflare R2 (region "auto") and Supabase Storage (its project region)
        _s3 = boto3.client("s3", endpoint_url=settings.S3_ENDPOINT, region_name=settings.S3_REGION or "auto",
                           aws_access_key_id=settings.S3_ACCESS_KEY, aws_secret_access_key=settings.S3_SECRET_KEY,
                           config=Config(s3={"addressing_style": "path"}, signature_version="s3v4"))
    return _s3


def using_s3() -> bool:
    return bool(settings.S3_ENDPOINT and settings.S3_BUCKET)


def _encode(img: Image.Image, max_side: int, quality: int) -> bytes:
    img = ImageOps.exif_transpose(img)
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, optimize=True, progressive=True)
    return buf.getvalue()


def _put(key: str, data: bytes):
    if using_s3():
        _s3_client().put_object(Bucket=settings.S3_BUCKET, Key=key, Body=data, ContentType="image/jpeg")
    else:
        p = UPLOAD_DIR / key
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def _delete(key: str):
    if not key:
        return
    try:
        if using_s3():
            _s3_client().delete_object(Bucket=settings.S3_BUCKET, Key=key)
        else:
            (UPLOAD_DIR / key).unlink(missing_ok=True)
    except Exception:
        pass


def url_for(key: str) -> str:
    """URL used inside the app. With a public bucket URL images load straight from R2/Supabase;
    otherwise /media/... checks the login and redirects to a short-lived presigned URL (S3) or serves the file."""
    if not key:
        return ""
    if using_s3() and settings.S3_PUBLIC_URL:
        return f"{settings.S3_PUBLIC_URL.rstrip('/')}/{key}"
    return f"/media/{key}"


def presigned_url(key: str, seconds: int = 3600) -> str:
    return _s3_client().generate_presigned_url("get_object", Params={"Bucket": settings.S3_BUCKET, "Key": key},
                                               ExpiresIn=seconds)


def media_token(key: str) -> str:
    from itsdangerous import URLSafeTimedSerializer
    return URLSafeTimedSerializer(settings.SECRET_KEY, salt="dms-media").dumps(key)


def media_token_ok(key: str, token: str, max_age: int = 7 * 86400) -> bool:
    from itsdangerous import BadSignature, URLSafeTimedSerializer
    try:
        return URLSafeTimedSerializer(settings.SECRET_KEY, salt="dms-media").loads(token, max_age=max_age) == key
    except BadSignature:
        return False


def external_url(key: str) -> str:
    """Absolute URL that LINE / Telegram / e-mail can open WITHOUT logging in (valid 7 days)."""
    if not key:
        return ""
    if using_s3():
        return f"{settings.S3_PUBLIC_URL.rstrip('/')}/{key}" if settings.S3_PUBLIC_URL else presigned_url(key, 7 * 86400)
    return f"{settings.BASE_URL}/media/{key}?t={media_token(key)}"


def read_bytes(key: str) -> bytes:
    if using_s3():
        return _s3_client().get_object(Bucket=settings.S3_BUCKET, Key=key)["Body"].read()
    return (UPLOAD_DIR / key).read_bytes()


def save_upload(db: Session, company_code: str, company_id: int, request_id: int, kind: str,
                raw: bytes, uploaded_by: str, note: str = "") -> Attachment:
    if len(raw) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"File larger than {settings.MAX_UPLOAD_MB} MB")
    img = Image.open(io.BytesIO(raw))
    img.load()
    main = _encode(img, settings.IMAGE_MAX_SIDE, settings.IMAGE_QUALITY)
    thumb = _encode(Image.open(io.BytesIO(main)), settings.THUMB_SIDE, 70)
    w, h = Image.open(io.BytesIO(main)).size
    today = today_local()
    base = f"{company_code}/{today:%Y}/{today:%m}/{request_id}_{kind.lower()}_{uuid.uuid4().hex[:10]}"
    _put(base + ".jpg", main)
    _put(base + "_t.jpg", thumb)
    att = Attachment(company_id=company_id, request_id=request_id, kind=kind, path=base + ".jpg",
                     thumb_path=base + "_t.jpg", size_bytes=len(main) + len(thumb), width=w, height=h,
                     uploaded_by=uploaded_by, note=note[:200] if note else None)
    db.add(att)
    db.flush()
    return att


def save_staff_photo(company_code: str, user, raw: bytes) -> str:
    """Staff photo for the TV board / team cards: centre-cropped square 320 px JPEG (~20 KB)."""
    if len(raw) > settings.MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"File larger than {settings.MAX_UPLOAD_MB} MB")
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(raw)))
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    img = ImageOps.fit(img, (320, 320), method=Image.LANCZOS, centering=(0.5, 0.35))   # keep the face (upper part)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=82, optimize=True)
    key = f"{company_code}/staff/{user.id}_{uuid.uuid4().hex[:8]}.jpg"
    old = user.photo_path
    _put(key, buf.getvalue())
    user.photo_path = key
    if old and old != key:
        _delete(old)
    return key


def remove_staff_photo(user):
    if user.photo_path:
        _delete(user.photo_path)
    user.photo_path = None


def delete_attachment(db: Session, att: Attachment):
    _delete(att.path)
    _delete(att.thumb_path)
    db.delete(att)


def storage_stats(db: Session, company_id: int = None) -> dict:
    q = db.query(func.count(Attachment.id), func.coalesce(func.sum(Attachment.size_bytes), 0))
    if company_id:
        q = q.filter(Attachment.company_id == company_id)
    count, total = q.one()
    disk_free_mb = None
    if not using_s3():
        try:
            disk_free_mb = shutil.disk_usage(UPLOAD_DIR).free // (1024 * 1024)
        except Exception:
            pass
    mb = round(total / (1024 * 1024), 1)
    return {"count": count, "mb": mb, "warn_mb": settings.STORAGE_WARN_MB, "warn": mb >= settings.STORAGE_WARN_MB,
            "pct": min(100, round(100 * mb / max(settings.STORAGE_WARN_MB, 1))), "disk_free_mb": disk_free_mb,
            "backend": "S3/R2" if using_s3() else "Local disk"}


def archive_old(db: Session, company_id: int, older_than_days: int) -> int:
    """Removes full-size images of CLOSED/CANCELLED jobs older than N days (thumbnail stays)."""
    cutoff = today_local() - timedelta(days=older_than_days)
    q = (db.query(Attachment).join(RepairRequest, Attachment.request_id == RepairRequest.id)
         .filter(Attachment.company_id == company_id, Attachment.is_archived == False,
                 RepairRequest.status.in_([ST["CLOSED"], ST["CANCELLED"]]),
                 RepairRequest.closed_at != None))
    n = 0
    for att in q.all():
        if att.request.closed_at.date() <= cutoff:
            _delete(att.path)
            att.is_archived = True
            att.size_bytes = max(att.size_bytes - int(att.size_bytes * 0.85), 0)
            n += 1
    db.commit()
    return n


def export_images_zip(db: Session, company_id: int, out_path: Path, request_ids=None) -> int:
    """Zips original images so the supervisor can back them up before archiving."""
    import zipfile
    q = db.query(Attachment).filter(Attachment.company_id == company_id, Attachment.is_archived == False)
    if request_ids:
        q = q.filter(Attachment.request_id.in_(request_ids))
    n = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_STORED) as z:
        for att in q.all():
            try:
                if using_s3():
                    obj = _s3_client().get_object(Bucket=settings.S3_BUCKET, Key=att.path)
                    z.writestr(att.path, obj["Body"].read())
                else:
                    z.write(UPLOAD_DIR / att.path, att.path)
                n += 1
            except Exception:
                continue
    return n


# ---- generic object helpers (used for off-site backups when S3/R2 is configured)
def put_object(key: str, data: bytes, content_type="application/zip"):
    _s3_client().put_object(Bucket=settings.S3_BUCKET, Key=key, Body=data, ContentType=content_type)


def list_objects(prefix: str):
    out, token = [], None
    while True:
        kw = dict(Bucket=settings.S3_BUCKET, Prefix=prefix)
        if token:
            kw["ContinuationToken"] = token
        r = _s3_client().list_objects_v2(**kw)
        out += r.get("Contents", [])
        if not r.get("IsTruncated"):
            return out
        token = r.get("NextContinuationToken")


def delete_object(key: str):
    _delete(key)
