from fastapi import APIRouter, Depends, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Group, Assignment, AssignmentItem, Result, ProblemResult
from app.auth import require_auth
from app.scraper import codeforces as cf
from app.scraper import atcoder as ac
from app import sync as sync_svc

router = APIRouter(prefix="/assignments", tags=["assignments"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/new", response_class=HTMLResponse)
async def new_assignment_form(
    request: Request,
    group_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    group = db.get(Group, group_id)
    if not group:
        return HTMLResponse("Group not found", status_code=404)
    return templates.TemplateResponse(
        "assignments/form.html", {"request": request, "group": group, "error": None}
    )


@router.post("/new")
async def create_assignment(
    request: Request,
    group_id: int = Form(...),
    title: str = Form(...),
    week_start_date: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    from datetime import date
    group = db.get(Group, group_id)
    if not group:
        return HTMLResponse("Group not found", status_code=404)

    parsed_date = None
    if week_start_date.strip():
        try:
            parsed_date = date.fromisoformat(week_start_date.strip())
        except ValueError:
            pass

    assignment = Assignment(
        group_id=group_id,
        title=title.strip(),
        week_start_date=parsed_date,
        description=description.strip() or None,
    )
    db.add(assignment)
    db.commit()
    return RedirectResponse(f"/assignments/{assignment.id}", status_code=303)


@router.get("/{assignment_id}", response_class=HTMLResponse)
async def assignment_detail(
    request: Request,
    assignment_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        return HTMLResponse("Assignment not found", status_code=404)

    group = assignment.group
    members = [m.user for m in group.memberships]
    items = assignment.items

    # Build matrix data
    matrix = _build_matrix(items, members, db)

    return templates.TemplateResponse(
        "assignments/detail.html",
        {
            "request": request,
            "assignment": assignment,
            "group": group,
            "members": members,
            "items": items,
            "matrix": matrix,
        },
    )


@router.post("/{assignment_id}/delete")
async def delete_assignment(
    assignment_id: int, db: Session = Depends(get_db), _=Depends(require_auth)
):
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        return HTMLResponse("Not found", status_code=404)
    group_id = assignment.group_id
    db.delete(assignment)
    db.commit()
    return RedirectResponse(f"/groups/{group_id}", status_code=303)


@router.post("/{assignment_id}/items/add")
async def add_item(
    request: Request,
    assignment_id: int,
    background_tasks: BackgroundTasks,
    item_type: str = Form(...),
    platform: str = Form(...),
    external_id: str = Form(...),
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        return HTMLResponse("Assignment not found", status_code=404)

    external_id = external_id.strip()
    error = None

    # Parse and validate the external_id
    if item_type == "contest":
        if platform == "codeforces":
            parsed = cf.parse_contest_id(external_id)
            if not parsed:
                error = "Invalid Codeforces contest URL or ID."
            else:
                external_id = parsed
        elif platform == "atcoder":
            parsed = ac.parse_contest_id(external_id)
            if not parsed:
                error = "Invalid AtCoder contest URL or slug."
            else:
                external_id = parsed
    elif item_type == "problem":
        if platform == "codeforces":
            parsed = cf.parse_problem_external_id(external_id)
            if not parsed:
                error = "Invalid Codeforces problem URL or ID (e.g. 1234A or https://codeforces.com/contest/1234/problem/A)."
            else:
                external_id = f"{parsed[0]}/{parsed[1]}"
        elif platform == "atcoder":
            parsed = ac.parse_problem_id(external_id)
            if not parsed:
                error = "Invalid AtCoder problem URL (e.g. https://atcoder.jp/contests/abc123/tasks/abc123_a)."
            else:
                external_id = parsed[1]

    if error:
        group = assignment.group
        members = [m.user for m in group.memberships]
        matrix = _build_matrix(assignment.items, members, db)
        return templates.TemplateResponse(
            "assignments/detail.html",
            {
                "request": request,
                "assignment": assignment,
                "group": group,
                "members": members,
                "items": assignment.items,
                "matrix": matrix,
                "add_error": error,
            },
            status_code=422,
        )

    item = AssignmentItem(
        assignment_id=assignment_id,
        type=item_type,
        platform=platform,
        external_id=external_id,
        sync_status="pending",
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    background_tasks.add_task(_run_sync, item.id)

    return RedirectResponse(f"/assignments/{assignment_id}", status_code=303)


@router.post("/{assignment_id}/items/{item_id}/delete")
async def delete_item(
    assignment_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    item = db.get(AssignmentItem, item_id)
    if item and item.assignment_id == assignment_id:
        db.delete(item)
        db.commit()
    return RedirectResponse(f"/assignments/{assignment_id}", status_code=303)


@router.post("/{assignment_id}/sync")
async def sync_assignment(
    assignment_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        return HTMLResponse("Not found", status_code=404)
    for item in assignment.items:
        background_tasks.add_task(_run_sync, item.id)
    return RedirectResponse(f"/assignments/{assignment_id}", status_code=303)


@router.post("/{assignment_id}/items/{item_id}/sync")
async def sync_item(
    assignment_id: int,
    item_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    item = db.get(AssignmentItem, item_id)
    if item and item.assignment_id == assignment_id:
        background_tasks.add_task(_run_sync, item.id)
    return RedirectResponse(f"/assignments/{assignment_id}", status_code=303)


# --- Helpers ---

def _build_matrix(items, members, db: Session) -> list[dict]:
    """
    Returns a list of row dicts for the matrix view.
    Each row: {item, cells: [{user, result, problem_results, live_count,
                               virtual_count, upsolve_count, standalone_count,
                               attempted_count}]}
    """
    rows = []
    for item in items:
        cells = []
        for user in members:
            result = (
                db.query(Result)
                .filter_by(assignment_item_id=item.id, user_id=user.id)
                .first()
            )
            problem_results: dict = {}
            live_count = virtual_count = upsolve_count = standalone_count = attempted_count = 0
            for cp in item.contest_problems:
                pr = (
                    db.query(ProblemResult)
                    .filter_by(contest_problem_id=cp.id, user_id=user.id)
                    .first()
                )
                problem_results[cp.id] = pr
                if pr:
                    if pr.solve_type == "live":
                        live_count += 1
                    elif pr.solve_type == "virtual":
                        virtual_count += 1
                    elif pr.solve_type == "upsolving":
                        upsolve_count += 1
                    elif pr.solve_type == "standalone":
                        standalone_count += 1
                    elif not pr.solved and pr.attempts:
                        attempted_count += 1
            cells.append({
                "user": user,
                "result": result,
                "problem_results": problem_results,
                "live_count": live_count,
                "virtual_count": virtual_count,
                "upsolve_count": upsolve_count,
                "standalone_count": standalone_count,
                "attempted_count": attempted_count,
            })
        rows.append({"item": item, "cells": cells})
    return rows


async def _run_sync(item_id: int) -> None:
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        await sync_svc.sync_item(item_id, db)
    finally:
        db.close()
