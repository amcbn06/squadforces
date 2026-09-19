import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import models, recommend as rec
from app.auth import require_auth
from app.database import get_db
from app.scraper import codeforces as cf

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/recommend", tags=["recommend"])
templates = Jinja2Templates(directory="app/templates")

templates.env.filters["ts_to_date"] = (
    lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    if ts else "?"
)

DEFAULT_DIVISIONS = ["div2", "div3", "educational"]


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
    divisions: Optional[list[str]] = Query(None),
    account=Depends(require_auth),
):
    # member_id arrives as "" when the select has no selection
    try:
        member_id_int = int(member_id) if member_id else None
    except (ValueError, TypeError):
        member_id_int = None

    all_members = db.query(models.User).filter(models.User.user_type != "admin").order_by(models.User.username).all()

    selected_divs = [d for d in (divisions or DEFAULT_DIVISIONS) if d in rec.DIVISION_LABELS]
    if not selected_divs:
        selected_divs = DEFAULT_DIVISIONS

    total = db.query(models.CfContest).count()
    if total == 0:
        background_tasks.add_task(rec.bootstrap_cache)

    progress = rec.get_cache_progress(db)
    recommendations = rec.get_recommendations(db, selected_divs, rating) if total > 0 else []

    return templates.TemplateResponse("recommend.html", {
        "request": request,
        "recommendations": recommendations,
        "progress": progress,
        "rating": rating,
        "member_id": member_id_int,
        "all_members": all_members,
        "selected_divs": selected_divs,
        "all_divisions": rec.DIVISION_LABELS,
        "bootstrapping": total == 0,
        "account": account,
    })
