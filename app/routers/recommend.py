from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import models, recommend as rec
from app.auth import require_auth
from app.database import get_db

router = APIRouter(prefix="/recommend", tags=["recommend"])
templates = Jinja2Templates(directory="app/templates")

templates.env.filters["ts_to_date"] = (
    lambda ts: datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
    if ts else "?"
)

DEFAULT_DIVISIONS = ["div2", "div3", "educational"]


@router.get("", response_class=HTMLResponse)
async def recommend_page(
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
    rating: int = 1200,
    member_id: Optional[str] = Query(default=None),
    divisions: Optional[list[str]] = Query(None),
):
    # member_id arrives as "" when the select has no selection
    try:
        member_id_int = int(member_id) if member_id else None
    except (ValueError, TypeError):
        member_id_int = None

    all_members = db.query(models.User).order_by(models.User.display_name).all()

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
    })
