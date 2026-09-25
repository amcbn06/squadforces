"""The audit log: who did what, and when.

Every security-relevant action writes one `audit_events` row: signing in (and failing to), account and group
changes, membership changes, invite links, ownership transfers, refused access. The row is added to the same
database session as the action itself, so it commits (or rolls back) together with it: a change can't exist without
its record, and a change that didn't happen leaves none. Events that change nothing (a failed login, a refused
request) commit on their own.

Rows carry a snapshot of the actor's name and of the target's label, and are not linked to users or groups, so the
history survives their deletion. Passwords, tokens and other secrets are never written (see `_scrub`).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

from app import models

logger = logging.getLogger(__name__)

RETENTION_DAYS = 400  # older events are deleted (they include IP addresses)

# action -> what the admin page shows. The dot-prefix groups them for the filter ("group.", "login.", ...).
ACTION_LABELS: dict[str, str] = {
    "login.success": "Signed in",
    "login.failed": "Failed sign-in",
    "login.blocked": "Sign-in blocked (too many failures)",
    "logout": "Signed out",
    "password.change": "Changed own password",
    "account.register": "Created an account",
    "profile.edit": "Edited a profile",
    "user.create": "Admin created a user",
    "user.edit": "Admin edited a user",
    "user.delete": "Admin deleted a user",
    "group.create": "Created a group",
    "group.edit": "Edited a group",
    "group.delete": "Deleted a group",
    "group.member_add": "Admin added a member",
    "group.member_remove": "Removed a member",
    "group.leave": "Left a group",
    "group.join": "Joined a group with an invite link",
    "group.owner_transfer": "Transferred group ownership",
    "invite.create": "Created an invite link",
    "invite.revoke": "Revoked an invite link",
    "assignment.create": "Created an assignment",
    "assignment.delete": "Deleted an assignment",
    "item.add": "Added contests or problems",
    "item.delete": "Removed a contest or problem",
    "access.denied": "Refused: no permission",
}

# What a group's owner may see about their own group (never IP addresses, never account-level events).
GROUP_ACTIONS = tuple(a for a in ACTION_LABELS if a.startswith(("group.", "invite.", "assignment.", "item.")))

_SECRET_WORDS = ("password", "token", "secret", "hash", "cookie")


def client_ip(request) -> Optional[str]:
    """The caller's address. Behind Railway's proxy the peer is the proxy, so its X-Forwarded-For header is used;
    that header can be forged by a client talking to the app directly, so treat the IP as a lead, not proof."""
    if request is None:
        return None
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45] or None
    return (request.client.host if request.client else None)


def _scrub(details: Optional[dict[str, Any]]) -> Optional[str]:
    """Details as compact JSON, without anything that looks like a secret."""
    if not details:
        return None
    clean = {k: v for k, v in details.items() if not any(w in str(k).lower() for w in _SECRET_WORDS)}
    return json.dumps(clean, sort_keys=True, default=str, ensure_ascii=False)[:2000] or None


def record(
    db: Session,
    request,
    action: str,
    *,
    actor: Any = "session",
    target_type: Optional[str] = None,
    target_id: Optional[int] = None,
    target_label: Optional[str] = None,
    group_id: Optional[int] = None,
    details: Optional[dict[str, Any]] = None,
    ok: bool = True,
    commit: bool = False,
) -> None:
    """Write one audit event. `actor` is a User, None (nobody: a failed sign-in) or "session" (whoever the request
    is signed in as). Add it before the action's own commit so both land together, or pass commit=True when there
    is no other change to commit. Never raises: a broken audit must not break the request."""
    try:
        if actor == "session":
            uid = request.session.get("user_id") if request is not None else None
            actor = db.get(models.User, uid) if uid is not None else None
        db.add(models.AuditEvent(
            actor_id=actor.id if actor is not None else None,
            actor_name=actor.username if actor is not None else None,
            action=action,
            target_type=target_type,
            target_id=target_id,
            target_label=(target_label or "")[:200] or None,
            group_id=group_id,
            details=_scrub(details),
            ok=ok,
            ip=client_ip(request),
            user_agent=((request.headers.get("user-agent", "") if request is not None else "")[:200] or None),
        ))
        if commit:
            db.commit()
    except Exception:
        logger.warning("Could not write audit event %s", action, exc_info=True)
        try:
            db.rollback() if commit else None
        except Exception:
            pass


def search(
    db: Session,
    *,
    action: str = "",
    actor: str = "",
    group_id: Optional[int] = None,
    days: Optional[int] = None,
    only_failures: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[models.AuditEvent], int]:
    """(events newest first, total matching). `action` matches exactly, or as a prefix when it ends with a dot."""
    q = db.query(models.AuditEvent)
    if action:
        q = q.filter(models.AuditEvent.action.like(action + "%")) if action.endswith(".") \
            else q.filter(models.AuditEvent.action == action)
    if actor.strip():
        q = q.filter(models.AuditEvent.actor_name == actor.strip())
    if group_id is not None:
        q = q.filter(models.AuditEvent.group_id == group_id)
    if days:
        q = q.filter(models.AuditEvent.at >= datetime.utcnow() - timedelta(days=days))
    if only_failures:
        q = q.filter(models.AuditEvent.ok.is_(False))
    total = q.count()
    rows = q.order_by(models.AuditEvent.at.desc(), models.AuditEvent.id.desc()).offset(offset).limit(limit).all()
    return rows, total


def for_group(db: Session, group_id: int, limit: int = 20) -> list[models.AuditEvent]:
    """The recent changes to one group, as its owner may see them."""
    return (
        db.query(models.AuditEvent)
        .filter(models.AuditEvent.group_id == group_id, models.AuditEvent.action.in_(GROUP_ACTIONS))
        .order_by(models.AuditEvent.at.desc(), models.AuditEvent.id.desc())
        .limit(limit)
        .all()
    )


def describe(event: models.AuditEvent) -> str:
    """One readable line: "Removed a member: bob (reason=...)"."""
    text = ACTION_LABELS.get(event.action, event.action)
    if event.target_label:
        text += f": {event.target_label}"
    if event.details:
        try:
            extra = ", ".join(f"{k}={v}" for k, v in json.loads(event.details).items())
        except ValueError:
            extra = event.details
        if extra:
            text += f" ({extra})"
    return text


def prune(db: Session, days: int = RETENTION_DAYS) -> int:
    """Delete events older than `days`. Returns how many."""
    n = db.query(models.AuditEvent).filter(models.AuditEvent.at < datetime.utcnow() - timedelta(days=days)).delete()
    db.commit()
    return n
