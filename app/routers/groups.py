from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Group, User, GroupMembership, Assignment, AssignmentItem, Result, ProblemResult
from app.auth import require_auth
from app.scraper import codeforces as cf

router = APIRouter(prefix="/groups", tags=["groups"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
async def list_groups(request: Request, db: Session = Depends(get_db), _=Depends(require_auth)):
    groups = db.query(Group).order_by(Group.created_at.desc()).all()
    return templates.TemplateResponse("groups/list.html", {"request": request, "groups": groups})


@router.get("/new", response_class=HTMLResponse)
async def new_group_form(request: Request, _=Depends(require_auth)):
    return templates.TemplateResponse("groups/form.html", {"request": request, "group": None, "error": None})


@router.post("/new")
async def create_group(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    name = name.strip()
    if not name:
        return templates.TemplateResponse(
            "groups/form.html",
            {"request": request, "group": None, "error": "Group name is required."},
            status_code=422,
        )
    group = Group(name=name, description=description.strip() or None)
    db.add(group)
    db.commit()
    return RedirectResponse(f"/groups/{group.id}", status_code=303)


@router.get("/{group_id}", response_class=HTMLResponse)
async def group_detail(request: Request, group_id: int, db: Session = Depends(get_db), _=Depends(require_auth)):
    group = db.get(Group, group_id)
    if not group:
        return HTMLResponse("Group not found", status_code=404)
    members = [m.user for m in group.memberships]

    # ── 30-day leaderboard ─────────────────────────────────────────────────────
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
            result = (
                db.query(Result)
                .filter_by(assignment_item_id=item.id, user_id=user.id)
                .first()
            )
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
        {"request": request, "group": group, "members": members, "leaderboard": leaderboard},
    )


@router.get("/{group_id}/edit-page", response_class=HTMLResponse)
async def edit_group_page(request: Request, group_id: int, db: Session = Depends(get_db), _=Depends(require_auth)):
    group = db.get(Group, group_id)
    if not group:
        return HTMLResponse("Group not found", status_code=404)
    return templates.TemplateResponse("groups/form.html", {"request": request, "group": group, "error": None})


@router.post("/{group_id}/edit")
async def edit_group(
    request: Request,
    group_id: int,
    name: str = Form(...),
    description: str = Form(""),
    db: Session = Depends(get_db),
    _=Depends(require_auth),
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
    group_id: int, db: Session = Depends(get_db), _=Depends(require_auth)
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
    cf_handle: str = Form(...),
    atcoder_handle: str = Form(""),
    display_name: str = Form(""),
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    group = db.get(Group, group_id)
    if not group:
        return HTMLResponse("Group not found", status_code=404)

    cf_handle = cf_handle.strip()
    error = None

    # Validate handle against CF API
    user_info = await cf.validate_handle(cf_handle)
    if not user_info:
        error = f"Codeforces handle '{cf_handle}' does not exist."
    else:
        # Find or create user
        user = db.query(User).filter_by(codeforces_handle=cf_handle).first()
        if not user:
            name = display_name.strip() or user_info.get("handle", cf_handle)
            user = User(
                display_name=name,
                codeforces_handle=cf_handle,
                cf_rating=user_info.get("rating"),
                cf_rank=user_info.get("rank"),
                atcoder_handle=atcoder_handle.strip() or None,
            )
            db.add(user)
            db.flush()
        elif atcoder_handle.strip():
            user.atcoder_handle = atcoder_handle.strip()

        # Add to group if not already
        existing = db.query(GroupMembership).filter_by(group_id=group_id, user_id=user.id).first()
        if existing:
            error = f"{cf_handle} is already in this group."
        else:
            db.add(GroupMembership(group_id=group_id, user_id=user.id))
            db.commit()
            return RedirectResponse(f"/groups/{group_id}", status_code=303)

    db.rollback()
    members = [m.user for m in group.memberships]
    return templates.TemplateResponse(
        "groups/detail.html",
        {"request": request, "group": group, "members": members, "error": error},
        status_code=422,
    )


@router.post("/{group_id}/members/{user_id}/edit")
async def edit_member(
    group_id: int,
    user_id: int,
    display_name: str = Form(""),
    atcoder_handle: str = Form(""),
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    user = db.get(User, user_id)
    if user:
        if display_name.strip():
            user.display_name = display_name.strip()
        user.atcoder_handle = atcoder_handle.strip() or None
        db.commit()
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


@router.post("/{group_id}/members/{user_id}/remove")
async def remove_member(
    group_id: int, user_id: int, db: Session = Depends(get_db), _=Depends(require_auth)
):
    membership = db.query(GroupMembership).filter_by(group_id=group_id, user_id=user_id).first()
    if membership:
        db.delete(membership)
        db.commit()
    return RedirectResponse(f"/groups/{group_id}", status_code=303)
