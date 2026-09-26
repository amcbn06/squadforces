from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.templating import make_templates
from app.models import Group, Invite, User, GroupMembership
from app.auth import require_auth, require_admin, can_manage_group, can_create_group
from app.limits import (
    MAX_GROUPS_PER_USER, MAX_GROUP_MEMBERS, DEFAULT_GROUP_MEMBERS,
    INVITE_DEFAULT_DAYS, INVITE_MAX_DAYS, INVITE_MAX_USES,
)
from app import activity as activity_svc
from app import leaderboard as leaderboard_svc
from app import audit
from app import invites as invite_svc

router = APIRouter(prefix="/groups", tags=["groups"])
templates = make_templates()

ADMIN_MEMBER_CEILING = 1000  # the admin isn't held to MAX_GROUP_MEMBERS, but a limit still has to be a sane number


def _check_group_access(account, group_id: int, db: Session):
    if account.user_type == "admin":
        return
    if db.query(GroupMembership).filter_by(group_id=group_id, user_id=account.id).first():
        return
    raise HTTPException(status_code=403)


def _get_managed_group(db: Session, group_id: int, account) -> Group:
    """The group, provided `account` may manage it (its owner or the admin); 404 / 403 otherwise."""
    group = db.get(Group, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    if not can_manage_group(account, group):
        raise HTTPException(status_code=403)
    return group


def _member_limit_error(account, raw: int, current_members: int) -> str:
    """Why `raw` isn't an acceptable member limit for this group ("" if it is)."""
    ceiling = ADMIN_MEMBER_CEILING if account.user_type == "admin" else MAX_GROUP_MEMBERS
    if raw < 1 or raw > ceiling:
        return f"The member limit must be between 1 and {ceiling}."
    if raw < current_members:
        return f"The group already has {current_members} members; the limit can't be lower than that."
    return ""


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
    allowed, reason = can_create_group(account, db)
    owned = db.query(Group).filter(Group.owner_id == account.id).count()
    return templates.TemplateResponse(request, "groups/list.html", {
        "request": request, "groups": groups, "account": account,
        "can_create": allowed, "create_reason": reason, "owned": owned, "max_groups": MAX_GROUPS_PER_USER,
        "hardworking": leaderboard_svc.for_platform(db), "window_days": leaderboard_svc.WINDOW_DAYS,
    })


def _form(request: Request, account, group=None, error=None, status_code=200):
    return templates.TemplateResponse(request, "groups/form.html", {
        "request": request, "group": group, "error": error, "account": account,
        "member_ceiling": ADMIN_MEMBER_CEILING if account.user_type == "admin" else MAX_GROUP_MEMBERS,
        "default_members": DEFAULT_GROUP_MEMBERS,
    }, status_code=status_code)


@router.get("/new", response_class=HTMLResponse)
async def new_group_form(request: Request, db: Session = Depends(get_db), account=Depends(require_auth)):
    allowed, reason = can_create_group(account, db)
    if not allowed:
        request.session["notice"] = reason
        return RedirectResponse("/groups/", status_code=303)
    return _form(request, account)


@router.post("/new")
async def create_group(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    hints_allowed: str = Form(""),
    notes_allowed: str = Form(""),
    max_members: int = Form(DEFAULT_GROUP_MEMBERS),
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    allowed, reason = can_create_group(account, db)
    if not allowed:
        raise HTTPException(status_code=403, detail=reason)
    name = name.strip()
    if not name:
        return _form(request, account, error="Group name is required.", status_code=422)
    error = _member_limit_error(account, max_members, 0)
    if error:
        return _form(request, account, error=error, status_code=422)
    group = Group(name=name, description=description.strip() or None, hints_allowed=bool(hints_allowed),
                  notes_allowed=bool(notes_allowed), owner_id=account.id, max_members=max_members)
    db.add(group)
    db.flush()
    if account.user_type != "admin":  # the owner is the group's first member (the admin can't be a member)
        db.add(GroupMembership(group_id=group.id, user_id=account.id))
    audit.record(db, request, "group.create", actor=account, target_type="group", target_id=group.id,
                 target_label=group.name, group_id=group.id,
                 details={"max_members": max_members, "hints_allowed": bool(hints_allowed),
                          "notes_allowed": bool(notes_allowed)})
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
    return _render_group(request, group, account, db)


def _render_group(request: Request, group: Group, account, db: Session, error=None, status_code=200):
    members = [m.user for m in group.memberships]
    manage = can_manage_group(account, group)

    leaderboard = leaderboard_svc.for_group(db, group)

    flash = request.session.get("new_invite")
    new_link = None
    if manage and flash and flash.get("group") == group.id:
        request.session.pop("new_invite")
        new_link = invite_svc.absolute_url(request, "/invite/" + flash["token"])

    return templates.TemplateResponse(request,
        "groups/detail.html",
        {
            "request": request,
            "group": group,
            "members": members,
            "leaderboard": leaderboard,
            "window_days": leaderboard_svc.WINDOW_DAYS,
            "account": account,
            "error": error,
            "can_manage": manage,
            "is_member": any(u.id == account.id for u in members),
            "invites": sorted(group.invites, key=lambda i: i.created_at, reverse=True) if manage else [],
            "invite_status": invite_svc.status,
            "new_link": new_link,
            "invite_defaults": {"days": INVITE_DEFAULT_DAYS, "max_days": INVITE_MAX_DAYS, "max_uses": INVITE_MAX_USES},
            "owner_name": group.owner.username if group.owner else "the administrator",
            "changes": audit.for_group(db, group.id, 20) if manage else [],
            "describe": audit.describe,
        },
        status_code=status_code,
    )


@router.get("/{group_id}/edit-page", response_class=HTMLResponse)
async def edit_group_page(
    request: Request, group_id: int, db: Session = Depends(get_db), account=Depends(require_auth)
):
    group = _get_managed_group(db, group_id, account)
    return _form(request, account, group=group)


@router.post("/{group_id}/edit")
async def edit_group(
    request: Request,
    group_id: int,
    name: str = Form(...),
    description: str = Form(""),
    hints_allowed: str = Form(""),
    notes_allowed: str = Form(""),
    max_members: int = Form(DEFAULT_GROUP_MEMBERS),
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    group = _get_managed_group(db, group_id, account)
    if not name.strip():
        return _form(request, account, group=group, error="Group name is required.", status_code=422)
    error = _member_limit_error(account, max_members, len(group.memberships))
    if error:
        return _form(request, account, group=group, error=error, status_code=422)
    before = {"name": group.name, "description": group.description, "hints_allowed": group.hints_allowed,
              "notes_allowed": group.notes_allowed, "max_members": group.max_members}
    group.name = name.strip()
    group.description = description.strip() or None
    group.hints_allowed = bool(hints_allowed)
    group.notes_allowed = bool(notes_allowed)
    group.max_members = max_members
    after = {"name": group.name, "description": group.description, "hints_allowed": group.hints_allowed,
             "notes_allowed": group.notes_allowed, "max_members": group.max_members}
    changed = {k: [before[k], after[k]] for k in after if before[k] != after[k]}
    if changed:
        audit.record(db, request, "group.edit", actor=account, target_type="group", target_id=group.id,
                     target_label=group.name, group_id=group.id, details=changed)
    db.commit()
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


@router.post("/{group_id}/delete")
async def delete_group(
    request: Request, group_id: int, db: Session = Depends(get_db), account=Depends(require_auth)
):
    group = _get_managed_group(db, group_id, account)
    audit.record(db, request, "group.delete", actor=account, target_type="group", target_id=group.id,
                 target_label=group.name, group_id=group.id,
                 details={"members": len(group.memberships), "assignments": len(group.assignments)})
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
    """Admin only, and it ignores the member limit: the admin can always add anyone. Everyone else brings people
    in through an invite link, which the invitee has to accept."""
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
            over_limit = bool(group.max_members and len(group.memberships) >= group.max_members)
            db.add(GroupMembership(group_id=group_id, user_id=user.id))
            audit.record(db, request, "group.member_add", actor=account, target_type="user", target_id=user.id,
                         target_label=user.username, group_id=group_id,
                         details={"group": group.name, "via": "add-member form", "over_limit": over_limit})
            db.commit()
            return RedirectResponse(f"/groups/{group_id}", status_code=303)

    return _render_group(request, group, account, db, error=error, status_code=422)


@router.post("/{group_id}/members/{user_id}/remove")
async def remove_member(
    request: Request, group_id: int, user_id: int, db: Session = Depends(get_db), account=Depends(require_auth)
):
    group = _get_managed_group(db, group_id, account)
    if user_id == group.owner_id:
        raise HTTPException(status_code=400, detail="The owner can't be removed; transfer or delete the group instead.")
    membership = db.query(GroupMembership).filter_by(group_id=group_id, user_id=user_id).first()
    if membership:
        audit.record(db, request, "group.member_remove", actor=account, target_type="user", target_id=user_id,
                     target_label=membership.user.username, group_id=group_id, details={"group": group.name})
        db.delete(membership)
        db.commit()
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


@router.post("/{group_id}/leave")
async def leave_group(group_id: int, request: Request, db: Session = Depends(get_db), account=Depends(require_auth)):
    group = db.get(Group, group_id)
    if not group:
        return HTMLResponse("Group not found", status_code=404)
    if group.owner_id == account.id:
        request.session["notice"] = "You own this group, so you can't leave it. Delete it, or ask the admin to transfer it."
        return RedirectResponse(f"/groups/{group_id}", status_code=303)
    membership = db.query(GroupMembership).filter_by(group_id=group_id, user_id=account.id).first()
    if membership:
        audit.record(db, request, "group.leave", actor=account, target_type="group", target_id=group.id,
                     target_label=group.name, group_id=group_id)
        db.delete(membership)
        db.commit()
    return RedirectResponse("/groups/", status_code=303)


@router.post("/{group_id}/owner")
async def transfer_ownership(
    request: Request,
    group_id: int,
    new_owner: str = Form(...),
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    """Admin only: hand a group to one of its members, or back to the admin."""
    group = db.get(Group, group_id)
    if not group:
        return HTMLResponse("Group not found", status_code=404)
    name = new_owner.strip()
    if name == "admin":
        audit.record(db, request, "group.owner_transfer", actor=account, target_type="group", target_id=group.id,
                     target_label=group.name, group_id=group.id,
                     details={"from": group.owner.username if group.owner else "admin", "to": "admin"})
        group.owner_id = 0
        db.commit()
        return RedirectResponse(f"/groups/{group_id}", status_code=303)
    user = db.query(User).filter_by(username=name).first()
    error = None
    if not user:
        error = f"No user found with username '{name}'."
    elif user.user_type != "user":
        error = "Only accounts of type 'user' can own groups."
    elif not any(m.user_id == user.id for m in group.memberships):
        error = f"'{name}' isn't a member of this group; add them first."
    elif user.id != group.owner_id and db.query(Group).filter(Group.owner_id == user.id).count() >= MAX_GROUPS_PER_USER:
        error = f"'{name}' already owns {MAX_GROUPS_PER_USER} groups."
    if error:
        return _render_group(request, group, account, db, error=error, status_code=422)
    audit.record(db, request, "group.owner_transfer", actor=account, target_type="group", target_id=group.id,
                 target_label=group.name, group_id=group.id,
                 details={"from": group.owner.username if group.owner else "admin", "to": user.username})
    group.owner_id = user.id
    db.commit()
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


# ── invite links for this group ──────────────────────────────────────────────────────────────────

@router.post("/{group_id}/invites")
async def create_group_invite(
    request: Request,
    group_id: int,
    label: str = Form(""),
    days: int = Form(INVITE_DEFAULT_DAYS),
    max_uses: int = Form(1),
    allows_signup: str = Form(""),
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    group = _get_managed_group(db, group_id, account)
    invite, token = invite_svc.create(
        db, created_by=account, group=group, label=label, days=days, max_uses=max_uses,
        allows_signup=bool(allows_signup) and account.user_type == "admin",  # only the admin can bring in new accounts
    )
    audit.record(db, request, "invite.create", actor=account, target_type="invite", target_id=invite.id,
                 target_label=invite.label or ("..." + invite.token_hint), group_id=group.id,
                 details={"group": group.name, "max_uses": invite.max_uses,
                          "expires": invite.expires_at.isoformat(timespec="minutes"),
                          "allows_signup": invite.allows_signup}, commit=True)
    request.session["new_invite"] = {"token": token, "group": group.id}
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


@router.post("/{group_id}/invites/{invite_id}/revoke")
async def revoke_group_invite(
    request: Request, group_id: int, invite_id: int, db: Session = Depends(get_db), account=Depends(require_auth)
):
    group = _get_managed_group(db, group_id, account)
    invite = db.get(Invite, invite_id)
    if invite and invite.group_id == group_id:
        audit.record(db, request, "invite.revoke", actor=account, target_type="invite", target_id=invite.id,
                     target_label=invite.label or ("..." + invite.token_hint), group_id=group_id,
                     details={"group": group.name, "uses": invite.uses})
        invite_svc.revoke(db, invite)
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

    activity = await activity_svc.user_activity(db, user)
    return JSONResponse(activity)
