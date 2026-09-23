import hashlib
import secrets
from fastapi import Request, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db


MIN_PASSWORD_LENGTH = 6


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"{salt}:{h}"


def verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split(":", 1)
        return hashlib.sha256((salt + password).encode()).hexdigest() == h
    except Exception:
        return False


def require_auth(request: Request, db: Session = Depends(get_db)):
    from app.models import User
    user_id = request.session.get("user_id")
    if user_id is None:  # explicit None check — id=0 (admin) is valid
        raise HTTPException(status_code=401, detail="Not authenticated")
    user = db.query(User).filter(User.id == user_id).first()
    # A session with no stored version predates session versioning, so it counts as version 0 and stays valid
    # until that user's password is next changed.
    if not user or request.session.get("sv", 0) != user.session_version:
        # Drop the dead cookie: /login and / redirect anyone who still has a user_id, which would loop forever.
        request.session.clear()
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


def require_admin(user=Depends(require_auth)):
    if user.user_type != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


def can_edit_user(current_user, target_user) -> bool:
    if current_user.user_type == "admin":
        return True
    return current_user.id == target_user.id


def can_delete_item(user, item) -> bool:
    if user.user_type == "admin":
        return True
    # user and student: only remove their own items
    return item.created_by_id is not None and item.created_by_id == user.id
