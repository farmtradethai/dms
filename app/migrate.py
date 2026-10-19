"""Tiny schema migrator — runs at every start-up.

`Base.metadata.create_all()` creates missing *tables* but never touches existing ones, so a new column
added in a later version would crash an existing database. This adds any missing column with ALTER TABLE
(works on SQLite and Postgres), so upgrading = replace the code and restart. Data is never dropped."""
from sqlalchemy import Boolean, DateTime, Date, Float, Integer, String, Text, inspect, text

from .database import Base, engine

APP_VERSION = "1.6.1"


def _sql_type(col, dialect):
    t = col.type
    if isinstance(t, Boolean):
        return "BOOLEAN"
    if isinstance(t, Integer):
        return "INTEGER"
    if isinstance(t, Float):
        return "DOUBLE PRECISION" if dialect == "postgresql" else "REAL"
    if isinstance(t, DateTime):
        return "TIMESTAMP"
    if isinstance(t, Date):
        return "DATE"
    if isinstance(t, String):
        return f"VARCHAR({t.length or 255})"
    if isinstance(t, Text):
        return "TEXT"
    return "TEXT"


def _default_sql(col):
    d = col.default
    if d is None or not getattr(d, "is_scalar", False):
        return ""
    v = d.arg
    if isinstance(v, bool):
        return f" DEFAULT {'TRUE' if v else 'FALSE'}"
    if isinstance(v, (int, float)):
        return f" DEFAULT {v}"
    if isinstance(v, str):
        return " DEFAULT '" + v.replace("'", "''") + "'"
    return ""


def run() -> list:
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    dialect = engine.dialect.name
    added = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = f'ALTER TABLE {table.name} ADD COLUMN {col.name} {_sql_type(col, dialect)}{_default_sql(col)}'
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
    if added:
        print(f"[migrate] added columns: {', '.join(added)}")
    return added
