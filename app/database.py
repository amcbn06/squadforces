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
        "assignment_items": {"rating": "INTEGER"},
    }
    false = "0" if engine.dialect.name == "sqlite" else "false"
    insp = inspect(engine)
    with engine.begin() as conn:
        for table, columns in additions.items():
            existing = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns.items():
                if name not in existing:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl.format(false=false)}"))


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
