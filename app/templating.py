import hashlib
from pathlib import Path

from fastapi.templating import Jinja2Templates

from app.platforms import registry


def _static_version() -> str:
    """Short content hash of style.css; appended to its URL so a changed file busts browser caches."""
    css = Path("static/style.css")
    return hashlib.md5(css.read_bytes()).hexdigest()[:10] if css.exists() else "0"


STATIC_VERSION = _static_version()


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory="app/templates")
    templates.env.globals["static_v"] = STATIC_VERSION
    templates.env.globals["pop_notice"] = lambda request: request.session.pop("notice", None)  # shown once
    # What differs per judge is answered by its platform module (app/platforms/), not by if-chains in templates.
    templates.env.globals["platform_of"] = registry.for_item
    templates.env.globals["platform_options"] = registry.all_platforms
    templates.env.globals["item_url"] = lambda item: registry.for_item(item).item_url(item)
    templates.env.globals["problem_url"] = lambda item, cp: registry.for_item(item).problem_url(item, cp)
    return templates
