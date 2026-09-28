import logging
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from sqlalchemy.orm import Session

from app import models, recommend as rec
from app.auth import require_auth
from app.database import get_db
from app.templating import make_templates
from app.scraper import codeforces as cf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recommend", tags=["recommend"])
templates = make_templates()

templates.env.filters["ts_to_date"] = (
    lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    if ts else "?"
)

DEFAULT_DIVISIONS = ["div2", "div3", "educational"]
SUB_FILTERS = ("all", "none", "attempted", "solved")
# More than a top-5 glance: with each card now showing solve progress, a longer, scrollable list lets the viewer
# skim past ones already done instead of re-filtering to find the next fresh one.
TOP_N = 20


def _url(**params) -> str:
    """A /recommend link carrying only the given params (division lists included as repeated keys)."""
    pairs: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None or value == "":
            continue
        if isinstance(value, (list, tuple)):
            pairs.extend((key, v) for v in value)
        else:
            pairs.append((key, str(value)))
    return "/recommend?" + urlencode(pairs)


@router.get("/debug", response_class=PlainTextResponse)
async def recommend_debug(
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    """Diagnostic endpoint: shows cache state and runs one live prefetch."""
    lines = []

    total = db.query(models.CfContest).count()
    fetched_count = (
        db.query(models.CfContest)
        .filter(models.CfContest.problems_fetched == True)  # noqa: E712
        .count()
    )
    lines.append(f"CF contests in DB: {total}")
    lines.append(f"  problems_fetched=True: {fetched_count}")
    lines.append(f"  remaining: {total - fetched_count}")
    lines.append("")

    # Division breakdown
    from sqlalchemy import func
    div_counts = (
        db.query(models.CfContest.division, func.count())
        .group_by(models.CfContest.division)
        .order_by(func.count().desc())
        .all()
    )
    lines.append("Division breakdown:")
    for div, cnt in div_counts:
        lines.append(f"  {div or 'None'}: {cnt}")
    lines.append("")

    uncached = (
        db.query(models.CfContest)
        .filter(models.CfContest.problems_fetched == False)  # noqa: E712
        .order_by(models.CfContest.start_time.desc())
        .limit(3)
        .all()
    )
    if not uncached:
        lines.append("No uncached contests — cache is complete or DB is empty.")
    else:
        lines.append("Next 3 contests to fetch:")
        for c in uncached:
            lines.append(f"  id={c.id}  division={c.division}  name={c.name}")
        lines.append("")

        # Run one live fetch for the first contest
        cid = uncached[0].id
        lines.append(f"Running get_contest_info({cid}) now…")
        try:
            info = await cf.get_contest_info(str(cid))
            problems = info.get("problems", [])
            lines.append(f"  title: {info.get('title', '(empty)')}")
            lines.append(f"  problems returned: {len(problems)}")
            for p in problems[:5]:
                lines.append(f"    index={p.get('index')} rating={p.get('rating')} name={p.get('name', '')[:40]}")
        except Exception as exc:
            lines.append(f"  ERROR: {exc}")

    return "\n".join(lines)


@router.get("", response_class=HTMLResponse)
async def recommend_page(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    rating: Optional[str] = Query(default=None),
    member_id: Optional[str] = Query(default=None),
    platform: str = "cf",
    cf_divisions: Optional[list[str]] = Query(None),
    atc_divisions: Optional[list[str]] = Query(None),
    sub_filter: str = "all",
    account=Depends(require_auth),
):
    platform = platform if platform in ("cf", "atc") else "cf"
    sub_filter = sub_filter if sub_filter in SUB_FILTERS else "all"

    # Only members of the viewer's own kind (a user sees users, a student sees students — the admin sees both,
    # same convention as leaderboard.for_platform()), and only ones with a handle linked to this platform.
    handle_col = models.User.codeforces_handle if platform == "cf" else models.User.atcoder_handle
    members_q = db.query(models.User).filter(handle_col.isnot(None), handle_col != "")
    members_q = members_q.filter(models.User.user_type == account.user_type) \
        if account.user_type in ("user", "student") else members_q.filter(models.User.user_type != "admin")
    all_members = members_q.order_by(models.User.username).all()
    eligible_ids = {m.id for m in all_members}

    # member_id: "" (the select's blank option, explicitly submitted) means "nobody, on purpose" and is kept as
    # None. Anything else invalid — missing entirely, or an id that isn't eligible here (e.g. carried over from
    # the other platform's tab) — falls back to the viewer themselves, if they qualify.
    member_id_explicit = member_id is not None
    try:
        member_id_int = int(member_id) if member_id else None
    except (ValueError, TypeError):
        member_id_int = None
    if member_id_int is not None and member_id_int not in eligible_ids:
        member_id_int, member_id_explicit = None, False
    if not member_id_explicit:
        member_id_int = account.id if account.id in eligible_ids else None
    selected_member = next((m for m in all_members if m.id == member_id_int), None)

    # rating: a typed value always wins, even with a member also selected. Otherwise it follows the selected
    # member's own rating (see recommend.effective_rating — 800 if they're linked but unrated), or 1200 with
    # nobody selected. Parsed leniently: malformed input never turns into a 422.
    try:
        rating_val = int(rating) if rating not in (None, "") else None
    except (ValueError, TypeError):
        rating_val = None
    if rating_val is None:
        rating_val = rec.effective_rating(selected_member, platform) if selected_member else 1200

    # The submission filter always checks the *selected* member's own history — defaulting to the viewer's own
    # only when nobody is picked, never the viewer's when a different member is chosen.
    filter_account = selected_member or account

    selected_cf_divs = [d for d in (cf_divisions or DEFAULT_DIVISIONS) if d in rec.DIVISION_LABELS] \
        or DEFAULT_DIVISIONS
    selected_atc_divs = [d for d in (atc_divisions or rec.ATC_DEFAULT_DIVISIONS) if d in rec.ATC_DIVISION_LABELS] \
        or rec.ATC_DEFAULT_DIVISIONS

    cf_total = db.query(models.CfContest).count()
    if cf_total == 0:
        background_tasks.add_task(rec.bootstrap_cache)
    atc_total = db.query(models.AtcContest).count()
    if atc_total == 0:
        background_tasks.add_task(rec.bootstrap_atc_cache)

    if platform == "cf":
        progress = rec.get_cache_progress(db)
        recommendations = rec.get_cf_recommendations(
            db, selected_cf_divs, rating_val, TOP_N, account=filter_account, sub_filter=sub_filter,
        ) if cf_total > 0 else []
        bootstrapping = cf_total == 0
    else:
        progress = rec.get_atc_cache_progress(db)
        recommendations = rec.get_atc_recommendations(
            db, selected_atc_divs, rating_val, TOP_N, account=filter_account, sub_filter=sub_filter,
        ) if atc_total > 0 else []
        bootstrapping = atc_total == 0

    # Link builders. A tab switch starts that platform fresh (its own default member/rating then apply); every
    # other control — Apply, a filter button — stays within the current platform and keeps what's chosen.
    within_platform = dict(rating=rating_val, member_id=member_id_int, cf_divisions=selected_cf_divs,
                            atc_divisions=selected_atc_divs)
    tab_urls = {"cf": _url(platform="cf"), "atc": _url(platform="atc")}
    filter_urls = {f: _url(platform=platform, sub_filter=f, **within_platform) for f in SUB_FILTERS}

    response = templates.TemplateResponse(request, "recommend.html", {
        "request": request,
        "recommendations": recommendations,
        "progress": progress,
        "rating": rating_val,
        "member_id": member_id_int,
        "all_members": all_members,
        "member_ratings": {m.id: rec.effective_rating(m, platform) for m in all_members},
        "platform": platform,
        "tab_urls": tab_urls,
        "sub_filter": sub_filter,
        "filter_urls": filter_urls,
        "selected_cf_divs": selected_cf_divs,
        "selected_atc_divs": selected_atc_divs,
        "cf_divisions": rec.DIVISION_LABELS,
        "atc_divisions": rec.ATC_DIVISION_LABELS,
        "bootstrapping": bootstrapping,
        "account": account,
        "filter_account": filter_account,
    })
    db.close()  # rendered; release the transaction before a background cache build starts
    return response
