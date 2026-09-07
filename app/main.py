import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form
from fastapi.exceptions import HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

load_dotenv()

from app.database import engine, Base, get_db
from app import models  # noqa: F401 — registers models with Base
from app.auth import require_auth, login_response, logout_response, ADMIN_PASSWORD, is_authenticated
from app.routers import groups, assignments
from app import scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    from sqlalchemy import text
    Base.metadata.create_all(bind=engine)
    # Runtime migration: add new columns to problem_results if missing
    _new_cols = [
        ("solve_type", "VARCHAR(20)"),
        ("attempts", "INTEGER"),
        ("best_wrong_verdict", "VARCHAR(30)"),
    ]
    with engine.connect() as conn:
        for col, coldef in _new_cols:
            try:
                conn.execute(text(f"ALTER TABLE problem_results ADD COLUMN {col} {coldef}"))
                conn.commit()
            except Exception:
                pass  # column already exists
    scheduler.start()
    yield
    scheduler.stop()


app = FastAPI(title="Squadforces", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="app/templates")

app.include_router(groups.router)
app.include_router(assignments.router)


# --- Auth exception handler ---
@app.exception_handler(HTTPException)
async def auth_exception_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        return RedirectResponse(url=f"/login?next={request.url.path}", status_code=303)
    return HTMLResponse(f"<h1>{exc.status_code}</h1><p>{exc.detail}</p>", status_code=exc.status_code)


# --- Root ---
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return RedirectResponse("/groups/", status_code=303)


# --- Login / Logout ---
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
async def login(
    request: Request,
    password: str = Form(...),
    next: str = Form("/"),
):
    if password == ADMIN_PASSWORD:
        return login_response(redirect_to=next or "/")
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Incorrect password.", "next": next},
        status_code=401,
    )


@app.post("/logout")
async def logout(_=None):
    return logout_response()
