import hashlib
import hmac
import secrets
from functools import lru_cache

from fastapi import Request, Depends, HTTPException
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import get_db


MIN_PASSWORD_LENGTH = 6

# PBKDF2-HMAC-SHA256 at the OWASP-recommended work factor. Stored as
#   pbkdf2_sha256$<iterations>$<salt hex>$<hash hex>
# Raise PBKDF2_ITERATIONS over time: needs_rehash() upgrades a hash at the owner's next login.
PBKDF2_ITERATIONS = 600_000
_SCHEME = "pbkdf2_sha256"


def _pbkdf2(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    return f"{_SCHEME}${PBKDF2_ITERATIONS}${salt.hex()}${_pbkdf2(password, salt, PBKDF2_ITERATIONS).hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Check a password against a stored hash in the current format, or in the legacy one
    ("<salt>:<sha256(salt + password)>") that accounts created before the upgrade still have."""
    try:
        if stored.startswith(_SCHEME + "$"):
            _, iterations, salt_hex, hash_hex = stored.split("$")
            actual = _pbkdf2(password, bytes.fromhex(salt_hex), int(iterations))
            return hmac.compare_digest(actual, bytes.fromhex(hash_hex))
        salt, legacy_hash = stored.split(":", 1)
        return hmac.compare_digest(hashlib.sha256((salt + password).encode("utf-8")).hexdigest(), legacy_hash)
    except Exception:
        return False


def needs_rehash(stored: str) -> bool:
    """True for a legacy hash or one made with fewer iterations than the current setting."""
    if not stored.startswith(_SCHEME + "$"):
        return True
    try:
        return int(stored.split("$")[1]) < PBKDF2_ITERATIONS
    except (IndexError, ValueError):
        return True


@lru_cache(maxsize=1)
def dummy_hash() -> str:
    """Verified against when a login names a user that doesn't exist, so it costs as long as a real one."""
    return hash_password(secrets.token_hex(16))


# A hash takes ~0.4 s of CPU. Inside an async route that would stall every other request, so run it in a thread
# (OpenSSL releases the GIL, so it really does run alongside them).
async def hash_password_async(password: str) -> str:
    return await run_in_threadpool(hash_password, password)


async def verify_password_async(password: str, stored: str) -> bool:
    return await run_in_threadpool(verify_password, password, stored)


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
