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

from app.database import engine, Base, get_db, SessionLocal, ensure_columns
from app import models
from app.auth import hash_password, verify_password, require_auth, can_edit_user
from app.routers import groups, assignments, recommend
from app.routers import admin as admin_router
from app import scheduler
from app.scraper import codeforces as cf
from app.scraper import atcoder as ac


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_columns()

    db = SessionLocal()
    try:
        # Seed admin with id=0 on first run
        if not db.query(models.User).filter(models.User.id == 0).first():
            admin_password = os.getenv("ADMIN_PASSWORD", "squadforces2024")
            db.add(models.User(
                id=0,
                username="admin",
                password_hash=hash_password(admin_password),
                user_type="admin",
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
class RevalidatingStaticFiles(StaticFiles):
    """Force browsers to revalidate (ETag -> 304) so CSS/JS edits show up right after a deploy."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatingStaticFiles(directory="static"), name="static")
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
    if request.session.get("user_id") is None:
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/groups/", status_code=303)


# --- Login / Logout ---
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if request.session.get("user_id") is not None:
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
    user = db.query(models.User).filter_by(username=username.strip()).first()
    if user and verify_password(password, user.password_hash):
        request.session["user_id"] = user.id
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
    if request.session.get("user_id") is not None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("register.html", {"request": request, "error": ""})


@app.post("/register")
async def register(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    full_name: str = Form(""),
    cf_handle: str = Form(""),
    atcoder_handle: str = Form(""),
    kilonova_handle: str = Form(""),
    db: Session = Depends(get_db),
):
    username = username.strip()
    cf_handle = cf_handle.strip()

    error = ""
    if not username or not password:
        error = "Username and password are required."
    elif password != password2:
        error = "Passwords do not match."
    elif len(password) < 6:
        error = "Password must be at least 6 characters."
    elif db.query(models.User).filter_by(username=username).first():
        error = f"Username '{username}' is already taken."
    elif cf_handle and db.query(models.User).filter_by(codeforces_handle=cf_handle).first():
        error = f"Codeforces handle '{cf_handle}' is already linked to another account."

    if error:
        return templates.TemplateResponse(
            "register.html", {"request": request, "error": error}, status_code=422
        )

    # Validate CF handle if provided
    cf_rating = cf_rank = None
    if cf_handle:
        user_info = await cf.validate_handle(cf_handle)
        if not user_info:
            return templates.TemplateResponse(
                "register.html",
                {"request": request, "error": f"Codeforces handle '{cf_handle}' does not exist."},
                status_code=422,
            )
        cf_rating = user_info.get("rating")
        cf_rank = user_info.get("rank")

    user = models.User(
        username=username,
        password_hash=hash_password(password),
        user_type="user",
        full_name=full_name.strip() or None,
        codeforces_handle=cf_handle or None,
        atcoder_handle=atcoder_handle.strip() or None,
        kilonova_handle=kilonova_handle.strip() or None,
        cf_rating=cf_rating,
        cf_rank=cf_rank,
    )
    db.add(user)
    db.commit()
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


# --- User profiles ---
@app.get("/profile", response_class=HTMLResponse)
async def my_profile(request: Request, account=Depends(require_auth)):
    return RedirectResponse(f"/users/{account.username}", status_code=303)


@app.get("/users/{username}", response_class=HTMLResponse)
async def user_profile(
    request: Request, username: str,
    db: Session = Depends(get_db), account=Depends(require_auth),
):
    user = db.query(models.User).filter_by(username=username).first()
    if not user:
        return HTMLResponse("User not found", status_code=404)
    return templates.TemplateResponse("users/profile.html", {
        "request": request,
        "user": user,
        "account": account,
        "can_edit": can_edit_user(account, user),
    })


@app.post("/users/{username}/edit")
async def edit_user_profile(
    request: Request,
    username: str,
    full_name: str = Form(""),
    cf_handle: str = Form(""),
    atcoder_handle: str = Form(""),
    kilonova_handle: str = Form(""),
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    user = db.query(models.User).filter_by(username=username).first()
    if not user:
        return HTMLResponse("User not found", status_code=404)
    if not can_edit_user(account, user):
        raise HTTPException(status_code=403)

    user.full_name = full_name.strip() or None
    user.atcoder_handle = atcoder_handle.strip() or None
    user.kilonova_handle = kilonova_handle.strip() or None

    new_cf = cf_handle.strip()
    if new_cf and new_cf != user.codeforces_handle and account.user_type == "admin":
        user_info = await cf.validate_handle(new_cf)
        if user_info:
            user.codeforces_handle = new_cf
            user.cf_rating = user_info.get("rating")
            user.cf_rank = user_info.get("rank")

    db.commit()
    return RedirectResponse(f"/users/{username}", status_code=303)


@app.get("/users/{username}/activity.json")
async def user_activity(
    username: str,
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    user = db.query(models.User).filter_by(username=username).first()
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


# --- Help ---
@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request, account=Depends(require_auth)):
    return templates.TemplateResponse("help.html", {"request": request, "account": account})
