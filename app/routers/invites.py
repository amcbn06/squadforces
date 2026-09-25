"""Invite links: the /invite/<token> landing page, joining a group with one, and the admin's list of invites."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app import invites
from app.auth import optional_user, require_admin, require_auth
from app.database import get_db
from app.limits import INVITE_DEFAULT_DAYS, INVITE_MAX_DAYS, INVITE_MAX_USES
from app.models import Group, Invite
from app.templating import make_templates

router = APIRouter(tags=["invites"])
templates = make_templates()


def _landing(request: Request, account, *, invite=None, error="", status_code=200):
    return templates.TemplateResponse(request, "invite/landing.html", {
        "request": request, "account": account, "invite": invite, "error": error,
        "token": request.path_params.get("token", ""),
    }, status_code=status_code)


@router.get("/invite/{token}", response_class=HTMLResponse)
async def invite_landing(request: Request, token: str, db: Session = Depends(get_db), account=Depends(optional_user)):
    invite = invites.find(db, token)
    problem = invites.why_unusable(invite)
    if problem:
        return _landing(request, account, error=problem, status_code=404)
    if invite.group_id is None:  # a platform invite: it is for creating an account
        if account:
            return _landing(request, account, invite=invite,
                            error="You already have an account, so this link has nothing more for you.")
        return RedirectResponse(f"/register?invite={token}", status_code=303)
    error = ""
    if account:
        if invites.is_member(db, invite.group_id, account.id):
            error = f"You are already in {invite.group.name}."
        elif not invites.group_has_room(invite.group):
            error = f"{invite.group.name} is full ({invite.group.max_members} members)."
        elif account.user_type == "admin":
            error = "The admin account can't be a group member; add people from the group page instead."
    return _landing(request, account, invite=invite, error=error)


@router.post("/invite/{token}/join")
async def join_with_invite(request: Request, token: str, db: Session = Depends(get_db), account=Depends(require_auth)):
    invite = invites.find(db, token)
    problem = invites.why_unusable(invite)
    if problem or invite.group_id is None:
        return _landing(request, account, error=problem or "This link isn't for joining a group.", status_code=404)
    group = invite.group
    error = invites.join_group(db, group, account)
    if error:
        db.rollback()
        return _landing(request, account, invite=invite, error=error, status_code=409)
    if not invites.redeem(db, invite):  # the last use went to someone else a moment ago
        db.rollback()
        return _landing(request, account, error="This invite link is no longer valid.", status_code=404)
    db.commit()
    return RedirectResponse(f"/groups/{group.id}", status_code=303)


# ── the admin's list (platform invites and every group's invites) ────────────────────────────────

def _flash_new_link(request: Request, group_id):
    """The link just created, once: it is only shown when made, since only its hash is stored."""
    flash = request.session.get("new_invite")
    if flash and flash.get("group") == group_id:
        request.session.pop("new_invite")
        return invites.absolute_url(request, "/invite/" + flash["token"])
    return None


@router.get("/admin/invites", response_class=HTMLResponse)
async def admin_invites(request: Request, db: Session = Depends(get_db), account=Depends(require_admin)):
    rows = db.query(Invite).order_by(Invite.created_at.desc()).all()
    return templates.TemplateResponse(request, "admin/invites.html", {
        "request": request, "account": account, "invites": rows, "status": invites.status,
        "groups": db.query(Group).order_by(Group.name).all(), "new_link": _flash_new_link(request, None),
        "defaults": {"days": INVITE_DEFAULT_DAYS, "max_days": INVITE_MAX_DAYS, "max_uses": INVITE_MAX_USES},
    })


@router.post("/admin/invites/new")
async def admin_create_invite(
    request: Request,
    label: str = Form(""),
    days: int = Form(INVITE_DEFAULT_DAYS),
    max_uses: int = Form(1),
    user_type: str = Form("user"),
    group_id: str = Form(""),
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    group = db.get(Group, int(group_id)) if group_id.strip().isdigit() else None
    invite, token = invites.create(
        db, created_by=account, group=group, label=label, days=days, max_uses=max_uses, user_type=user_type,
        allows_signup=True,  # an admin's link from this page is for making an account (and, if chosen, joining a group)
    )
    request.session["new_invite"] = {"token": token, "group": None}
    return RedirectResponse("/admin/invites", status_code=303)


@router.post("/admin/invites/{invite_id}/revoke")
async def admin_revoke_invite(invite_id: int, db: Session = Depends(get_db), account=Depends(require_admin)):
    invite = db.get(Invite, invite_id)
    if invite:
        invites.revoke(db, invite)
    return RedirectResponse("/admin/invites", status_code=303)
