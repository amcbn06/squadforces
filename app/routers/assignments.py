from fastapi import APIRouter, Depends, Request, Form, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from sqlalchemy import or_

from app.models import (
    Group, Assignment, AssignmentItem, ContestProblem, Result, ProblemResult, GroupMembership, Hint,
)
from app.auth import require_auth, require_admin, can_delete_item
from app.scraper import codeforces as cf
from app.scraper import atcoder as ac
from app.scraper import kilonova as kn
from app import link_parser
from app import sync as sync_svc

router = APIRouter(prefix="/assignments", tags=["assignments"])
templates = Jinja2Templates(directory="app/templates")


def _check_group_access(account, group_id: int, db: Session):
    if account.user_type == "admin":
        return
    if db.query(GroupMembership).filter_by(group_id=group_id, user_id=account.id).first():
        return
    raise HTTPException(status_code=403)


@router.get("/new", response_class=HTMLResponse)
async def new_assignment_form(
    request: Request,
    group_id: int,
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    group = db.get(Group, group_id)
    if not group:
        return HTMLResponse("Group not found", status_code=404)
    _check_group_access(account, group_id, db)
    return templates.TemplateResponse(
        "assignments/form.html", {"request": request, "group": group, "error": None, "account": account}
    )


@router.post("/new")
async def create_assignment(
    request: Request,
    group_id: int = Form(...),
    title: str = Form(...),
    week_start_date: str = Form(""),
    description: str = Form(""),
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    from datetime import date
    group = db.get(Group, group_id)
    if not group:
        return HTMLResponse("Group not found", status_code=404)
    _check_group_access(account, group_id, db)

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
    account=Depends(require_auth),
):
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        return HTMLResponse("Assignment not found", status_code=404)

    _check_group_access(account, assignment.group_id, db)
    return _render_detail(request, assignment, account, db)


@router.post("/{assignment_id}/delete")
async def delete_assignment(
    assignment_id: int, db: Session = Depends(get_db), account=Depends(require_admin)
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
    account=Depends(require_auth),
):
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        return HTMLResponse("Assignment not found", status_code=404)

    _check_group_access(account, assignment.group_id, db)

    external_id = external_id.strip()
    error = None

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
        elif platform == "kilonova":
            parsed = kn.parse_contest_id(external_id)
            if not parsed:
                error = "Invalid Kilonova problem list URL or ID (e.g. 1572 or https://kilonova.ro/problem_lists/1572)."
            else:
                external_id = str(parsed)
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
        elif platform == "kilonova":
            parsed = kn.parse_problem_id(external_id)
            if not parsed:
                error = "Invalid Kilonova problem URL or ID (e.g. 2460 or https://kilonova.ro/problems/2460)."
            else:
                external_id = str(parsed)

    if error:
        return _render_detail(request, assignment, account, db, status_code=422, add_error=error)

    item = AssignmentItem(
        assignment_id=assignment_id,
        type=item_type,
        platform=platform,
        external_id=external_id,
        sync_status="pending",
        created_by_id=account.id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)

    background_tasks.add_task(_run_sync, item.id)

    return RedirectResponse(f"/assignments/{assignment_id}", status_code=303)


@router.post("/{assignment_id}/items/bulk")
async def bulk_add_items(
    request: Request,
    assignment_id: int,
    background_tasks: BackgroundTasks,
    links: str = Form(...),
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        return HTMLResponse("Assignment not found", status_code=404)
    _check_group_access(account, assignment.group_id, db)

    parsed, errors = link_parser.parse_links(links)
    if len(parsed) > link_parser.MAX_LINKS:
        return _render_detail(
            request, assignment, account, db, status_code=422,
            bulk_text=links,
            bulk_errors=[("", f"Too many links: {len(parsed)} found, the limit is {link_parser.MAX_LINKS} per submission.")],
        )

    existing = {(i.platform, i.type, i.external_id) for i in assignment.items}
    added, duplicates, new_items = [], [], []
    for p in parsed:
        if (p["platform"], p["type"], p["external_id"]) in existing:
            duplicates.append(p["label"])
            continue
        item = AssignmentItem(
            assignment_id=assignment_id,
            type=p["type"],
            platform=p["platform"],
            external_id=p["external_id"],
            sync_status="pending",
            created_by_id=account.id,
        )
        db.add(item)
        new_items.append(item)
        added.append(p["label"])
    db.commit()

    for item in new_items:
        background_tasks.add_task(_run_sync, item.id)

    if not errors and not duplicates:
        return RedirectResponse(f"/assignments/{assignment_id}", status_code=303)

    db.refresh(assignment)
    return _render_detail(
        request, assignment, account, db,
        bulk_added=added,
        bulk_duplicates=duplicates,
        bulk_errors=errors,
        bulk_text="\n".join(token for token, _ in errors),
    )


@router.post("/{assignment_id}/hints/add")
async def add_hint(
    assignment_id: int,
    target: str = Form(...),
    text: str = Form(...),
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        return HTMLResponse("Assignment not found", status_code=404)
    if not assignment.group.hints_allowed:
        raise HTTPException(status_code=400, detail="Hints are not enabled for this group.")

    kind, raw_id = target[:1], target[1:]
    if kind not in ("i", "p") or not raw_id.isdigit():
        return HTMLResponse("Bad hint target", status_code=400)
    target_id = int(raw_id)

    text = text.strip()
    if not text:
        return RedirectResponse(f"/assignments/{assignment_id}?hint={target}", status_code=303)
    if len(text) > 5000:
        raise HTTPException(status_code=400, detail="Hint is too long (max 5000 characters).")

    if kind == "i":
        item = db.get(AssignmentItem, target_id)
        if not item or item.assignment_id != assignment_id or item.type != "problem":
            return HTMLResponse("Problem not found", status_code=404)
        db.add(Hint(assignment_item_id=item.id, text=text))
    else:
        cp = db.get(ContestProblem, target_id)
        if not cp or cp.assignment_item.assignment_id != assignment_id:
            return HTMLResponse("Problem not found", status_code=404)
        db.add(Hint(contest_problem_id=cp.id, text=text))
    db.commit()
    return RedirectResponse(f"/assignments/{assignment_id}?hint={target}", status_code=303)


@router.post("/{assignment_id}/hints/{hint_id}/delete")
async def delete_hint(
    assignment_id: int,
    hint_id: int,
    db: Session = Depends(get_db),
    account=Depends(require_admin),
):
    hint = db.get(Hint, hint_id)
    if not hint:
        return RedirectResponse(f"/assignments/{assignment_id}", status_code=303)
    owner = hint.assignment_item or hint.contest_problem.assignment_item
    if owner.assignment_id != assignment_id:
        return HTMLResponse("Hint not found", status_code=404)
    target = f"i{hint.assignment_item_id}" if hint.assignment_item_id else f"p{hint.contest_problem_id}"
    db.delete(hint)
    db.commit()
    return RedirectResponse(f"/assignments/{assignment_id}?hint={target}", status_code=303)


@router.post("/{assignment_id}/items/{item_id}/delete")
async def delete_item(
    assignment_id: int,
    item_id: int,
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    item = db.get(AssignmentItem, item_id)
    if item and item.assignment_id == assignment_id:
        if not can_delete_item(account, item):
            raise HTTPException(status_code=403, detail="You can only remove items you added.")
        db.delete(item)
        db.commit()
    return RedirectResponse(f"/assignments/{assignment_id}", status_code=303)


@router.post("/{assignment_id}/sync")
async def sync_assignment(
    assignment_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    assignment = db.get(Assignment, assignment_id)
    if not assignment:
        return HTMLResponse("Not found", status_code=404)
    _check_group_access(account, assignment.group_id, db)
    for item in assignment.items:
        background_tasks.add_task(_run_sync, item.id)
    return RedirectResponse(f"/assignments/{assignment_id}", status_code=303)


@router.post("/{assignment_id}/items/{item_id}/sync")
async def sync_item(
    assignment_id: int,
    item_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    account=Depends(require_auth),
):
    item = db.get(AssignmentItem, item_id)
    if item and item.assignment_id == assignment_id:
        _check_group_access(account, item.assignment.group_id, db)
        background_tasks.add_task(_run_sync, item.id)
    return RedirectResponse(f"/assignments/{assignment_id}", status_code=303)


# --- Helpers ---

def _hints_map(assignment, db: Session) -> dict[str, list[dict]]:
    """Hints keyed by 'i<item id>' (standalone problem) or 'p<contest problem id>', oldest first."""
    item_ids = [i.id for i in assignment.items if i.type == "problem"]
    cp_ids = [cp.id for i in assignment.items for cp in i.contest_problems]
    if not item_ids and not cp_ids:
        return {}
    hints = (
        db.query(Hint)
        .filter(or_(Hint.assignment_item_id.in_(item_ids), Hint.contest_problem_id.in_(cp_ids)))
        .order_by(Hint.id)
        .all()
    )
    result: dict[str, list[dict]] = {}
    for h in hints:
        key = f"i{h.assignment_item_id}" if h.assignment_item_id else f"p{h.contest_problem_id}"
        result.setdefault(key, []).append({"id": h.id, "text": h.text})
    return result


def _render_detail(request: Request, assignment, account, db: Session, status_code: int = 200, **extra):
    group = assignment.group
    members = [m.user for m in group.memberships]
    return templates.TemplateResponse(
        "assignments/detail.html",
        {
            "request": request,
            "assignment": assignment,
            "group": group,
            "members": members,
            "items": assignment.items,
            "matrix": _build_matrix(assignment.items, members, db),
            "hints_map": _hints_map(assignment, db) if group.hints_allowed else {},
            "account": account,
            **extra,
        },
        status_code=status_code,
    )


def _build_matrix(items, members, db: Session) -> list[dict]:
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
