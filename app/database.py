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
        "groups": {"hints_allowed": "BOOLEAN NOT NULL DEFAULT {false}"},
        "assignment_items": {"rating": "INTEGER", "source_url": "VARCHAR(500)"},
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
            existing = {c["name"] for c in insp.get_columns(table)}
            existing_by_table[table] = existing
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl.format(false=false)}"))
        # Older deploys stored solution/hint as a boolean on a since-removed column; fold it into kind.
        if "is_solution" in existing_by_table.get("hints", set()):
            conn.execute(text("UPDATE hints SET kind = 'solution' WHERE is_solution = 1"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
