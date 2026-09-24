import hashlib
import hmac
import secrets
import time
from functools import lru_cache

from fastapi import Request, Depends, HTTPException
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.database import get_db


MIN_PASSWORD_LENGTH = 8


def safe_next(next_url: str | None) -> str:
    """Where to send someone after login: `next_url` if it is a path on this site, else "/".
    Refuses anything that could leave the site ("//evil.com", a path starting with a backslash, "https://...", control characters)."""
    if not next_url or not next_url.startswith("/") or next_url.startswith(("//", "/\\")):
        return "/"
    if any(ord(c) < 32 or c == "\\" for c in next_url):
        return "/"
    return next_url


class LoginThrottle:
    """Slows password guessing. After `max_failures` wrong passwords for one username within `window` seconds, further
    attempts for it are refused for a while (60 s, doubling with every further `max_failures` failures, capped at an
    hour); a refused attempt is not counted. A successful sign-in clears the record. Unknown usernames are treated
    the same, so it doesn't reveal which names exist. In memory, per process."""

    def __init__(self, max_failures: int = 5, window: float = 600, base_lock: float = 60, max_lock: float = 3600):
        self.max_failures, self.window, self.base_lock, self.max_lock = max_failures, window, base_lock, max_lock
        self._failures: dict[str, list[float]] = {}

    def _recent(self, key: str, now: float) -> list[float]:
        recent = [t for t in self._failures.get(key, []) if now - t < max(self.window, self.max_lock)]
        if recent:
            self._failures[key] = recent
        else:
            self._failures.pop(key, None)
        return recent

    def blocked_for(self, key: str, now: float | None = None) -> int:
        """Seconds left before `key` may try again (0 = may try now)."""
        now = time.time() if now is None else now
        recent = self._recent(key, now)
        count = len([t for t in recent if now - t < self.window])
        if count < self.max_failures:
            return 0
        lock = min(self.max_lock, self.base_lock * 2 ** (count // self.max_failures - 1))
        return max(0, int(recent[-1] + lock - now + 0.999))

    def record_failure(self, key: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._recent(key, now)
        self._failures.setdefault(key, []).append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)

    def clear(self) -> None:
        self._failures.clear()


login_throttle = LoginThrottle()

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
