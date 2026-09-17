from fastapi import APIRouter, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Account, AccountGroupAccess, Group
from app.auth import require_admin, hash_password

router = APIRouter(prefix="/admin", tags=["admin"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/accounts", response_class=HTMLResponse)
async def list_accounts(
    request: Request,
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    accounts = db.query(Account).order_by(Account.created_at).all()
    all_groups = db.query(Group).order_by(Group.name).all()
    error = request.query_params.get("error", "")
    return templates.TemplateResponse("admin/accounts.html", {
        "request": request,
        "account": account,
        "accounts": accounts,
        "all_groups": all_groups,
        "error": error,
    })


@router.get("/accounts/new", response_class=HTMLResponse)
async def new_account_form(
    request: Request,
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    all_groups = db.query(Group).order_by(Group.name).all()
    return templates.TemplateResponse("admin/account_form.html", {
        "request": request,
        "account": account,
        "edit_account": None,
        "all_groups": all_groups,
        "selected_group_ids": [],
        "error": None,
    })


@router.post("/accounts/new")
async def create_account(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    role: str = Form(...),
    group_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    username = username.strip()
    if not username or not password:
        all_groups = db.query(Group).order_by(Group.name).all()
        return templates.TemplateResponse("admin/account_form.html", {
            "request": request, "account": account, "edit_account": None,
            "all_groups": all_groups, "selected_group_ids": group_ids,
            "error": "Username and password are required.",
        }, status_code=422)

    if db.query(Account).filter_by(username=username).first():
        all_groups = db.query(Group).order_by(Group.name).all()
        return templates.TemplateResponse("admin/account_form.html", {
            "request": request, "account": account, "edit_account": None,
            "all_groups": all_groups, "selected_group_ids": group_ids,
            "error": f"Username '{username}' is already taken.",
        }, status_code=422)

    role = role if role in ("admin", "user", "student") else "user"
    new_acct = Account(username=username, password_hash=hash_password(password), role=role)
    db.add(new_acct)
    db.flush()
    for gid in group_ids:
        if db.get(Group, gid):
            db.add(AccountGroupAccess(account_id=new_acct.id, group_id=gid))
    db.commit()
    return RedirectResponse("/admin/accounts", status_code=303)


@router.get("/accounts/{account_id}/edit", response_class=HTMLResponse)
async def edit_account_form(
    request: Request,
    account_id: int,
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    edit_acct = db.get(Account, account_id)
    if not edit_acct:
        return HTMLResponse("Account not found", status_code=404)
    all_groups = db.query(Group).order_by(Group.name).all()
    selected_ids = [a.group_id for a in edit_acct.group_access]
    return templates.TemplateResponse("admin/account_form.html", {
        "request": request,
        "account": account,
        "edit_account": edit_acct,
        "all_groups": all_groups,
        "selected_group_ids": selected_ids,
        "error": None,
    })


@router.post("/accounts/{account_id}/edit")
async def edit_account(
    request: Request,
    account_id: int,
    username: str = Form(...),
    password: str = Form(""),
    role: str = Form(...),
    group_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    edit_acct = db.get(Account, account_id)
    if not edit_acct:
        return HTMLResponse("Account not found", status_code=404)

    username = username.strip()
    duplicate = db.query(Account).filter(
        Account.username == username, Account.id != account_id
    ).first()
    if duplicate:
        all_groups = db.query(Group).order_by(Group.name).all()
        return templates.TemplateResponse("admin/account_form.html", {
            "request": request, "account": account, "edit_account": edit_acct,
            "all_groups": all_groups, "selected_group_ids": group_ids,
            "error": f"Username '{username}' is already taken.",
        }, status_code=422)

    edit_acct.username = username
    if password.strip():
        edit_acct.password_hash = hash_password(password.strip())
    if role in ("admin", "user", "student"):
        edit_acct.role = role

    # Replace group access
    db.query(AccountGroupAccess).filter_by(account_id=account_id).delete()
    for gid in group_ids:
        if db.get(Group, gid):
            db.add(AccountGroupAccess(account_id=account_id, group_id=gid))
    db.commit()
    return RedirectResponse("/admin/accounts", status_code=303)


@router.post("/accounts/{account_id}/delete")
async def delete_account(
    account_id: int,
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    if account.id == account_id:
        return RedirectResponse("/admin/accounts?error=Cannot+delete+your+own+account", status_code=303)
    edit_acct = db.get(Account, account_id)
    if edit_acct:
        db.delete(edit_acct)
        db.commit()
    return RedirectResponse("/admin/accounts", status_code=303)
