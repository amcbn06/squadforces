import hashlib
from pathlib import Path

from fastapi.templating import Jinja2Templates


def _static_version() -> str:
    """Short content hash of style.css; appended to its URL so a changed file busts browser caches."""
    css = Path("static/style.css")
    return hashlib.md5(css.read_bytes()).hexdigest()[:10] if css.exists() else "0"


STATIC_VERSION = _static_version()


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory="app/templates")
    templates.env.globals["static_v"] = STATIC_VERSION
    return templates
