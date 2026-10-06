"""
Hosur All Property — Admin routes
=================================
This is an APIRouter, not a standalone app — main.py imports `router` from
here and mounts it, so everything runs as ONE process on ONE port (needed
for a single Render Web Service). The file stays separate purely for code
organization; it does not run on its own.

Covers:
  - Login via HTTP Basic Auth, credentials from environment variables
        ADMIN_USERNAME (default: "admin")
        ADMIN_PASSWORD (default: "1234")
  - View all properties (pending / approved / rejected)
  - Edit an existing listing's fields and photos
  - Approve / reject a user-submitted ad
  - Admin can directly post a new property with photos (auto-approved,
    posted_by="admin")
  - Delete a property (and its images)
  - View leads — captured by the AI agent (see agent.py) and by the
    "General Enquiry" contact form (see main.py's /api/contact)

All routes below are protected by require_admin (HTTP Basic Auth), except
where noted.
"""

import os
import secrets
from datetime import datetime
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import (
    APIRouter, Form, File, UploadFile, HTTPException, Depends, Query, status
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy.orm import Session

import storage
from locations import canonical_location
from database import (
    get_db, Property, PropertyImage, Lead,
    PropertyOut, MessageOut, LeadOut,
    LISTING_TYPES, normalize_listing_type, normalize_price_type,
    PRICE_UNITS, parse_price_unit, normalize_price_unit,
)

load_dotenv()
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "1234")


def _parse_valid_till(raw: Optional[str]):
    """Parses the "Valid Till" date field (HTML <input type="date">). See
    main.py's copy of this helper for the full docstring — kept identical
    here so admin-created/edited listings use the same date parsing as
    public submissions."""
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
    """Cleans up a comma-separated tags string. See main.py's copy for the
    full docstring."""
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

router = APIRouter(prefix="/admin", tags=["admin"])

# ---------------------------------------------------------------------------
# Auth — HTTP Basic, checked against env username/password
# ---------------------------------------------------------------------------

security = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """
    Protects every admin route. Clients send:
        Authorization: Basic base64(username:password)
    secrets.compare_digest avoids leaking timing info during comparison.
    """
    valid_username = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    valid_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    if not (valid_username and valid_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/properties", response_model=List[PropertyOut])
def admin_list_properties(
    status_filter: Optional[str] = Query(
        None, alias="status", description="pending | approved | rejected"
    ),
    db: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    """Admin dashboard feed — sees ALL ads regardless of status, unlike the public API."""
    query = db.query(Property)
    if status_filter:
        query = query.filter(Property.status == status_filter)
    return query.order_by(Property.created_at.desc()).all()


@router.get("/properties/{property_id}", response_model=PropertyOut)
def admin_get_property(
    property_id: int,
    db: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")
    return prop


@router.post("/properties", response_model=MessageOut)
def admin_create_property(
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
    photos: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    """Admin posts a property directly — saved as already approved, no moderation needed."""
    canonical_type = normalize_listing_type(type)
    if canonical_type is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid listing type. Use one of: " + ", ".join(LISTING_TYPES) + ".",
        )
    stored_images = storage.validate_and_store_uploads(photos)
    if not stored_images:
        # No photo -> show the Hosur All Property logo, same as public ads.
        stored_images = [storage.default_logo_image()]

    new_property = Property(
        title=title,
        type=canonical_type,
        category=category,
        description=description,
        price=price,
        price_type=normalize_price_type(price_type, price),
        price_unit=normalize_price_unit(price_unit, price),
        location=canonical_location(location),
        address=address.strip() if address else None,
        phone=phone,
        contact_name=contact_name.strip() if contact_name else None,
        email=email.strip() if email else None,
        skype=skype.strip() if skype else None,
        listed_by=listed_by.strip() if listed_by and listed_by.strip() in ("Agent", "Owner") else None,
        tags=_normalize_tags(tags),
        valid_till=_parse_valid_till(valid_till),
        posted_by="admin",
        status="approved",
    )
    db.add(new_property)
    db.commit()
    db.refresh(new_property)

    for stored_name, public_url in stored_images:
        db.add(
            PropertyImage(
                property_id=new_property.id,
                filename=stored_name,
                url=public_url,
            )
        )
    db.commit()

    return MessageOut(
        success=True,
        message="Property published successfully.",
        property_id=new_property.id,
    )


@router.put("/properties/{property_id}", response_model=MessageOut)
def admin_update_property(
    property_id: int,
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
    remove_image_ids: Optional[str] = Form(
        None, description="Comma-separated PropertyImage ids to delete, e.g. '3,5'"
    ),
    photos: List[UploadFile] = File(default=[]),
    db: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    """
    Edit an existing listing's details from the admin dashboard.
    - Text fields are always overwritten with the submitted values.
    - remove_image_ids (optional): deletes those specific photos.
    - photos (optional): any newly uploaded files are appended to
      whatever images remain after removal.
    """
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")

    canonical_type = normalize_listing_type(type)
    if canonical_type is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid listing type. Use one of: " + ", ".join(LISTING_TYPES) + ".",
        )

    prop.title = title
    prop.type = canonical_type
    prop.category = category
    prop.description = description
    prop.price = price
    prop.price_type = normalize_price_type(price_type, price)
    prop.price_unit = normalize_price_unit(price_unit, price)
    prop.location = canonical_location(location)
    prop.address = address.strip() if address else None
    prop.phone = phone
    prop.contact_name = contact_name.strip() if contact_name else None
    prop.email = email.strip() if email else None
    prop.skype = skype.strip() if skype else None
    prop.listed_by = listed_by.strip() if listed_by and listed_by.strip() in ("Agent", "Owner") else None
    prop.tags = _normalize_tags(tags)
    prop.valid_till = _parse_valid_till(valid_till)

    # Validate + store new uploads FIRST. If they're invalid we bail out
    # before touching any existing photo, so a bad upload can't cost the
    # admin the photos they already had.
    new_images = storage.validate_and_store_uploads(photos)

    if remove_image_ids:
        ids_to_remove = {
            int(i) for i in remove_image_ids.split(",") if i.strip().isdigit()
        }
        for image in list(prop.images):
            if image.id in ids_to_remove:
                storage.delete_image(image.filename, image.url)
                db.delete(image)

    for stored_name, public_url in new_images:
        db.add(
            PropertyImage(
                property_id=prop.id,
                filename=stored_name,
                url=public_url,
            )
        )

    db.flush()
    db.refresh(prop)

    # Real photos replace the placeholder logo; if every photo was removed
    # (and none added), fall back to the logo again.
    real_images = [i for i in prop.images if i.filename != storage.LOGO_OBJECT_NAME]
    logo_images = [i for i in prop.images if i.filename == storage.LOGO_OBJECT_NAME]
    if real_images:
        for logo in logo_images:
            db.delete(logo)
    elif not prop.images:
        name, url = storage.default_logo_image()
        db.add(PropertyImage(property_id=prop.id, filename=name, url=url))

    db.commit()
    return MessageOut(success=True, message="Listing updated.", property_id=prop.id)


@router.patch("/properties/{property_id}/approve", response_model=MessageOut)
def approve_property(
    property_id: int,
    db: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    """Approve a listing so it appears on the public site."""
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")
    prop.status = "approved"
    db.commit()
    return MessageOut(success=True, message="Listing approved.", property_id=prop.id)


@router.patch("/properties/{property_id}/reject", response_model=MessageOut)
def reject_property(
    property_id: int,
    db: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    """Reject a listing — hides it from the public site (kept in DB, not deleted)."""
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")
    prop.status = "rejected"
    db.commit()
    return MessageOut(success=True, message="Listing rejected.", property_id=prop.id)


@router.delete("/properties/{property_id}", response_model=MessageOut)
def delete_property(
    property_id: int,
    db: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    """Permanently delete a property and its image files."""
    prop = db.query(Property).filter(Property.id == property_id).first()
    if not prop:
        raise HTTPException(status_code=404, detail="Property not found")

    for image in prop.images:
        storage.delete_image(image.filename, image.url)

    db.delete(prop)
    db.commit()
    return MessageOut(success=True, message="Listing deleted.", property_id=property_id)


@router.get("/leads", response_model=List[LeadOut])
def list_leads(
    db: Session = Depends(get_db),
    admin_user: str = Depends(require_admin),
):
    """
    Contacts captured either by the AI agent (source="ai_agent") when a user
    showed interest in a listing, or by the "General Enquiry" contact form
    on contact.html (source="contact_form").
    """
    return db.query(Lead).order_by(Lead.created_at.desc()).all()


@router.post("/test-email", response_model=MessageOut)
def send_test_email(admin_user: str = Depends(require_admin)):
    """
    Sends a sample "new property" email to ADMIN_NOTIFY_EMAIL so you can
    confirm Resend is configured correctly without posting a real ad.
    Runs synchronously (unlike the real notification) so a failure is
    reported straight back to the admin instead of only appearing in logs.
    """
    from datetime import datetime
    import notifications
    from notifications import send_new_property_email, RESEND_API_KEY, ADMIN_NOTIFY_EMAIL

    if not RESEND_API_KEY:
        raise HTTPException(
            status_code=503,
            detail="RESEND_API_KEY is not set on the server, so no email can be sent.",
        )

    sample = {
        "id": 0,
        "title": "TEST — 3 Acre Agriculture Land near Hosur",
        "type": "Sell",
        "category": "Agriculture Lands",
        "description": "This is a test email from the admin console.\nIf you can read this, email alerts are working.",
        "price": 4500000,
        "price_type": "Negotiable",
        "price_unit": "Total",
        "location": "Hosur",
        "address": "Survey No. 12, Rayakottai Main Road, Hosur",
        "phone": "9500391129",
        "contact_name": "Test Poster",
        "email": "test.poster@example.com",
        "skype": "test.poster.skype",
        "listed_by": "Owner",
        "tags": "corner plot, near school",
        "valid_till": None,
        "status": "approved",
        "created_at": datetime.utcnow(),
    }
    logo_name, logo_url = storage.default_logo_image()
    ok = send_new_property_email(sample, [logo_url], used_default_logo=True)
    if not ok:
        raise HTTPException(
            status_code=502,
            detail=notifications.last_error or "The email could not be sent. Check the server logs.",
        )
    return MessageOut(success=True, message=f"Test email sent to {ADMIN_NOTIFY_EMAIL}.")
