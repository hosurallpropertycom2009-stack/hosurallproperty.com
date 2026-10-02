"""
Hosur All Property — Shared database models & schemas
========================================================
Single source of truth for the SQLAlchemy models and Pydantic schemas used
by main.py (public routes), admin.py (admin routes), and agent.py (the AI
chat agent's tools). Importing from one place here means all three always
agree on the exact same table structure and API response shape — no risk
of the definitions drifting apart the way they would if each file declared
its own copy.
"""

import os
from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    create_engine, Column, Integer, String, Float, Text, DateTime, ForeignKey
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, Session
from dotenv import load_dotenv
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

# Where the data lives:
#   - DATABASE_URL set  -> Supabase Postgres (paste the connection string from
#                          Supabase > Project Settings > Database > Connection string)
#   - DATABASE_URL unset -> a local SQLite file, so `uvicorn main:app` still
#                          works on a fresh checkout with zero setup.
_raw_db_url = os.environ.get("DATABASE_URL", "").strip()

if _raw_db_url:
    # Supabase hands out URLs starting with "postgres://" or "postgresql://".
    # SQLAlchemy 2.x needs an explicit driver, and dropped the old
    # "postgres://" alias entirely, so normalise both to the psycopg2 driver.
    if _raw_db_url.startswith("postgres://"):
        _raw_db_url = "postgresql+psycopg2://" + _raw_db_url[len("postgres://"):]
    elif _raw_db_url.startswith("postgresql://"):
        _raw_db_url = "postgresql+psycopg2://" + _raw_db_url[len("postgresql://"):]
    DB_URL = _raw_db_url
    IS_SQLITE = False
else:
    DB_URL = "sqlite:///./property.db"
    IS_SQLITE = True

# Kept here (rather than in storage.py) so main.py/admin.py have one import
# for all the upload rules. The actual saving/deleting lives in storage.py.
UPLOAD_DIR = "property_images"
MAX_PHOTOS_PER_AD = 5
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif"}
MAX_IMAGE_SIZE_BYTES = 1000 * 1024  # 1000kb — matches the frontend's stated limit

os.makedirs(UPLOAD_DIR, exist_ok=True)

# ---------------------------------------------------------------------------
# Database setup
# ---------------------------------------------------------------------------

if IS_SQLITE:
    # check_same_thread is a SQLite-only option; passing it to Postgres crashes.
    engine = create_engine(DB_URL, connect_args={"check_same_thread": False})
else:
    # pool_pre_ping: Supabase closes idle connections, so test each pooled
    # connection before use instead of failing the first request after a quiet
    # period. pool_recycle keeps connections younger than Supabase's idle limit.
    engine = create_engine(
        DB_URL, pool_pre_ping=True, pool_recycle=300, pool_size=5, max_overflow=5,
        connect_args={"connect_timeout": 10},  # fail fast instead of hanging if Supabase is unreachable
    )
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


class Property(Base):
    """
    One row per property ad. Fields match the "Post an Ad" form on
    post-property.html: title, type, category, description, price,
    location, phone, photos.
    """
    __tablename__ = "properties"

    id = Column(Integer, primary_key=True, index=True)
    title = Column(String(200), nullable=False)
    type = Column(String(50), nullable=False)          # Sell / Buy / Rental & Lease / Tenants / JV/JD
    category = Column(String(100), nullable=False)     # Agriculture Lands, Houses & Villas, ...
    description = Column(Text, nullable=False)
    price = Column(Float, nullable=True)                # null -> shown as "On Request"
    price_type = Column(String(20), nullable=True)      # "Fixed" or "Negotiable" (only meaningful when price is set)
    price_unit = Column(String(20), nullable=True)      # what the price is for: "Total", "Per Sq.ft", "Per Cent" or "Per Acre" (only meaningful when price is set)
    location = Column(String(100), nullable=False)
    address = Column(String(255), nullable=True)        # street/site address of the property itself
    phone = Column(String(20), nullable=False)
    contact_name = Column(String(120), nullable=True)   # name of the person who posted the ad
    email = Column(String(255), nullable=True)          # poster's email, for follow-up
    skype = Column(String(120), nullable=True)          # poster's Skype ID, for follow-up
    tags = Column(String(500), nullable=True)            # comma-separated search tags, e.g. "corner plot,near school"
    valid_till = Column(DateTime, nullable=True)          # listing expiry date — ad is hidden after this date
    listed_by = Column(String(20), nullable=True)         # "Agent" or "Owner" — who the poster is, shown/filterable publicly

    # when the poster ticked "I have read and agree" on the Terms & Policy
    # (Non-Circumvention, Non-Disclosure and Commission Agreement). Null for admin-created ads.
    terms_accepted_at = Column(DateTime, nullable=True)

    # who created it: "user" (public ad submission) or "admin" (posted via admin routes)
    posted_by = Column(String(10), nullable=False, default="user")

    # pending -> awaiting review, approved -> visible publicly, rejected -> hidden
    status = Column(String(20), nullable=False, default="pending")

    created_at = Column(DateTime, default=datetime.utcnow)

    images = relationship(
        "PropertyImage", back_populates="property", cascade="all, delete-orphan"
    )


class PropertyImage(Base):
    __tablename__ = "property_images"

    id = Column(Integer, primary_key=True, index=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=False)
    filename = Column(String(255), nullable=False)  # stored filename on disk
    url = Column(String(255), nullable=False)        # public path to serve it

    property = relationship("Property", back_populates="images")


class Lead(Base):
    """
    A contact captured either by the AI agent (source="ai_agent") when a
    user shows interest in a property, or by the "General Enquiry" contact
    form on contact.html (source="contact_form").
    Separate from Property.phone (which belongs to whoever posted the ad) —
    a Lead is the *enquirer's* contact info, tied to whichever listing (if
    any) prompted them to leave it.
    """
    __tablename__ = "leads"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(120), nullable=False)
    phone = Column(String(20), nullable=False)
    email = Column(String(255), nullable=True)   # contact form only; AI agent leaves this null
    place = Column(String(120), nullable=True)    # contact form only; AI agent leaves this null
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=True)
    note = Column(Text, nullable=True)  # e.g. a short summary of what they asked about, or the enquiry message
    source = Column(String(20), nullable=False, default="ai_agent")  # "ai_agent" | "contact_form"
    created_at = Column(DateTime, default=datetime.utcnow)


def _explain_db_error(exc):
    """Logs a plain-English hint for the most common Supabase connection problems."""
    import logging
    msg = str(exc).lower()
    hint = "Check DATABASE_URL in backend/.env."
    if any(k in msg for k in ("network is unreachable", "could not translate host", "name or service not known", "no route to host")):
        hint = (
            "Supabase's 'Direct connection' host (db.<project>.supabase.co) is IPv6-only, and many "
            "networks/hosts (including Render's free tier and some home ISPs) cannot reach it. In Supabase "
            "go to Connect > 'Session pooler' and paste THAT connection string into DATABASE_URL."
        )
    elif "password authentication failed" in msg:
        hint = "The database password in DATABASE_URL is wrong (special characters must be URL-encoded, e.g. @ -> %40)."
    elif "timeout" in msg or "timed out" in msg:
        hint = "The database did not answer in time. Check the host/port in DATABASE_URL and that the Supabase project is not paused."
    logging.getLogger("hosur.database").error("DATABASE CONNECTION FAILED: %s | HINT: %s", exc, hint)


try:
    Base.metadata.create_all(bind=engine)
except Exception as _exc:  # noqa: BLE001 - log a helpful hint, then fail loudly as before
    _explain_db_error(_exc)
    raise


def _run_light_migrations():
    """
    Base.metadata.create_all() only creates tables that don't exist yet — it
    never adds columns to a table that's already there. That matters here
    because `leads` shipped before `email`/`place` existed, and `properties`
    shipped before `address`/`contact_name`/`email` existed: on any database
    created before those updates, `create_all()` silently does nothing for
    the new columns. Rather than requiring a manual migration step, just
    add them if they're missing. Safe to run every startup (checks first).
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    ddl_type = "TEXT" if IS_SQLITE else "VARCHAR(255)"
    table_names = set(inspector.get_table_names())

    if "leads" in table_names:
        existing_cols = {col["name"] for col in inspector.get_columns("leads")}
        with engine.begin() as conn:
            if "email" not in existing_cols:
                conn.execute(text(f"ALTER TABLE leads ADD COLUMN email {ddl_type}"))
            if "place" not in existing_cols:
                conn.execute(text(f"ALTER TABLE leads ADD COLUMN place {ddl_type}"))

    if "properties" in table_names:
        existing_cols = {col["name"] for col in inspector.get_columns("properties")}
        datetime_type = "TEXT" if IS_SQLITE else "TIMESTAMP"
        with engine.begin() as conn:
            if "address" not in existing_cols:
                conn.execute(text(f"ALTER TABLE properties ADD COLUMN address {ddl_type}"))
            if "contact_name" not in existing_cols:
                conn.execute(text(f"ALTER TABLE properties ADD COLUMN contact_name {ddl_type}"))
            if "email" not in existing_cols:
                conn.execute(text(f"ALTER TABLE properties ADD COLUMN email {ddl_type}"))
            if "skype" not in existing_cols:
                conn.execute(text(f"ALTER TABLE properties ADD COLUMN skype {ddl_type}"))
            if "tags" not in existing_cols:
                conn.execute(text(f"ALTER TABLE properties ADD COLUMN tags {ddl_type}"))
            if "valid_till" not in existing_cols:
                conn.execute(text(f"ALTER TABLE properties ADD COLUMN valid_till {datetime_type}"))
            if "listed_by" not in existing_cols:
                conn.execute(text(f"ALTER TABLE properties ADD COLUMN listed_by {ddl_type}"))
            if "terms_accepted_at" not in existing_cols:
                conn.execute(text(f"ALTER TABLE properties ADD COLUMN terms_accepted_at {datetime_type}"))
            if "price_type" not in existing_cols:
                conn.execute(text(f"ALTER TABLE properties ADD COLUMN price_type {ddl_type}"))
            if "price_unit" not in existing_cols:
                conn.execute(text(f"ALTER TABLE properties ADD COLUMN price_unit {ddl_type}"))

        # Ads saved before price units existed all quoted a total price.
        # (Safe to repeat; only touches priced rows that have no unit yet.)
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE properties SET price_unit = 'Total' "
                "WHERE price IS NOT NULL AND price_unit IS NULL"
            ))

        # Old listing types -> the new canonical ones (safe to repeat; only
        # touches rows that still hold an old value).
        with engine.begin() as conn:
            conn.execute(text("UPDATE properties SET type = 'Sell' WHERE type = 'Sale'"))
            conn.execute(text("UPDATE properties SET type = 'Rental & Lease' WHERE type IN ('Rental', 'Lease')"))


_run_light_migrations()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Listing types & price types
# ---------------------------------------------------------------------------

# Canonical listing types, in the order they are shown on the Post Ad form.
LISTING_TYPES = ["Sell", "Buy", "Rental & Lease", "Tenants", "JV/JD"]

# Older values (and a few natural spellings) mapped onto the canonical ones,
# so previously saved ads, cached pages and the AI agent keep working.
_TYPE_ALIASES = {
    "sell": "Sell", "sale": "Sell",
    "buy": "Buy",
    "rental & lease": "Rental & Lease", "rental and lease": "Rental & Lease",
    "rental": "Rental & Lease", "lease": "Rental & Lease", "rent": "Rental & Lease",
    "tenants": "Tenants", "tenant": "Tenants",
    "jv/jd": "JV/JD", "jv / jd": "JV/JD", "jvjd": "JV/JD",
    "joint venture": "JV/JD", "joint development": "JV/JD",
}


def normalize_listing_type(value):
    """Returns the canonical listing type for `value`, or None if unrecognised."""
    if value is None:
        return None
    return _TYPE_ALIASES.get(str(value).strip().lower())


PRICE_TYPES = ["Fixed", "Negotiable"]


def normalize_price_type(value, price=None):
    """
    Returns "Fixed" / "Negotiable" for a valid value, else None. If the ad has
    no price ("On Request") there is nothing to be fixed or negotiable, so None.
    """
    if price is None or value is None:
        return None
    v = str(value).strip().lower()
    if v == "fixed":
        return "Fixed"
    if v == "negotiable":
        return "Negotiable"
    return None


# What a price is quoted for, in the order shown on the Post Ad form.
PRICE_UNITS = ["Total", "Per Sq.ft", "Per Cent", "Per Acre"]

_PRICE_UNIT_ALIASES = {
    "total": "Total", "total price": "Total", "full": "Total", "lump sum": "Total", "lumpsum": "Total",
    "per sq.ft": "Per Sq.ft", "per sqft": "Per Sq.ft", "per sq ft": "Per Sq.ft",
    "per sq. ft": "Per Sq.ft", "per sq.ft.": "Per Sq.ft", "sqft": "Per Sq.ft", "sq.ft": "Per Sq.ft",
    "per cent": "Per Cent", "per cents": "Per Cent", "cent": "Per Cent",
    "per acre": "Per Acre", "per acres": "Per Acre", "acre": "Per Acre",
}


def parse_price_unit(value):
    """
    Returns the canonical price unit for `value`. An empty value means
    "Total" (what every ad quoted before units existed). Returns None only
    if a value was given but isn't recognised.
    """
    if value is None or not str(value).strip():
        return "Total"
    return _PRICE_UNIT_ALIASES.get(str(value).strip().lower())


def normalize_price_unit(value, price=None):
    """
    Returns the canonical price unit to store. With no price ("On Request")
    there is no unit, so None; an empty/unknown unit falls back to "Total".
    """
    if price is None:
        return None
    return parse_price_unit(value) or "Total"


# ---------------------------------------------------------------------------
# Pydantic response schemas
# ---------------------------------------------------------------------------

class ImageOut(BaseModel):
    id: int
    url: str

    class Config:
        from_attributes = True


class PropertyOut(BaseModel):
    id: int
    title: str
    type: str
    category: str
    description: str
    price: Optional[float]
    price_type: Optional[str] = None
    price_unit: Optional[str] = None
    location: str
    address: Optional[str] = None
    phone: str
    contact_name: Optional[str] = None
    email: Optional[str] = None
    skype: Optional[str] = None
    tags: Optional[str] = None
    valid_till: Optional[datetime] = None
    listed_by: Optional[str] = None
    posted_by: str
    status: str
    created_at: datetime
    images: List[ImageOut] = []

    class Config:
        from_attributes = True


class MessageOut(BaseModel):
    success: bool
    message: str
    property_id: Optional[int] = None


class LeadOut(BaseModel):
    id: int
    name: str
    phone: str
    email: Optional[str] = None
    place: Optional[str] = None
    property_id: Optional[int]
    note: Optional[str]
    source: str
    created_at: datetime

    class Config:
        from_attributes = True
