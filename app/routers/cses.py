"""The CSES import page and the endpoint the page posts to (see app/cses_import.py for what it does and why)."""
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app import audit, cses_import, models
from app.auth import require_auth
from app.database import get_db
from app.templating import make_templates

router = APIRouter(prefix="/cses", tags=["cses"])
templates = make_templates()


@router.get("", response_class=HTMLResponse)
async def cses_page(request: Request, db: Session = Depends(get_db), account=Depends(require_auth)):
    state = db.query(models.SubmissionSync).filter_by(user_id=account.id, platform=cses_import.PLATFORM).first()
    solved = db.query(models.Submission).filter_by(user_id=account.id, platform=cses_import.PLATFORM, accepted=True).count()
    return templates.TemplateResponse(request, "cses.html", {
        "request": request, "account": account, "cses_id": account.cses_id, "solved": solved,
        "last_import": state.last_synced_at if state else None,
    })


@router.post("/import")
async def import_progress(request: Request, db: Session = Depends(get_db), account=Depends(require_auth)):
    """JSON in: {"cses_id": "12345", "solved": ["1068", ...]}. JSON out: what was recorded, or {"error": ...}."""
    if account.user_type == "admin":
        raise HTTPException(status_code=403, detail="The admin account has no CSES progress to import.")
    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return JSONResponse({"error": "That request wasn't understood."}, status_code=400)
    try:
        result = await cses_import.import_solved(db, account, data.get("cses_id"), data.get("solved"))
    except cses_import.ImportRejected as rejected:
        db.commit()  # keeps the attempt time, which is what the cooldown reads; nothing else was written
        return JSONResponse({"error": rejected.message}, status_code=rejected.status)
    audit.record(db, request, "cses.import", actor=account, target_type="user", target_id=account.id,
                 target_label=account.username,
                 details={"cses_id": result.cses_id, "solved": result.solved, "new": result.new,
                          "first_import": result.baseline})
    db.commit()
    return JSONResponse({"cses_id": result.cses_id, "name": result.name, "solved": result.solved, "new": result.new,
                         "baseline": result.baseline})
