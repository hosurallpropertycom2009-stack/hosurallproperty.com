"""
Hosur All Property — Image storage
====================================
One place that knows where property photos live. Everything else in the
backend (main.py, admin.py) just calls the functions below and gets back a
plain public URL — it never needs to know whether that URL points at
Supabase Storage or at the local `property_images/` folder.

Modes (chosen automatically from environment variables):

  1. Supabase Storage  — when SUPABASE_URL and SUPABASE_SERVICE_KEY are set.
                         Photos are uploaded to the bucket named by
                         SUPABASE_BUCKET (default: "property-images") and
                         served from Supabase's public CDN URL.
  2. Local disk        — when those variables are missing. Photos go into
                         ./property_images and are served by main.py at
                         /property_images/<name>. Handy for local dev.

Default image:
  If an ad is posted with no photos, `default_logo_image()` returns the
  Hosur All Property logo so the listing still shows something branded
  instead of an empty box. In Supabase mode the logo is uploaded to the
  bucket once (under a fixed name) and reused for every such listing.
"""

import logging
import mimetypes
import os
import uuid
from typing import List, Optional, Tuple

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("hosur.storage")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
SUPABASE_BUCKET = os.environ.get("SUPABASE_BUCKET", "property-images").strip()

UPLOAD_DIR = "property_images"
os.makedirs(UPLOAD_DIR, exist_ok=True)

# Fixed object name for the shared logo in the bucket.
LOGO_OBJECT_NAME = "_defaults/hosur-all-property-logo.png"

# Where the logo lives in the repo. Looked up relative to this file so it
# works no matter which directory uvicorn is started from.
_HERE = os.path.dirname(os.path.abspath(__file__))
LOGO_LOCAL_CANDIDATES = [
    os.path.join(_HERE, "assets", "logo.png"),                       # copy shipped with backend
    os.path.join(_HERE, "..", "frontend", "assets", "img", "logo.png"),  # monorepo layout
]

USE_SUPABASE = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

_client = None
_logo_url_cache: Optional[str] = None


def _get_client():
    """Lazily create the Supabase client so the app still boots without it."""
    global _client
    if _client is None:
        from supabase import create_client
        _client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
    return _client


def _find_logo_file() -> Optional[str]:
    for path in LOGO_LOCAL_CANDIDATES:
        if os.path.isfile(path):
            return os.path.normpath(path)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def is_supabase_mode() -> bool:
    return USE_SUPABASE


def save_image(contents: bytes, original_filename: str, content_type: str) -> Tuple[str, str]:
    """
    Store one uploaded image. Returns (stored_name, public_url).

    `stored_name` is what gets saved in PropertyImage.filename and is what
    delete_image() later needs to remove it. `public_url` is what the
    frontend uses as the <img src>.
    """
    ext = os.path.splitext(original_filename or "")[1].lower() or ".jpg"
    stored_name = f"{uuid.uuid4().hex}{ext}"

    if USE_SUPABASE:
        client = _get_client()
        client.storage.from_(SUPABASE_BUCKET).upload(
            path=stored_name,
            file=contents,
            file_options={"content-type": content_type, "upsert": "false"},
        )
        public_url = client.storage.from_(SUPABASE_BUCKET).get_public_url(stored_name)
        return stored_name, public_url

    with open(os.path.join(UPLOAD_DIR, stored_name), "wb") as out:
        out.write(contents)
    return stored_name, f"/property_images/{stored_name}"


def delete_image(stored_name: str, url: str = "") -> None:
    """
    Remove one image. Never raises — a failed cleanup must not break the
    delete/edit action the admin was actually performing.

    The shared logo is never deleted, even if a listing referencing it is.
    """
    if not stored_name or stored_name == LOGO_OBJECT_NAME:
        return
    try:
        if USE_SUPABASE:
            _get_client().storage.from_(SUPABASE_BUCKET).remove([stored_name])
        else:
            path = os.path.join(UPLOAD_DIR, stored_name)
            if os.path.exists(path):
                os.remove(path)
    except Exception as exc:  # noqa: BLE001 - cleanup is best-effort
        log.warning("Could not delete image %s: %s", stored_name, exc)


def default_logo_image() -> Tuple[str, str]:
    """
    Returns (stored_name, public_url) for the Hosur All Property logo, used
    when a listing is posted without any photos.

    In Supabase mode the logo is uploaded once and its URL cached in memory;
    if the upload ever fails we fall back to the local file so a listing is
    never blocked just because the logo couldn't be pushed.
    """
    global _logo_url_cache

    if USE_SUPABASE:
        if _logo_url_cache:
            return LOGO_OBJECT_NAME, _logo_url_cache
        logo_path = _find_logo_file()
        if logo_path:
            try:
                bucket = _get_client().storage.from_(SUPABASE_BUCKET)
                with open(logo_path, "rb") as fh:
                    bucket.upload(
                        path=LOGO_OBJECT_NAME,
                        file=fh.read(),
                        file_options={"content-type": "image/png", "upsert": "true"},
                    )
                _logo_url_cache = bucket.get_public_url(LOGO_OBJECT_NAME)
                return LOGO_OBJECT_NAME, _logo_url_cache
            except Exception as exc:  # noqa: BLE001
                log.warning("Could not upload logo to Supabase: %s", exc)

    # Local mode (or Supabase logo upload failed): copy the logo into the
    # uploads folder once and serve it from there.
    local_name = "hosur-all-property-logo.png"
    dest = os.path.join(UPLOAD_DIR, local_name)
    if not os.path.exists(dest):
        logo_path = _find_logo_file()
        if logo_path:
            with open(logo_path, "rb") as src, open(dest, "wb") as out:
                out.write(src.read())
    # Always report the fixed LOGO_OBJECT_NAME as the stored name so
    # delete_image() recognises it and refuses to delete the shared logo.
    return LOGO_OBJECT_NAME, f"/property_images/{local_name}"


def guess_content_type(filename: str) -> str:
    return mimetypes.guess_type(filename)[0] or "application/octet-stream"


def validate_and_store_uploads(files) -> List[Tuple[str, str]]:
    """
    Validate a batch of FastAPI UploadFiles and store them.
    Returns a list of (stored_name, public_url).

    Shared by main.py (public "Post an Ad") and admin.py (admin create/edit)
    so both enforce identical rules. Everything is validated BEFORE anything
    is stored, so a bad 4th file can't leave the first 3 orphaned in the
    bucket. If a later upload fails partway, the ones already stored are
    removed again.
    """
    # Imported here to avoid a circular import (database -> storage is not
    # needed, but keeping the limits in database.py as the single source).
    from fastapi import HTTPException
    from database import MAX_PHOTOS_PER_AD, ALLOWED_IMAGE_TYPES, MAX_IMAGE_SIZE_BYTES

    files = [f for f in (files or []) if f and f.filename]

    if len(files) > MAX_PHOTOS_PER_AD:
        raise HTTPException(
            status_code=400,
            detail=f"You can upload a maximum of {MAX_PHOTOS_PER_AD} photos.",
        )

    # Pass 1 — validate everything, storing nothing yet.
    prepared = []
    for f in files:
        if f.content_type not in ALLOWED_IMAGE_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"'{f.filename}' has unsupported type {f.content_type}. "
                       f"Only PNG, JPEG, GIF are allowed.",
            )
        contents = f.file.read()
        if len(contents) > MAX_IMAGE_SIZE_BYTES:
            raise HTTPException(
                status_code=400,
                detail=f"'{f.filename}' exceeds the 1000kb size limit.",
            )
        prepared.append((contents, f.filename, f.content_type))

    # Pass 2 — store. Roll back what we stored if any upload fails.
    stored: List[Tuple[str, str]] = []
    try:
        for contents, filename, content_type in prepared:
            stored.append(save_image(contents, filename, content_type))
    except Exception as exc:  # noqa: BLE001
        for name, url in stored:
            delete_image(name, url)
        log.error("Image upload failed: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Could not store your photos right now. Please try again in a moment.",
        )
    return stored
