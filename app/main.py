import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, Depends
from fastapi.exceptions import HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

from app.database import engine, Base, get_db, SessionLocal
from app import models  # noqa: F401 — registers models with Base
from app.auth import hash_password, verify_password, require_auth
from app.routers import groups, assignments, recommend
from app.routers import admin as admin_router
from app import scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    from sqlalchemy import text
    Base.metadata.create_all(bind=engine)

    # Runtime migrations: add new columns to existing tables if missing
    _migrations = [
        ("problem_results", "solve_type", "VARCHAR(20)"),
        ("problem_results", "attempts", "INTEGER"),
        ("problem_results", "best_wrong_verdict", "VARCHAR(30)"),
        ("assignment_items", "created_by_id", "INTEGER"),
        ("users", "kilonova_handle", "VARCHAR(50)"),
    ]
    with engine.connect() as conn:
        for table, col, coldef in _migrations:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coldef}"))
                conn.commit()
            except Exception:
                pass  # column already exists

    # Seed admin account on first run
    db = SessionLocal()
    try:
        if db.query(models.Account).count() == 0:
            admin_password = os.getenv("ADMIN_PASSWORD", "squadforces2024")
            db.add(models.Account(
                username="admin",
                password_hash=hash_password(admin_password),
                role="admin",
            ))
            db.commit()
    finally:
        db.close()

    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="Squadforces", lifespan=lifespan)

app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "squadforces-dev-secret-change-me"),
)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="app/templates")

app.include_router(groups.router)
app.include_router(assignments.router)
app.include_router(recommend.router)
app.include_router(admin_router.router)


# --- Exception handlers ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)
    if exc.status_code == 403:
        return HTMLResponse(
            "<h1>403 Forbidden</h1><p>You don't have permission to access this page.</p>",
            status_code=403,
        )
    return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>", status_code=exc.status_code)


# --- Root ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if not request.session.get("account_id"):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/groups/", status_code=303)


# --- Login / Logout ---
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if request.session.get("account_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", {"request": request, "error": "", "next": next})


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    account = db.query(models.Account).filter_by(username=username.strip()).first()
    if account and verify_password(password, account.password_hash):
        request.session["account_id"] = account.id
        return RedirectResponse(url=next or "/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Incorrect username or password.", "next": next},
        status_code=401,
    )


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- Register (user role only — students are created by admin) ---
@app.get("/register", response_class=HTMLResponse)
async def register_page(request: Request):
    if request.session.get("account_id"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("register.html", {"request": request, "error": ""})


@app.post("/register")
async def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    db: Session = Depends(get_db),
):
    username = username.strip()
    error = ""
    if not username or not password:
        error = "Username and password are required."
    elif password != password2:
        error = "Passwords do not match."
    elif len(password) < 6:
        error = "Password must be at least 6 characters."
    elif db.query(models.Account).filter_by(username=username).first():
        error = f"Username '{username}' is already taken."

    if error:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": error}, status_code=422
        )

    account = models.Account(
        username=username,
        password_hash=hash_password(password),
        role="user",
    )
    db.add(account)
    db.commit()
    request.session["account_id"] = account.id
    return RedirectResponse("/", status_code=303)
