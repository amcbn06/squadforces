from fastapi import APIRouter, BackgroundTasks, Depends, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.templating import make_templates
from app.models import User, Group, GroupMembership
from app.auth import require_admin, hash_password_async
from app import histories, submissions
from app.scraper import codeforces as cf

router = APIRouter(prefix="/admin", tags=["admin"])
templates = make_templates()


@router.get("/users", response_class=HTMLResponse)
async def list_users(
    request: Request,
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    users = db.query(User).filter(User.user_type != "admin").order_by(User.created_at).all()
    all_groups = db.query(Group).order_by(Group.name).all()
    error = request.query_params.get("error", "")
    return templates.TemplateResponse(request, "admin/users.html", {
        "request": request,
        "account": account,
        "users": users,
        "all_groups": all_groups,
        "error": error,
    })


@router.get("/users/new", response_class=HTMLResponse)
async def new_user_form(
    request: Request,
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    all_groups = db.query(Group).order_by(Group.name).all()
    return templates.TemplateResponse(request, "admin/user_form.html", {
        "request": request,
        "account": account,
        "edit_user": None,
        "all_groups": all_groups,
        "selected_group_ids": [],
        "error": None,
    })


@router.post("/users/new")
async def create_user(
    request: Request,
    background_tasks: BackgroundTasks,
    username: str = Form(...),
    password: str = Form(...),
    user_type: str = Form("user"),
    full_name: str = Form(""),
    cf_handle: str = Form(""),
    atcoder_handle: str = Form(""),
    kilonova_handle: str = Form(""),
    group_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    username = username.strip()
    cf_handle = cf_handle.strip()
    error = None

    if not username or not password:
        error = "Username and password are required."
    elif db.query(User).filter_by(username=username).first():
        error = f"Username '{username}' is already taken."
    elif cf_handle and db.query(User).filter_by(codeforces_handle=cf_handle).first():
        error = f"Codeforces handle '{cf_handle}' is already in use."

    if error:
        all_groups = db.query(Group).order_by(Group.name).all()
        return templates.TemplateResponse(request, "admin/user_form.html", {
            "request": request, "account": account, "edit_user": None,
            "all_groups": all_groups, "selected_group_ids": group_ids, "error": error,
        }, status_code=422)

    # Validate + fetch CF rating if handle provided
    cf_rating = cf_rank = None
    if cf_handle:
        user_info = await cf.validate_handle(cf_handle)
        if user_info:
            cf_rating = user_info.get("rating")
            cf_rank = user_info.get("rank")

    user_type = user_type if user_type in ("user", "student") else "user"
    new_user = User(
        username=username,
        password_hash=await hash_password_async(password),
        user_type=user_type,
        full_name=full_name.strip() or None,
        codeforces_handle=cf_handle or None,
        atcoder_handle=atcoder_handle.strip() or None,
        kilonova_handle=kilonova_handle.strip() or None,
        cf_rating=cf_rating,
        cf_rank=cf_rank,
    )
    db.add(new_user)
    db.flush()
    for gid in group_ids:
        if db.get(Group, gid):
            db.add(GroupMembership(group_id=gid, user_id=new_user.id))
    db.commit()
    background_tasks.add_task(histories.load_histories, new_user.id, histories.apply_handle_changes(db, new_user))
    db.close()  # end the request's transaction: a background task that writes must not wait on it (SQLite locks the file)
    return RedirectResponse("/admin/users", status_code=303)


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
async def edit_user_form(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    edit_user = db.query(User).filter(User.id == user_id).first()
    if not edit_user:
        return HTMLResponse("User not found", status_code=404)
    all_groups = db.query(Group).order_by(Group.name).all()
    selected_ids = [m.group_id for m in edit_user.memberships]
    return templates.TemplateResponse(request, "admin/user_form.html", {
        "request": request,
        "account": account,
        "edit_user": edit_user,
        "all_groups": all_groups,
        "selected_group_ids": selected_ids,
        "error": None,
    })


@router.post("/users/{user_id}/edit")
async def edit_user(
    request: Request,
    background_tasks: BackgroundTasks,
    user_id: int,
    username: str = Form(...),
    password: str = Form(""),
    user_type: str = Form("user"),
    full_name: str = Form(""),
    cf_handle: str = Form(""),
    atcoder_handle: str = Form(""),
    kilonova_handle: str = Form(""),
    group_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    edit_user = db.query(User).filter(User.id == user_id).first()
    if not edit_user:
        return HTMLResponse("User not found", status_code=404)

    username = username.strip()
    cf_handle = cf_handle.strip()
    duplicate = db.query(User).filter(User.username == username, User.id != user_id).first()
    if duplicate:
        all_groups = db.query(Group).order_by(Group.name).all()
        return templates.TemplateResponse(request, "admin/user_form.html", {
            "request": request, "account": account, "edit_user": edit_user,
            "all_groups": all_groups, "selected_group_ids": group_ids,
            "error": f"Username '{username}' is already taken.",
        }, status_code=422)

    handles_before = histories.handles_of(edit_user)
    edit_user.username = username
    password_reset = bool(password.strip())
    if password_reset:
        edit_user.password_hash = await hash_password_async(password.strip())
        edit_user.session_version += 1   # signs that user out on every device
    if user_type in ("user", "student") and edit_user.id != 0:  # the built-in admin account stays an admin
        edit_user.user_type = user_type
    edit_user.full_name = full_name.strip() or None
    edit_user.atcoder_handle = atcoder_handle.strip() or None
    edit_user.kilonova_handle = kilonova_handle.strip() or None

    if cf_handle != (edit_user.codeforces_handle or ""):
        edit_user.codeforces_handle = cf_handle or None
        if cf_handle:
            user_info = await cf.validate_handle(cf_handle)
            if user_info:
                edit_user.cf_rating = user_info.get("rating")
                edit_user.cf_rank = user_info.get("rank")

    # Replace group memberships
    db.query(GroupMembership).filter_by(user_id=user_id).delete()
    for gid in group_ids:
        if db.get(Group, gid):
            db.add(GroupMembership(group_id=gid, user_id=user_id))
    to_load = histories.apply_handle_changes(db, edit_user, handles_before)
    db.commit()
    background_tasks.add_task(histories.load_histories, edit_user.id, to_load)
    if password_reset and edit_user.id == account.id:
        request.session["sv"] = edit_user.session_version   # the admin reset their own; keep this session
    db.close()  # end the request's transaction: a background task that writes must not wait on it (SQLite locks the file)
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    if user_id == 0:
        return RedirectResponse("/admin/users?error=Cannot+delete+admin", status_code=303)
    user = db.query(User).filter(User.id == user_id).first()
    if user:
        submissions.delete_user_data(db, user_id)  # SQLite doesn't cascade these on its own
        db.query(Group).filter(Group.owner_id == user_id).update({"owner_id": 0})  # their groups go back to the admin
        db.delete(user)
        db.commit()
    return RedirectResponse("/admin/users", status_code=303)


# Legacy redirect: old /admin/accounts URL
@router.get("/accounts", response_class=HTMLResponse)
async def legacy_accounts(account=Depends(require_admin)):
    return RedirectResponse("/admin/users", status_code=303)
