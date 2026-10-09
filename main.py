"""
Hosur All Property — Main entry point
=================================
This is the ONLY file you run. It creates the single FastAPI app and
mounts:
  - Its own public routes (below): browse listings, get one listing,
    submit a new ad
  - admin.py's router at /admin/*  (HTTP Basic Auth)
  - agent.py's router at /agent/*  (the AI property chat agent)
  - visitors.py's router at /api/visitors/*  (the entry form shown to every visitor)

Everything runs as one process on one port — this is what a single Render
Web Service needs. admin.py and agent.py are still separate files for
readability; they just don't run standalone anymore.

Run with:
    uvicorn main:app --reload --port 8000

Docs (interactive):
    http://127.0.0.1:8000/docs
"""

import logging
from datetime import datetime
from typing import List, Optional

from fastapi import (
    FastAPI, Form, File, UploadFile, HTTPException, Query, Depends, BackgroundTasks
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from database import (
    get_db, Property, PropertyImage, Lead, PropertyOut, MessageOut, UPLOAD_DIR,
    LISTING_TYPES, normalize_listing_type, normalize_price_type,
    PRICE_UNITS, parse_price_unit, normalize_price_unit,
)
import admin
import agent
import visitors
import storage
from locations import LOCATIONS, canonical_location, location_variants
from notifications import send_new_property_email, send_contact_enquiry_email

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("hosur.main")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="Hosur All Property API")

# Allow the static HTML frontend (opened via file://, a dev server, or
# deployed separately) to call this API.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve uploaded photos at /property_images/<filename>
app.mount("/property_images", StaticFiles(directory=UPLOAD_DIR), name="property_images")

# Mount the admin routes (/admin/*) and AI agent routes (/agent/*) onto
# this same app/process.
app.include_router(admin.router)
app.include_router(agent.router)
app.include_router(visitors.router)   # visitor entry form: /api/visitors/*, /api/visitor-count


# ---------------------------------------------------------------------------
# Small shared helpers for the new "valid till" / "tags" fields
# ---------------------------------------------------------------------------

def _parse_valid_till(raw: Optional[str]):
    """
    Parses the "Valid Till" date sent by the post-property form
    (an HTML <input type="date">, e.g. "2026-12-31"). Returns None for a
    blank/missing value rather than raising, since the field is optional.
    """
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
        try:
            return datetime.strptime(raw, fmt)
        except ValueError:
            continue
    return None


def _normalize_tags(raw: Optional[str]):
    """
    Cleans up the comma-separated tags string from the form (preset
    checkboxes + free-text merged client-side into one field): trims
    whitespace around each tag, drops empties/duplicates, keeps order.
    """
    if not raw or not raw.strip():
        return None
    seen = set()
    cleaned = []
    for part in raw.split(","):
        tag = part.strip()
        if tag and tag.lower() not in seen:
            seen.add(tag.lower())
            cleaned.append(tag)
    return ", ".join(cleaned) if cleaned else None



# Column limits from database.py. SQLite silently ignores them, but Postgres
# (Supabase) rejects over-long text with a 500 error — so check up front and
# give the poster a clear message instead.
def _require_max_len(label: str, value: Optional[str], max_len: int):
    if value is not None and len(value) > max_len:
        raise HTTPException(
            status_code=400,
            detail=f"{label} is too long (maximum {max_len} characters).",
        )

# ---------------------------------------------------------------------------
# Public routes
# ---------------------------------------------------------------------------

@app.get("/")
def root():
    return {"status": "ok", "service": "Hosur All Property public API"}


@app.get("/api/locations")
def list_locations():
    """The places we serve, in display order."""
    return {"locations": LOCATIONS}


@app.get("/api/properties", response_model=List[PropertyOut])
def list_properties(
    category: Optional[str] = Query(None, description="Filter by category"),
    location: Optional[str] = Query(None, description="Filter by location"),
    type: Optional[str] = Query(None, description="Filter by type (Sell/Buy/Rental & Lease/Tenants/JV/JD)"),
    listed_by: Optional[str] = Query(None, description="Filter by who posted it (Agent/Owner)"),
    keyword: Optional[str] = Query(None, description="Search in title & description"),
    min_price: Optional[float] = Query(None, description="Minimum budget (₹)"),
    max_price: Optional[float] = Query(None, description="Maximum budget (₹)"),
    db: Session = Depends(get_db),
):
    """
    Public listing feed — only approved ads are returned.
    Powers listings.html search/filter bar and the homepage grid.
    """
    query = db.query(Property).filter(Property.status == "approved")

    # Hide listings past their validity date. valid_till is NULL for listings
    # posted before this field existed (or left blank) — those never expire.
    query = query.filter(
        (Property.valid_till.is_(None)) | (Property.valid_till >= datetime.utcnow())
    )

    if category:
        query = query.filter(Property.category == category)
    if location:
        # Match the current spelling and older ones (Bangalore -> Bengaluru, etc.)
        query = query.filter(func.lower(Property.location).in_(location_variants(location)))
    if type:
        # Accept old spellings (Sale, Rental, Lease...) as well as the new types.
        query = query.filter(Property.type == (normalize_listing_type(type) or type))
    if listed_by:
        query = query.filter(Property.listed_by == listed_by)
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            (Property.title.ilike(like))
            | (Property.description.ilike(like))
            | (Property.tags.ilike(like))
        )
    # Budget filter. A listing with no price ("On Request") has no known
    # value to compare against a budget range, so it's excluded whenever
    # either bound is set — otherwise every "On Request" ad would silently
    # show up inside (or outside) every budget search.
    # A "Per Sq.ft" / "Per Cent" / "Per Acre" price is a rate, not a total, so
    # it can't be compared with a total budget — only total prices are matched
    # (ads saved before units existed have no unit and are total prices).
    total_only = (Property.price_unit.is_(None)) | (Property.price_unit == "Total")
    if min_price is not None:
        query = query.filter(Property.price.isnot(None), total_only, Property.price >= min_price)
    if max_price is not None:
        query = query.filter(Property.price.isnot(None), total_only, Property.price <= max_price)

    return query.order_by(Property.created_at.desc()).all()


@app.get("/api/properties/{property_id}", response_model=PropertyOut)
def get_property(property_id: int, db: Session = Depends(get_db)):
    """Single listing detail page."""
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop or prop.status != "approved":
        raise HTTPException(status_code=404, detail="Listing not found")
    return prop


@app.post("/api/properties", response_model=MessageOut)
def create_property_ad(
    background_tasks: BackgroundTasks,
    title: str = Form(...),
    type: str = Form(...),
    category: str = Form(...),
    description: str = Form(...),
    location: str = Form(...),
    address: Optional[str] = Form(None),
    phone: str = Form(...),
    contact_name: Optional[str] = Form(None),
    email: Optional[str] = Form(None),
    skype: Optional[str] = Form(None),
    listed_by: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    valid_till: Optional[str] = Form(None),
    price: Optional[float] = Form(None),
    price_type: Optional[str] = Form(None),
    price_unit: Optional[str] = Form(None),
    terms_accepted: Optional[str] = Form(None),
    photos: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
):
    """
    Public "Post an Ad" submission — mirrors the form on post-property.html
    (#adTitle, #adType, #adCategory, #adDescription, #adPrice, #adLocation,
    #adAddress, #adPhone, #adContactName, #adEmail, #adSkype, #adListedBy,
    #adTags, #adValidTill, #adPhotos).

    Saved with status="approved" — goes live immediately, no admin
    review step. The admin dashboard can still edit or remove any
    listing (including these) after the fact if needed.

    After saving, the site owner is emailed the full listing details plus a
    link to the admin page. That email is sent as a background task, so it
    can never slow down or fail the user's submission.
    """
    # Terms & Policy must be accepted before an ad can be published.
    if not terms_accepted or terms_accepted.strip().lower() not in ("true", "1", "on", "yes"):
        raise HTTPException(
            status_code=400,
            detail="You must read and accept the Terms & Policy before posting your listing.",
        )

    canonical_type = normalize_listing_type(type)
    if canonical_type is None:
        raise HTTPException(
            status_code=400,
            detail="Please choose a valid listing type: " + ", ".join(LISTING_TYPES) + ".",
        )
    if price is not None and normalize_price_type(price_type, price) is None:
        raise HTTPException(
            status_code=400,
            detail="Please choose whether the price is Fixed or Negotiable.",
        )
    if price is not None and parse_price_unit(price_unit) is None:
        raise HTTPException(
            status_code=400,
            detail="Please choose what the price is for: " + ", ".join(PRICE_UNITS) + ".",
        )

    title = title.strip()
    category = category.strip()
    description = description.strip()
    location = canonical_location(location)
    phone = phone.strip()
    clean_tags = _normalize_tags(tags)
    _require_max_len("Title", title, 200)
    _require_max_len("Category", category, 100)
    _require_max_len("Location", location, 100)
    _require_max_len("Phone number", phone, 20)
    _require_max_len("Address", address.strip() if address else None, 255)
    _require_max_len("Name", contact_name.strip() if contact_name else None, 120)
    _require_max_len("Email", email.strip() if email else None, 255)
    _require_max_len("Tags", clean_tags, 500)

    # Validate + upload photos to Supabase Storage (or local disk in dev).
    stored_images = storage.validate_and_store_uploads(photos)

    # No photo uploaded -> attach the Hosur All Property logo instead so the
    # listing still shows something branded rather than an empty box.
    used_default_logo = False
    if not stored_images:
        stored_images = [storage.default_logo_image()]
        used_default_logo = True

    new_property = Property(
        title=title,
        type=canonical_type,
        category=category,
        description=description,
        price=price,
        price_type=normalize_price_type(price_type, price),
        price_unit=normalize_price_unit(price_unit, price),
        location=location,
        address=address.strip() if address else None,
        phone=phone,
        contact_name=contact_name.strip() if contact_name else None,
        email=email.strip() if email else None,
        skype=skype.strip() if skype else None,
        listed_by=listed_by.strip() if listed_by and listed_by.strip() in ("Agent", "Owner") else None,
        tags=clean_tags,
        valid_till=_parse_valid_till(valid_till),
        posted_by="user",
        status="approved",
        terms_accepted_at=datetime.utcnow(),
    )

    # Listing + its photo rows are saved in ONE transaction, so an ad can never
    # end up stored without its images (or the other way round).
    try:
        db.add(new_property)
        db.flush()  # assigns new_property.id
        for stored_name, public_url in stored_images:
            db.add(
                PropertyImage(
                    property_id=new_property.id,
                    filename=stored_name,
                    url=public_url,
                )
            )
        db.commit()
        db.refresh(new_property)
    except SQLAlchemyError:
        # Saving failed after photos were already uploaded — clean them up so
        # we don't leave orphaned files in the bucket, and tell the poster
        # plainly (an unhandled error would surface in the browser as a
        # confusing "connection lost" message instead).
        db.rollback()
        log.exception("Could not save new listing to the database")
        if not used_default_logo:
            for name, url in stored_images:
                storage.delete_image(name, url)
        raise HTTPException(
            status_code=503,
            detail="We couldn't save your listing to the database right now. "
                   "Please try again in a moment.",
        )

    # Snapshot plain values now — the background task runs after this
    # request's DB session is closed, so it must not touch ORM objects.
    snapshot = {
        "id": new_property.id,
        "title": new_property.title,
        "type": new_property.type,
        "category": new_property.category,
        "description": new_property.description,
        "price": new_property.price,
        "price_type": new_property.price_type,
        "price_unit": new_property.price_unit,
        "location": new_property.location,
        "address": new_property.address,
        "phone": new_property.phone,
        "contact_name": new_property.contact_name,
        "email": new_property.email,
        "skype": new_property.skype,
        "listed_by": new_property.listed_by,
        "tags": new_property.tags,
        "valid_till": new_property.valid_till,
        "status": new_property.status,
        "created_at": new_property.created_at,
    }
    background_tasks.add_task(
        send_new_property_email,
        snapshot,
        [url for _, url in stored_images],
        used_default_logo,
    )

    return MessageOut(
        success=True,
        message="Your listing is live now.",
        property_id=new_property.id,
    )


@app.post("/api/contact", response_model=MessageOut)
def submit_contact_enquiry(
    background_tasks: BackgroundTasks,
    name: str = Form(...),
    phone: str = Form(...),
    email: str = Form(...),
    place: str = Form(...),
    message: str = Form(...),
    db: Session = Depends(get_db),
):
    """
    Public "Send a Message" / General Enquiry submission — mirrors the form
    on contact.html (#cName, #cPhone, #cEmail, #cPlace, #cMessage).

    Saved into the same `leads` table the AI agent writes to (source is
    what tells them apart), so every enquiry — whether typed into the
    contact form or captured mid-chat — shows up together in the admin
    Leads tab. Lives in Supabase Postgres when DATABASE_URL is set, local
    SQLite otherwise (see database.py).

    After saving, the site owner is emailed the enquiry details. That email
    is sent as a background task, so it can never slow down or fail the
    user's submission.
    """
    new_lead = Lead(
        name=name.strip(),
        phone=phone.strip(),
        email=email.strip(),
        place=place.strip(),
        note=message.strip(),
        source="contact_form",
    )
    db.add(new_lead)
    db.commit()
    db.refresh(new_lead)

    # Snapshot plain values now — the background task runs after this
    # request's DB session is closed, so it must not touch ORM objects.
    snapshot = {
        "id": new_lead.id,
        "name": new_lead.name,
        "phone": new_lead.phone,
        "email": new_lead.email,
        "place": new_lead.place,
        "note": new_lead.note,
        "created_at": new_lead.created_at,
    }
    background_tasks.add_task(send_contact_enquiry_email, snapshot)

    return MessageOut(
        success=True,
        message="Thanks — your message has been sent. We'll get back to you shortly.",
        property_id=None,
    )
