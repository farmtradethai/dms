from datetime import datetime, timedelta, timezone

from sqlalchemy import String, create_engine, event, inspect as sa_inspect
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

connect_args = {}
engine_kwargs = {"pool_pre_ping": True}
if settings.DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False}
else:
    engine_kwargs.update(pool_size=5, max_overflow=5, pool_recycle=280)

engine = create_engine(settings.DATABASE_URL, connect_args=connect_args, **engine_kwargs)

if settings.DATABASE_URL.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragma(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


@event.listens_for(Session, "before_flush")
def _fit_string_lengths(session, flush_context, instances):
    """SQLite ignores VARCHAR(n) but Postgres rejects the whole save when one text is too long
    (e.g. a long Part Name pasted from Excel). Trim every String column to its declared length."""
    for obj in list(session.new) + list(session.dirty):
        try:
            mapper = sa_inspect(obj).mapper
        except Exception:
            continue
        for col in mapper.columns:
            if isinstance(col.type, String) and col.type.length and col.key in obj.__dict__:
                v = obj.__dict__.get(col.key)
                if isinstance(v, str) and len(v) > col.type.length:
                    setattr(obj, col.key, v[:col.type.length])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def today_local():
    return now_local().date()


def now_local() -> datetime:
    """Naive local (factory) time. The whole app stores naive local timestamps on purpose:
    shift / break calculations are defined in factory wall-clock time."""
    return (datetime.now(timezone.utc) + timedelta(hours=settings.TZ_OFFSET_HOURS)).replace(tzinfo=None)
