import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form, Depends
from fastapi.exceptions import HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

from app.database import engine, Base, get_db, SessionLocal
from app import models  # noqa: F401 — registers models with Base
from app.auth import hash_password, verify_password, require_auth, can_edit_user
from app.routers import groups, assignments, recommend
from app.routers import admin as admin_router
from app import scheduler
from app.scraper import codeforces as cf
from app.scraper import atcoder as ac


@asynccontextmanager
async def lifespan(app: FastAPI):
    from sqlalchemy import text, not_, exists
    Base.metadata.create_all(bind=engine)

    # Runtime migrations: add new columns to existing tables if missing
    _migrations = [
        ("problem_results", "solve_type", "VARCHAR(20)"),
        ("problem_results", "attempts", "INTEGER"),
        ("problem_results", "best_wrong_verdict", "VARCHAR(30)"),
        ("assignment_items", "created_by_id", "INTEGER"),
        ("users", "kilonova_handle", "VARCHAR(50)"),
        ("problem_results", "score", "INTEGER"),
        ("accounts", "user_id", "INTEGER"),
    ]
    with engine.connect() as conn:
        for table, col, coldef in _migrations:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {coldef}"))
                conn.commit()
            except Exception:
                pass  # column already exists

    db = SessionLocal()
    try:
        # Seed admin account on first run
        if db.query(models.Account).count() == 0:
            admin_password = os.getenv("ADMIN_PASSWORD", "squadforces2024")
            db.add(models.Account(
                username="admin",
                password_hash=hash_password(admin_password),
                role="admin",
            ))
            db.commit()

        # Link existing accounts to users by CF handle match (idempotent)
        for acct in db.query(models.Account).filter(models.Account.user_id.is_(None)).all():
            user = db.query(models.User).filter(
                models.User.codeforces_handle.ilike(acct.username)
            ).first()
            if user:
                acct.user_id = user.id
        db.commit()

        # Remove GroupMembership rows for users with no registered account
        linked_user_ids = {
            a.user_id for a in db.query(models.Account).filter(models.Account.user_id.isnot(None)).all()
        }
        orphans = db.query(models.GroupMembership).filter(
            ~models.GroupMembership.user_id.in_(linked_user_ids) if linked_user_ids
            else models.GroupMembership.user_id.isnot(None)
        ).all()
        for m in orphans:
            db.delete(m)
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


# --- Register ---
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
    full_name: str = Form(...),
    cf_handle: str = Form(...),
    atcoder_handle: str = Form(""),
    kilonova_handle: str = Form(""),
    db: Session = Depends(get_db),
):
    username = username.strip()
    full_name = full_name.strip()
    cf_handle = cf_handle.strip()

    error = ""
    if not username or not password:
        error = "Username and password are required."
    elif not full_name:
        error = "Full name is required."
    elif not cf_handle:
        error = "Codeforces handle is required."
    elif password != password2:
        error = "Passwords do not match."
    elif len(password) < 6:
        error = "Password must be at least 6 characters."
    elif db.query(models.Account).filter_by(username=username).first():
        error = f"Username '{username}' is already taken."
    else:
        # Check CF handle not already claimed by another account
        existing_user = db.query(models.User).filter_by(codeforces_handle=cf_handle).first()
        if existing_user:
            clash = db.query(models.Account).filter_by(user_id=existing_user.id).first()
            if clash:
                error = f"Codeforces handle '{cf_handle}' is already linked to another account."

    if error:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": error}, status_code=422
        )

    # Validate CF handle exists on Codeforces
    user_info = await cf.validate_handle(cf_handle)
    if not user_info:
        return templates.TemplateResponse(
            "register.html",
            {"request": request, "error": f"Codeforces handle '{cf_handle}' does not exist."},
            status_code=422,
        )

    # Reuse existing User row (pre-added by admin) or create new one
    user = db.query(models.User).filter_by(codeforces_handle=cf_handle).first()
    if user:
        user.display_name = full_name
        user.cf_rating = user_info.get("rating")
        user.cf_rank = user_info.get("rank")
        if atcoder_handle.strip():
            user.atcoder_handle = atcoder_handle.strip()
        if kilonova_handle.strip():
            user.kilonova_handle = kilonova_handle.strip()
    else:
        user = models.User(
            display_name=full_name,
            codeforces_handle=cf_handle,
            cf_rating=user_info.get("rating"),
            cf_rank=user_info.get("rank"),
            atcoder_handle=atcoder_handle.strip() or None,
            kilonova_handle=kilonova_handle.strip() or None,
        )
        db.add(user)
        db.flush()

    account = models.Account(
        username=username,
        password_hash=hash_password(password),
        role="user",
        user_id=user.id,
    )
    db.add(account)
    db.commit()
    request.session["account_id"] = account.id
    return RedirectResponse("/", status_code=303)


# --- Standalone user profiles ---
@app.get("/profile", response_class=HTMLResponse)
async def my_profile(request: Request, account=Depends(require_auth)):
    if account.user_id:
        return RedirectResponse(f"/users/{account.user_id}", status_code=303)
    return templates.TemplateResponse(
        "users/no_profile.html", {"request": request, "account": account}
    )


@app.get("/users/{user_id}", response_class=HTMLResponse)
async def user_profile(
    request: Request, user_id: int,
    db: Session = Depends(get_db), account=Depends(require_auth),
):
    user = db.get(models.User, user_id)
    if not user:
        return HTMLResponse("Not found", status_code=404)
    return templates.TemplateResponse("users/profile.html", {
        "request": request,
        "user": user,
        "account": account,
        "can_edit": can_edit_user(account, user),
    })


@app.post("/users/{user_id}/edit")
async def edit_user_profile(
    request: Request,
    user_id: int,
    display_name: str = Form(""),
    cf_handle: str = Form(""),
    atcoder_handle: str = Form(""),
    kilonova_handle: str = Form(""),
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    user = db.get(models.User, user_id)
    if not user:
        return HTMLResponse("Not found", status_code=404)
    if not can_edit_user(account, user):
        raise HTTPException(status_code=403, detail="You cannot edit this profile.")

    if display_name.strip():
        user.display_name = display_name.strip()
    user.atcoder_handle = atcoder_handle.strip() or None
    user.kilonova_handle = kilonova_handle.strip() or None

    new_cf = cf_handle.strip()
    if new_cf and new_cf != user.codeforces_handle and account.role == "admin":
        user_info = await cf.validate_handle(new_cf)
        if user_info:
            user.codeforces_handle = new_cf
            user.cf_rating = user_info.get("rating")
            user.cf_rank = user_info.get("rank")

    db.commit()
    return RedirectResponse(f"/users/{user_id}", status_code=303)


@app.get("/users/{user_id}/activity.json")
async def user_activity(
    user_id: int,
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    user = db.get(models.User, user_id)
    if not user:
        return JSONResponse({})

    now = datetime.now(timezone.utc)
    cutoff_ts = datetime(now.year - 2, 1, 1, tzinfo=timezone.utc).timestamp()
    activity: dict[str, dict] = {}

    def add(date_str: str, platform: str) -> None:
        if date_str not in activity:
            activity[date_str] = {"cf": 0, "atc": 0}
        activity[date_str][platform] += 1

    if user.codeforces_handle:
        try:
            subs = await cf.get_all_user_submissions(user.codeforces_handle, 3000)
            for s in subs:
                ts = s.get("creationTimeSeconds", 0)
                if ts < cutoff_ts:
                    continue
                date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                add(date, "cf")
        except Exception:
            pass

    if user.atcoder_handle:
        try:
            subs = await ac.get_user_submissions(user.atcoder_handle)
            for s in subs:
                ts = s.get("epoch_second", 0)
                if ts < cutoff_ts:
                    continue
                date = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
                add(date, "atc")
        except Exception:
            pass

    return JSONResponse(activity)


@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request, account=Depends(require_auth)):
    return templates.TemplateResponse("help.html", {"request": request, "account": account})
