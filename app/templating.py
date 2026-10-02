import hashlib
import re
from datetime import datetime
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from app.platforms import registry
from app import auth


def _static_version() -> str:
    """Short content hash of style.css; appended to its URL so a changed file busts browser caches."""
    css = Path("static/style.css")
    return hashlib.md5(css.read_bytes()).hexdigest()[:10] if css.exists() else "0"


STATIC_VERSION = _static_version()


def time_ago(moment) -> str:
    """"3 hours ago" for a UTC datetime ("" for None). Coarse on purpose."""
    if moment is None:
        return ""
    seconds = max(0, int((datetime.utcnow() - moment).total_seconds()))
    for unit, size in (("year", 31536000), ("month", 2592000), ("day", 86400), ("hour", 3600), ("minute", 60)):
        if seconds >= size:
            n = seconds // size
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return "just now"


_URL = re.compile(r"https?://[^\s<>\"']+")


def linkify(text) -> Markup:
    """`text` with each http(s) URL in it made a link that opens in a new tab. URLs are found in the raw text and
    every piece (links included) is escaped on its own, so nothing a user types can become markup; trailing
    punctuation (a full stop after a link) stays outside it."""
    text = text or ""
    out, pos = [], 0
    for m in _URL.finditer(text):
        url, tail = m.group(0), ""
        while url and url[-1] in ".,;:!?)]":
            url, tail = url[:-1], url[-1] + tail
        out.append(str(escape(text[pos:m.start()])))
        out.append(f'<a href="{escape(url)}" target="_blank" rel="noopener noreferrer">{escape(url)}</a>'
                   f'{escape(tail)}')
        pos = m.end()
    out.append(str(escape(text[pos:])))
    return Markup("".join(out))


def make_templates() -> Jinja2Templates:
    templates = Jinja2Templates(directory="app/templates")
    templates.env.filters["linkify"] = linkify
    templates.env.globals["static_v"] = STATIC_VERSION
    templates.env.globals["time_ago"] = time_ago
    templates.env.globals["can_log_in"] = auth.can_log_in
    templates.env.globals["pop_notice"] = lambda request: request.session.pop("notice", None)  # shown once
    # What differs per judge is answered by its platform module (app/platforms/), not by if-chains in templates.
    templates.env.globals["platform_of"] = registry.for_item
    templates.env.globals["platform_options"] = registry.all_platforms
    templates.env.globals["item_url"] = lambda item: registry.for_item(item).item_url(item)
    templates.env.globals["problem_url"] = lambda item, cp: registry.for_item(item).problem_url(item, cp)
    return templates
