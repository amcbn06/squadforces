import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./squadforces.db")

if DATABASE_URL.startswith("sqlite"):
    connect_args = {"check_same_thread": False, "timeout": 30}
else:
    connect_args = {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def ensure_columns():
    """Additive schema upgrade: create_all skips existing tables, so add columns introduced later.
    Only ever adds a missing column; never drops or rewrites anything."""
    from sqlalchemy import inspect, text
    additions = {
        "groups": {"hints_allowed": "BOOLEAN NOT NULL DEFAULT {false}", "owner_id": "INTEGER", "max_members": "INTEGER"},
        "assignment_items": {"rating": "INTEGER", "source_url": "VARCHAR(500)"},
        "contest_problems": {"max_score": "INTEGER"},
        "users": {"session_version": "INTEGER NOT NULL DEFAULT 0"},
        "hints": {
            "kind": "VARCHAR(10) NOT NULL DEFAULT 'hint'",
            "author_id": "INTEGER",
            "time_minutes": "INTEGER",
        },
    }
    false = "0" if engine.dialect.name == "sqlite" else "false"
    insp = inspect(engine)
    with engine.begin() as conn:
        existing_by_table = {}
        for table, columns in additions.items():
            if not insp.has_table(table):
                continue  # create_all makes missing tables complete; nothing to add to
            existing = {c["name"] for c in insp.get_columns(table)}
            existing_by_table[table] = existing
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl.format(false=false)}"))
        # Older deploys stored solution/hint as a boolean on a since-removed column; fold it into kind.
        if "is_solution" in existing_by_table.get("hints", set()):
            conn.execute(text("UPDATE hints SET kind = 'solution' WHERE is_solution = 1"))


def migrate_groups():
    """One-time, idempotent data step for groups that predate ownership: the admin (id 0) owns them, and each gets
    a member limit (the default, or its current size if larger, so nobody is ever removed). Groups already
    migrated, and new ones, have both set and are left alone."""
    from sqlalchemy import text
    from app.limits import DEFAULT_GROUP_MEMBERS
    with engine.begin() as conn:
        conn.execute(text("UPDATE groups SET owner_id = 0 WHERE owner_id IS NULL"))
        conn.execute(text(
            "UPDATE groups SET max_members = CASE "
            "WHEN (SELECT COUNT(*) FROM group_memberships m WHERE m.group_id = groups.id) > :d "
            "THEN (SELECT COUNT(*) FROM group_memberships m WHERE m.group_id = groups.id) ELSE :d END "
            "WHERE max_members IS NULL"), {"d": DEFAULT_GROUP_MEMBERS})


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
