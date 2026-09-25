"""Invite links: one shape for both kinds.

    platform invite   group_id is None; made by the admin; lets someone create an account
    group invite      group_id is set; made by the group's owner (or the admin); lets an existing account join the
                      group. An admin-made group invite may also allow signing up, so it can bring a new person in.

A link is /invite/<token>. Only the SHA-256 of the token is stored (like a password), so the link can be shown
once, when it is made, and a leaked database doesn't leak working links. A link expires, has a use limit, and can be
revoked.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy.orm import Session

from app import models
from app.limits import INVITE_DEFAULT_DAYS, INVITE_MAX_DAYS, INVITE_MAX_USES


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def clean_token(raw: str) -> str:
    """The token from what was pasted: the token itself, or a whole invite link."""
    raw = (raw or "").strip()
    if raw.startswith(("http://", "https://")):
        raw = urlparse(raw).path
    return raw.rstrip("/").rsplit("/", 1)[-1]


def create(
    db: Session,
    *,
    created_by,
    group: Optional[models.Group] = None,
    label: str = "",
    days: int = INVITE_DEFAULT_DAYS,
    max_uses: int = 1,
    user_type: str = "user",
    allows_signup: Optional[bool] = None,
) -> tuple[models.Invite, str]:
    """Make an invite and return (invite, token). The token can't be recovered later. Days and uses are clamped."""
    days = max(1, min(int(days), INVITE_MAX_DAYS))
    max_uses = max(1, min(int(max_uses), INVITE_MAX_USES))
    token = secrets.token_urlsafe(24)
    invite = models.Invite(
        token_hash=hash_token(token),
        token_hint=token[-4:],
        label=(label or "").strip()[:100] or None,
        group_id=group.id if group else None,
        created_by_id=created_by.id,
        user_type=user_type if user_type in ("user", "student") else "user",
        allows_signup=(group is None) if allows_signup is None else bool(allows_signup),
        max_uses=max_uses,
        expires_at=datetime.utcnow() + timedelta(days=days),
    )
    db.add(invite)
    db.commit()
    return invite, token


def find(db: Session, raw: str) -> Optional[models.Invite]:
    token = clean_token(raw)
    if not token:
        return None
    return db.query(models.Invite).filter_by(token_hash=hash_token(token)).first()


def status(invite: models.Invite, now: Optional[datetime] = None) -> str:
    """"active", "revoked", "expired" or "used up" (checked in that order)."""
    now = now or datetime.utcnow()
    if invite.revoked_at is not None:
        return "revoked"
    if invite.expires_at <= now:
        return "expired"
    if invite.uses >= invite.max_uses:
        return "used up"
    return "active"


def why_unusable(invite: Optional[models.Invite]) -> Optional[str]:
    """A sentence for the person holding the link, or None if it is usable."""
    if invite is None:
        return "This invite link isn't valid."
    state = status(invite)
    return None if state == "active" else f"This invite link is no longer valid ({state})."


def redeem(db: Session, invite: models.Invite) -> bool:
    """Use one of the invite's uses. Atomic: two people racing for the last use can't both get it. The caller
    commits, so a failure later in the same request can roll the use back."""
    now = datetime.utcnow()
    changed = (
        db.query(models.Invite)
        .filter(
            models.Invite.id == invite.id,
            models.Invite.revoked_at.is_(None),
            models.Invite.expires_at > now,
            models.Invite.uses < models.Invite.max_uses,
        )
        .update({"uses": models.Invite.uses + 1}, synchronize_session=False)
    )
    return bool(changed)


def revoke(db: Session, invite: models.Invite) -> None:
    if invite.revoked_at is None:
        invite.revoked_at = datetime.utcnow()
        db.commit()


def group_has_room(group: models.Group) -> bool:
    return group.max_members is None or len(group.memberships) < group.max_members


def is_member(db: Session, group_id: int, user_id: int) -> bool:
    return db.query(models.GroupMembership).filter_by(group_id=group_id, user_id=user_id).first() is not None


def join_group(db: Session, group: models.Group, user) -> Optional[str]:
    """Add `user` to `group` (does not commit). Returns an error sentence, or None on success."""
    if user.user_type == "admin":
        return "The admin account can't be a group member."
    if is_member(db, group.id, user.id):
        return f"You are already in {group.name}."
    if not group_has_room(group):
        return f"{group.name} is full ({group.max_members} members)."
    db.add(models.GroupMembership(group_id=group.id, user_id=user.id))
    return None


def absolute_url(request, path: str) -> str:
    """A link to `path` that works from outside: PUBLIC_URL if set, else Railway's public domain over https, else
    the address the request came to."""
    base = os.getenv("PUBLIC_URL", "").rstrip("/")
    if not base and os.getenv("RAILWAY_PUBLIC_DOMAIN"):
        base = "https://" + os.environ["RAILWAY_PUBLIC_DOMAIN"]
    if not base:
        base = str(request.base_url).rstrip("/")
    return base + path


def open_registration() -> bool:
    """True where anyone may create an account without an invite (local development, the demo). Off by default."""
    return os.getenv("ALLOW_OPEN_REGISTRATION", "").lower() in ("1", "true", "yes")
