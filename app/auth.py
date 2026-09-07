import os
import hashlib
from fastapi import Request
from fastapi.responses import RedirectResponse


ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "squadforces2024")


def _session_token() -> str:
    return hashlib.sha256(ADMIN_PASSWORD.encode()).hexdigest()


def is_authenticated(request: Request) -> bool:
    return request.cookies.get("session") == _session_token()


def require_auth(request: Request):
    """Use as a dependency; redirects to /login if not authenticated."""
    if not is_authenticated(request):
        # Raise an exception that main.py catches and redirects
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Not authenticated")


def login_response(redirect_to: str = "/") -> RedirectResponse:
    response = RedirectResponse(url=redirect_to, status_code=303)
    response.set_cookie("session", _session_token(), httponly=True, samesite="lax")
    return response


def logout_response() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie("session")
    return response
