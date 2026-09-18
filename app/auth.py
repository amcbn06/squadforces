import hashlib
import secrets
from fastapi import Request, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db


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
    from app.models import Account
    account_id = request.session.get("account_id")
    if not account_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    account = db.get(Account, account_id)
    if not account:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return account


def require_admin(account=Depends(require_auth)):
    if account.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return account


def can_edit_user(account, user) -> bool:
    """Returns True if this account is allowed to edit the given User's profile."""
    if account.role == "admin":
        return True
    # A user can edit their own profile if their account username matches their CF handle
    return account.username.lower() == user.codeforces_handle.lower()


def can_delete_item(account, item) -> bool:
    """Returns True if this account is allowed to delete the given AssignmentItem."""
    if account.role in ("admin", "user"):
        return True
    # student: only items they added (NULL created_by_id = admin-owned, cannot delete)
    return item.created_by_id is not None and item.created_by_id == account.id
