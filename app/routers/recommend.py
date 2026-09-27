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
    rating: int = 1200,
    member_id: Optional[str] = Query(default=None),
    platform: str = "cf",
    cf_divisions: Optional[list[str]] = Query(None),
    atc_divisions: Optional[list[str]] = Query(None),
    sub_filter: str = "all",
    account=Depends(require_auth),
):
    # member_id arrives as "" when the select has no selection
    try:
        member_id_int = int(member_id) if member_id else None
    except (ValueError, TypeError):
        member_id_int = None

    platform = platform if platform in ("cf", "atc") else "cf"
    sub_filter = sub_filter if sub_filter in SUB_FILTERS else "all"

    all_members = db.query(models.User).filter(models.User.user_type != "admin").order_by(models.User.username).all()

    selected_cf_divs = [d for d in (cf_divisions or DEFAULT_DIVISIONS) if d in rec.DIVISION_LABELS] \
        or DEFAULT_DIVISIONS
    selected_atc_divs = [d for d in (atc_divisions or rec.ATC_DEFAULT_DIVISIONS) if d in rec.ATC_DIVISION_LABELS] \
        or rec.ATC_DEFAULT_DIVISIONS

    total = db.query(models.CfContest).count()
    if total == 0:
        background_tasks.add_task(rec.bootstrap_cache)
    progress = rec.get_cache_progress(db)

    if platform == "cf":
        recommendations = rec.get_cf_recommendations(
            db, selected_cf_divs, rating, account=account, sub_filter=sub_filter,
        ) if total > 0 else []
    else:
        recommendations = await rec.get_atc_recommendations(
            selected_atc_divs, rating, db=db, account=account, sub_filter=sub_filter,
        )

    # link builders: every control (tab, filter, Apply) needs to keep the params it doesn't itself change
    shared = dict(rating=rating, member_id=member_id_int, cf_divisions=selected_cf_divs,
                  atc_divisions=selected_atc_divs)
    tab_urls = {
        "cf": _url(platform="cf", sub_filter=sub_filter, **shared),
        "atc": _url(platform="atc", sub_filter=sub_filter, **shared),
    }
    filter_urls = {f: _url(platform=platform, sub_filter=f, **shared) for f in SUB_FILTERS}

    response = templates.TemplateResponse(request, "recommend.html", {
        "request": request,
        "recommendations": recommendations,
        "progress": progress,
        "rating": rating,
        "member_id": member_id_int,
        "all_members": all_members,
        "platform": platform,
        "tab_urls": tab_urls,
        "sub_filter": sub_filter,
        "filter_urls": filter_urls,
        "selected_cf_divs": selected_cf_divs,
        "selected_atc_divs": selected_atc_divs,
        "cf_divisions": rec.DIVISION_LABELS,
        "atc_divisions": rec.ATC_DIVISION_LABELS,
        "bootstrapping": total == 0,
        "account": account,
    })
    db.close()  # rendered; release the transaction before a background cache build starts
    return response
