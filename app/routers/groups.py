from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Group, User, GroupMembership, Assignment, AssignmentItem, Result
from app.auth import require_auth, require_admin, can_edit_user
from app.scraper import codeforces as cf
from app.scraper import atcoder as ac

router = APIRouter(prefix="/groups", tags=["groups"])
templates = Jinja2Templates(directory="app/templates")


def _check_group_access(account, group_id: int, db: Session):
    if account.user_type == "admin":
        return
    if db.query(GroupMembership).filter_by(group_id=group_id, user_id=account.id).first():
        return
    raise HTTPException(status_code=403)


@router.get("/", response_class=HTMLResponse)
async def list_groups(request: Request, db: Session = Depends(get_db), account=Depends(require_auth)):
    if account.user_type == "admin":
        groups = db.query(Group).order_by(Group.created_at.desc()).all()
    else:
        accessible_ids = [
            m.group_id for m in
            db.query(GroupMembership).filter_by(user_id=account.id).all()
        ]
        groups = (
            db.query(Group)
            .filter(Group.id.in_(accessible_ids))
            .order_by(Group.created_at.desc())
            .all()
        ) if accessible_ids else []
    return templates.TemplateResponse("groups/list.html", {
        "request": request, "groups": groups, "account": account,
    })


@router.get("/new", response_class=HTMLResponse)
async def new_group_form(request: Request, account=Depends(require_admin)):
    return templates.TemplateResponse("groups/form.html", {
        "request": request, "group": None, "error": None, "account": account,
    })


@router.post("/new")
async def create_group(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    name = name.strip()
    if not name:
        return templates.TemplateResponse(
            "groups/form.html",
            {"request": request, "group": None, "error": "Group name is required.", "account": account},
            status_code=422,
        )
    group = Group(name=name, description=description.strip() or None)
    db.add(group)
    db.commit()
    return RedirectResponse(f"/groups/{group.id}", status_code=303)


@router.get("/{group_id}", response_class=HTMLResponse)
async def group_detail(
    request: Request, group_id: int, db: Session = Depends(get_db), account=Depends(require_auth)
):
    group = db.get(Group, group_id)
    if not group:
        return HTMLResponse("Group not found", status_code=404)
    _check_group_access(account, group_id, db)

    members = [m.user for m in group.memberships]

    # 30-day leaderboard
    cutoff = datetime.utcnow() - timedelta(days=30)
    recent_items = (
        db.query(AssignmentItem)
        .join(Assignment)
        .filter(
            Assignment.group_id == group_id,
            AssignmentItem.added_at >= cutoff,
        )
        .all()
    )

    leaderboard = []
    for user in members:
        problems_solved = 0
        contests_done = 0
        for item in recent_items:
            result = db.query(Result).filter_by(assignment_item_id=item.id, user_id=user.id).first()
            if item.type == "contest":
                if result and result.participated:
                    contests_done += 1
                if result and result.problems_solved_count:
                    problems_solved += result.problems_solved_count
            elif item.type == "problem":
                if result and result.solved:
                    problems_solved += 1
        leaderboard.append({
            "user": user,
            "problems_solved": problems_solved,
            "contests_done": contests_done,
        })
    leaderboard.sort(key=lambda x: (-x["problems_solved"], -x["contests_done"]))

    return templates.TemplateResponse(
        "groups/detail.html",
        {
            "request": request,
            "group": group,
            "members": members,
            "leaderboard": leaderboard,
            "account": account,
        },
    )


@router.get("/{group_id}/edit-page", response_class=HTMLResponse)
async def edit_group_page(
    request: Request, group_id: int, db: Session = Depends(get_db), account=Depends(require_admin)
):
    group = db.get(Group, group_id)
    if not group:
        return HTMLResponse("Group not found", status_code=404)
    return templates.TemplateResponse("groups/form.html", {
        "request": request, "group": group, "error": None, "account": account,
    })


@router.post("/{group_id}/edit")
async def edit_group(
    request: Request,
    group_id: int,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    group = db.get(Group, group_id)
    if not group:
        return HTMLResponse("Group not found", status_code=404)
    group.name = name.strip()
    group.description = description.strip() or None
    db.commit()
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


@router.post("/{group_id}/delete")
async def delete_group(
    group_id: int, db: Session = Depends(get_db), account=Depends(require_admin)
):
    group = db.get(Group, group_id)
    if group:
        db.delete(group)
        db.commit()
    return RedirectResponse("/groups/", status_code=303)


@router.post("/{group_id}/members/add")
async def add_member(
    request: Request,
    group_id: int,
    member_username: str = Form(...),
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    group = db.get(Group, group_id)
    if not group:
        return HTMLResponse("Group not found", status_code=404)

    member_username = member_username.strip()
    user = db.query(User).filter_by(username=member_username).first()
    error = None

    if not user:
        error = f"No user found with username '{member_username}'."
    elif user.user_type == "admin":
        error = "Admin cannot be added as a group member."
    else:
        existing = db.query(GroupMembership).filter_by(group_id=group_id, user_id=user.id).first()
        if existing:
            error = f"'{member_username}' is already in this group."
        else:
            db.add(GroupMembership(group_id=group_id, user_id=user.id))
            db.commit()
            return RedirectResponse(f"/groups/{group_id}", status_code=303)

    members = [m.user for m in group.memberships]
    return templates.TemplateResponse(
        "groups/detail.html",
        {
            "request": request, "group": group, "members": members,
            "error": error, "account": account, "leaderboard": [],
        },
        status_code=422,
    )


@router.post("/{group_id}/members/{user_id}/remove")
async def remove_member(
    group_id: int, user_id: int, db: Session = Depends(get_db), account=Depends(require_admin)
):
    membership = db.query(GroupMembership).filter_by(group_id=group_id, user_id=user_id).first()
    if membership:
        db.delete(membership)
        db.commit()
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


@router.get("/{group_id}/members/{user_id}/activity.json")
async def member_activity(
    group_id: int, user_id: int,
    db: Session = Depends(get_db), account=Depends(require_auth),
):
    """Legacy activity endpoint — redirects to the username-based endpoint."""
    _check_group_access(account, group_id, db)
    user = db.get(User, user_id)
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
