import os
from contextlib import asynccontextmanager
from urllib.parse import urlencode

from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request, Form, Depends
from fastapi.exceptions import HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

load_dotenv()

from app.database import engine, Base, get_db, SessionLocal, ensure_columns
from app.templating import make_templates
from app import models
from app.auth import (
    hash_password, hash_password_async, verify_password_async, needs_rehash, dummy_hash,
    require_auth, can_edit_user, MIN_PASSWORD_LENGTH, safe_next, login_throttle,
)
from app.routers import groups, assignments, recommend
from app.routers import admin as admin_router
from app import scheduler
from app.scraper import codeforces as cf
from app import activity as activity_svc
from app import histories


@asynccontextmanager
async def lifespan(app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_columns()
    dummy_hash()  # build it now, so the first login for an unknown name isn't visibly slower than later ones

    db = SessionLocal()
    try:
        # Seed admin with id=0 on first run
        if not db.query(models.User).filter(models.User.id == 0).first():
            admin_password = _admin_password()
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

DEV_SECRET_KEY = "squadforces-dev-secret-change-me"
DEV_ADMIN_PASSWORD = "squadforces2024"


def _deployed() -> bool:
    return bool(os.getenv("RAILWAY_ENVIRONMENT"))


def _secret_key() -> str:
    """The session-signing key. The built-in development key is public (it is in this repository), so a deployment
    without SECRET_KEY refuses to start instead of running with forgeable sessions."""
    key = os.getenv("SECRET_KEY")
    if key:
        return key
    if _deployed():
        raise RuntimeError("SECRET_KEY must be set when deployed: sessions would be signed with a public key")
    return DEV_SECRET_KEY


def _admin_password() -> str:
    """Password for the admin account created on a fresh database. Same rule: no public default when deployed."""
    password = os.getenv("ADMIN_PASSWORD")
    if password:
        return password
    if _deployed():
        raise RuntimeError("ADMIN_PASSWORD must be set when creating the admin account on a deployed database")
    return DEV_ADMIN_PASSWORD


def _cookie_secure() -> bool:
    """Mark the session cookie Secure (HTTPS only) when deployed. Railway serves the app over HTTPS."""
    return os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes") or bool(os.getenv("RAILWAY_ENVIRONMENT"))


app.add_middleware(
    SessionMiddleware,
    secret_key=_secret_key(),
    same_site="lax",  # not sent on cross-site POSTs, which is what blocks cross-site form forgery
    https_only=_cookie_secure(),
)
class RevalidatingStaticFiles(StaticFiles):
    """Force browsers to revalidate (ETag -> 304) so CSS/JS edits show up right after a deploy."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/static", RevalidatingStaticFiles(directory="static"), name="static")
templates = make_templates()

app.include_router(groups.router)
app.include_router(assignments.router)
app.include_router(recommend.router)
app.include_router(admin_router.router)


# --- Exception handlers ---
@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return RedirectResponse(url="/login?" + urlencode({"next": request.url.path}), status_code=303)
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
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": "", "next": safe_next(next)})


@app.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
    db: Session = Depends(get_db),
):
    next = safe_next(next)
    throttle_key = username.strip().lower()
    wait = login_throttle.blocked_for(throttle_key)
    if wait:
        return templates.TemplateResponse(request,
            "login.html",
            {"request": request, "error": f"Too many failed attempts. Try again in {wait} seconds.", "next": next},
            status_code=429,
        )
    user = db.query(models.User).filter_by(username=username.strip()).first()
    # Always do a full hash, even for an unknown username, so response time doesn't reveal which names exist.
    verified = await verify_password_async(password, user.password_hash if user else dummy_hash())
    if user and verified:
        login_throttle.reset(throttle_key)
        if needs_rehash(user.password_hash):
            # Legacy SHA-256 hash (or fewer iterations than now): upgrade it while we hold the plaintext.
            user.password_hash = await hash_password_async(password)
            db.commit()
        request.session["user_id"] = user.id
        request.session["sv"] = user.session_version
        return RedirectResponse(url=next, status_code=303)
    login_throttle.record_failure(throttle_key)
    return templates.TemplateResponse(request,
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
    return templates.TemplateResponse(request, "register.html", {"request": request, "error": ""})


@app.post("/register")
async def register(
    request: Request,
    background_tasks: BackgroundTasks,
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
    elif len(password) < MIN_PASSWORD_LENGTH:
        error = f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
    elif db.query(models.User).filter_by(username=username).first():
        error = f"Username '{username}' is already taken."
    elif cf_handle and db.query(models.User).filter_by(codeforces_handle=cf_handle).first():
        error = f"Codeforces handle '{cf_handle}' is already linked to another account."

    if error:
        return templates.TemplateResponse(request,
            "register.html", {"request": request, "error": error}, status_code=422
        )

    # Validate CF handle if provided
    cf_rating = cf_rank = None
    if cf_handle:
        user_info = await cf.validate_handle(cf_handle)
        if not user_info:
            return templates.TemplateResponse(request,
                "register.html",
                {"request": request, "error": f"Codeforces handle '{cf_handle}' does not exist."},
                status_code=422,
            )
        cf_rating = user_info.get("rating")
        cf_rank = user_info.get("rank")

    user = models.User(
        username=username,
        password_hash=await hash_password_async(password),
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
    user_id, session_version = user.id, user.session_version
    background_tasks.add_task(histories.load_histories, user_id, histories.apply_handle_changes(db, user))
    request.session["user_id"] = user_id
    request.session["sv"] = session_version
    db.close()  # end the request's transaction: a background task that writes must not wait on it (SQLite locks the file)
    return RedirectResponse("/", status_code=303)


# --- Change password ---
def _password_page(request: Request, account, error: str = "", changed: bool = False, status_code: int = 200):
    return templates.TemplateResponse(request,
        "account/password.html",
        {"request": request, "account": account, "error": error, "changed": changed,
         "min_length": MIN_PASSWORD_LENGTH},
        status_code=status_code,
    )


@app.get("/account/password", response_class=HTMLResponse)
async def password_page(request: Request, changed: str = "", account=Depends(require_auth)):
    return _password_page(request, account, changed=bool(changed))


@app.post("/account/password")
async def change_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    error = ""
    if not await verify_password_async(current_password, account.password_hash):
        error = "Current password is incorrect."
    elif len(new_password) < MIN_PASSWORD_LENGTH:
        error = f"New password must be at least {MIN_PASSWORD_LENGTH} characters."
    elif new_password != confirm_password:
        error = "The new passwords do not match."
    elif new_password == current_password:
        error = "Choose a password different from your current one."
    if error:
        return _password_page(request, account, error=error, status_code=422)

    account.password_hash = await hash_password_async(new_password)
    account.session_version += 1   # signs out every other device; this one is re-stamped just below
    db.commit()
    request.session["sv"] = account.session_version
    return RedirectResponse("/account/password?changed=1", status_code=303)


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
    return templates.TemplateResponse(request, "users/profile.html", {
        "request": request,
        "user": user,
        "account": account,
        "can_edit": can_edit_user(account, user),
        "history_status": histories.history_status(db, user),
        "recent_submissions": await histories.recent_submissions(db, user),
    })


@app.post("/users/{username}/edit")
async def edit_user_profile(
    request: Request,
    background_tasks: BackgroundTasks,
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

    handles_before = histories.handles_of(user)
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

    to_load = histories.apply_handle_changes(db, user, handles_before)
    db.commit()
    background_tasks.add_task(histories.load_histories, user.id, to_load)
    db.close()  # end the request's transaction: a background task that writes must not wait on it (SQLite locks the file)
    return RedirectResponse(f"/users/{username}", status_code=303)


@app.post("/users/{username}/reload-history")
async def reload_history(
    username: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    """Re-fetch a user's submissions now (e.g. after fixing a handle that failed to load)."""
    user = db.query(models.User).filter_by(username=username).first()
    if not user:
        return HTMLResponse("User not found", status_code=404)
    if not can_edit_user(account, user):
        raise HTTPException(status_code=403)
    background_tasks.add_task(histories.load_histories, user.id, histories.HISTORY_PLATFORMS, force=True)
    db.close()  # end the request's transaction: a background task that writes must not wait on it (SQLite locks the file)
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

    activity = await activity_svc.user_activity(db, user)
    return JSONResponse(activity)


# --- Help ---
@app.get("/help", response_class=HTMLResponse)
async def help_page(request: Request, account=Depends(require_auth)):
    return templates.TemplateResponse(request, "help.html", {"request": request, "account": account})
