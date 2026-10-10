"""
Hosur All Property — Visitor entry (public routes)
===================================================
Every time someone enters the website they must first fill in the entry form
(name, phone, optional email, place, and what they are: Seller / Buyer / ...). The form
lives in frontend/assets/js/visitor-gate.js and talks to the routes below.

This is an APIRouter mounted by main.py (same process / port as everything
else). The admin side — seeing who came in, and blocking by phone number — is
in admin.py and is protected by the admin login.

Routes
  POST /api/visitors/register  — save the entry form. 403 if the phone is blocked.
  POST /api/visitors/check     — "is this phone blocked?" (asked on every page load,
                                 so a block takes effect while the person is browsing)
  GET  /api/visitor-count      — how many visitors so far (shown on the site)
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from database import (
    get_db, Visitor, BlockedPhone,
    VISITOR_CATEGORIES, normalize_visitor_category, normalize_phone,
)

log = logging.getLogger("hosur.visitors")

router = APIRouter(tags=["visitors"])

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")

BLOCKED_MESSAGE = (
    "Access to this website has been restricted for this phone number. "
    "Please contact Hosur All Property on +91 95003 91129 if you think this is a mistake."
)

IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Small helpers (also used by admin.py)
# ---------------------------------------------------------------------------

def iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """
    Naive UTC datetime from the database -> ISO string ending in "Z", so the
    browser knows it is UTC and shows it in the viewer's own time zone.
    """
    if dt is None:
        return None
    return dt.replace(microsecond=0).isoformat() + "Z"


def ist_today_start_utc() -> datetime:
    """Midnight at the start of today in India (IST), as a naive UTC datetime."""
    now_ist = datetime.now(IST)
    midnight_ist = now_ist.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_ist.astimezone(timezone.utc).replace(tzinfo=None)


def is_blocked(db: Session, phone: str) -> bool:
    """`phone` must already be normalised."""
    return db.query(BlockedPhone.id).filter(BlockedPhone.phone == phone).first() is not None


def _blocked_response() -> JSONResponse:
    return JSONResponse(
        status_code=403,
        content={"success": False, "blocked": True, "detail": BLOCKED_MESSAGE},
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/api/visitors/register")
def register_visitor(
    name: str = Form(...),
    phone: str = Form(...),
    email: Optional[str] = Form(None),   # optional
    place: str = Form(...),
    category: str = Form(...),
    db: Session = Depends(get_db),
):
    """
    Saves the entry form. A phone number seen before keeps its single row: the
    details are refreshed to what was just typed and the visit counter goes up.
    A blocked phone number is refused with HTTP 403 and nothing is saved.
    """
    name = " ".join(name.split())
    email = (email or "").strip() or None   # empty -> not provided
    place = " ".join(place.split())

    if len(name) < 2:
        raise HTTPException(status_code=400, detail="Please enter your name.")
    if len(name) > 120:
        raise HTTPException(status_code=400, detail="Name is too long (maximum 120 characters).")

    clean_phone = normalize_phone(phone)
    if clean_phone is None:
        raise HTTPException(status_code=400, detail="Please enter a valid phone number (10 digits).")

    if email is not None and (not _EMAIL_RE.match(email) or len(email) > 255):
        raise HTTPException(status_code=400, detail="Please enter a valid email address.")

    if len(place) < 2:
        raise HTTPException(status_code=400, detail="Please enter your place.")
    if len(place) > 120:
        raise HTTPException(status_code=400, detail="Place is too long (maximum 120 characters).")

    clean_category = normalize_visitor_category(category)
    if clean_category is None:
        raise HTTPException(
            status_code=400,
            detail="Please choose one: " + ", ".join(VISITOR_CATEGORIES) + ".",
        )

    # Blocked numbers never get in — and are not recorded again.
    if is_blocked(db, clean_phone):
        return _blocked_response()

    def _upsert() -> None:
        now = datetime.utcnow()
        visitor = db.query(Visitor).filter(Visitor.phone == clean_phone).first()
        if visitor is None:
            db.add(Visitor(
                name=name, phone=clean_phone, email=email, place=place,
                category=clean_category, visit_count=1,
                first_visit=now, last_visit=now,
            ))
        else:
            visitor.name = name
            visitor.email = email
            visitor.place = place
            visitor.category = clean_category
            visitor.visit_count = (visitor.visit_count or 0) + 1
            visitor.last_visit = now
        db.commit()

    try:
        try:
            _upsert()
        except IntegrityError:
            # Two entries for a brand-new number arrived at the same moment and
            # both tried to create the row. The second one just updates it.
            db.rollback()
            _upsert()
    except SQLAlchemyError:
        db.rollback()
        log.exception("Could not save visitor entry")
        raise HTTPException(
            status_code=503,
            detail="We couldn't save your details right now. Please try again in a moment.",
        )

    return {"success": True, "blocked": False, "message": "Welcome to Hosur All Property."}


@router.post("/api/visitors/check")
def check_visitor(phone: str = Form(...), db: Session = Depends(get_db)):
    """Is this phone number blocked? Called on every page load for a returning session."""
    clean_phone = normalize_phone(phone)
    if clean_phone is None:
        return {"blocked": False}
    return {"blocked": is_blocked(db, clean_phone)}


@router.get("/api/visitor-count")
def visitor_count(db: Session = Depends(get_db)):
    """Public counter: distinct visitors, and total entries."""
    total_visitors, total_visits = db.query(
        func.count(Visitor.id), func.coalesce(func.sum(Visitor.visit_count), 0)
    ).one()
    return {"total_visitors": int(total_visitors or 0), "total_visits": int(total_visits or 0)}
