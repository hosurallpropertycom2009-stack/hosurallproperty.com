"""
End-to-end tests for: post an ad -> stored -> logo fallback -> admin email.

These run the REAL FastAPI app against a throwaway SQLite database. Only the
two network boundaries are faked:
  * Resend  (resend.Emails.send)      -> captured so we can inspect the email
  * Supabase Storage (storage client) -> an in-memory fake bucket

Run:   pytest -v test_flow.py                      (isolated throwaway SQLite)
       TEST_DATABASE_URL=postgres://user@host:5432/db pytest -v test_flow.py
                                                   (same tests against Postgres)

Never point TEST_DATABASE_URL at your real Supabase database — the tests
create and modify listings.
"""

import io
import os
import sys
import tempfile

import pytest

# --- isolate: fresh DB + upload dir, and force the config we want to test ---
_tmp = tempfile.mkdtemp()
os.chdir(_tmp)
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "")
os.environ["SUPABASE_URL"] = ""
os.environ["SUPABASE_SERVICE_KEY"] = ""
os.environ["RESEND_API_KEY"] = "re_test_key"
os.environ["ADMIN_NOTIFY_EMAIL"] = "nostalgic1235@gmail.com"
os.environ["ADMIN_PAGE_URL"] = "https://hosurallproperty.com/admin.html"

from fastapi.testclient import TestClient  # noqa: E402

import notifications  # noqa: E402
import storage  # noqa: E402
import main  # noqa: E402
from database import SessionLocal, Property, PropertyImage  # noqa: E402

client = TestClient(main.app)
AUTH = ("admin", "1234")

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06"
    b"\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe"
    b"\x02\xfe\xa7\x9a\xa0\xa0\x00\x00\x00\x00IEND\xaeB`\x82"
)

AD = {
    "title": "2BHK House near Attibele",
    "type": "Rental & Lease",
    "category": "Houses & Villas",
    "description": "Bright 2BHK.\nGround floor, parking.",
    "location": "Hosur",
    "phone": "9876543210",
    "price": "15000",
    "price_type": "Negotiable",
    "terms_accepted": "true",
}


@pytest.fixture()
def sent_emails(monkeypatch):
    """Capture what would have been sent to Resend."""
    sent = []

    import resend

    def fake_send(params, options=None):
        sent.append(params)
        return {"id": "fake-email-id"}

    monkeypatch.setattr(resend.Emails, "send", staticmethod(fake_send))
    monkeypatch.setattr(notifications, "RESEND_API_KEY", "re_test_key")
    return sent


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def test_posting_ad_emails_admin_with_all_details(sent_emails):
    r = client.post("/api/properties", data=AD, files=[("photos", ("a.png", PNG, "image/png"))])
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True

    assert len(sent_emails) == 1, "exactly one email should be sent per ad"
    mail = sent_emails[0]
    assert mail["to"] == ["nostalgic1235@gmail.com"]
    assert "2BHK House near Attibele" in mail["subject"]

    html = mail["html"]
    for expected in [
        "2BHK House near Attibele", "Rental &amp; Lease", "Houses &amp; Villas", "Hosur",
        "9876543210", "15,000", "Negotiable", "Bright 2BHK.<br>Ground floor, parking.",
    ]:
        assert expected in html, f"email is missing: {expected!r}"

    # admin page link is in the email
    assert 'href="https://hosurallproperty.com/admin.html"' in html
    assert "Open Admin Page" in html
    assert "https://hosurallproperty.com/admin.html" in mail["text"]


def test_email_escapes_html_in_user_input(sent_emails):
    evil = dict(AD, title="<script>alert(1)</script>", description="<img src=x onerror=alert(2)>")
    r = client.post("/api/properties", data=evil)
    assert r.status_code == 200
    html = sent_emails[0]["html"]
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img src=x onerror" not in html


def test_price_omitted_shows_on_request(sent_emails):
    ad = {k: v for k, v in AD.items() if k != "price"}
    r = client.post("/api/properties", data=ad)
    assert r.status_code == 200
    assert "On Request" in sent_emails[0]["html"]


def test_email_failure_never_breaks_the_ad(monkeypatch):
    """If Resend is down / key is bad, the user must still get a live listing."""
    import resend

    def boom(params, options=None):
        raise RuntimeError("resend is down")

    monkeypatch.setattr(resend.Emails, "send", staticmethod(boom))
    monkeypatch.setattr(notifications, "RESEND_API_KEY", "re_test_key")

    r = client.post("/api/properties", data=AD)
    assert r.status_code == 200
    assert r.json()["success"] is True
    pid = r.json()["property_id"]
    assert client.get(f"/api/properties/{pid}").status_code == 200


def test_no_api_key_skips_email_but_ad_still_saves(monkeypatch):
    monkeypatch.setattr(notifications, "RESEND_API_KEY", "")
    r = client.post("/api/properties", data=AD)
    assert r.status_code == 200 and r.json()["success"] is True


# ---------------------------------------------------------------------------
# Logo fallback
# ---------------------------------------------------------------------------

def test_no_photo_gets_logo_image(sent_emails):
    r = client.post("/api/properties", data=AD)
    pid = r.json()["property_id"]
    prop = client.get(f"/api/properties/{pid}").json()

    assert len(prop["images"]) == 1, "listing with no photo should get the logo"
    assert "hosur-all-property-logo" in prop["images"][0]["url"]


def test_logo_url_actually_serves_the_real_logo():
    r = client.post("/api/properties", data=AD)
    pid = r.json()["property_id"]
    url = client.get(f"/api/properties/{pid}").json()["images"][0]["url"]
    img = client.get(url)
    assert img.status_code == 200
    assert img.content[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(img.content) > 10_000, "should be the real logo, not an empty placeholder"


def test_uploaded_photo_replaces_logo_fallback():
    r = client.post("/api/properties", data=AD, files=[("photos", ("a.png", PNG, "image/png"))])
    prop = client.get(f"/api/properties/{r.json()['property_id']}").json()
    assert len(prop["images"]) == 1
    assert "hosur-all-property-logo" not in prop["images"][0]["url"]


def test_email_notes_logo_when_no_photo(sent_emails):
    client.post("/api/properties", data=AD)
    assert "logo is being shown" in sent_emails[0]["html"]


# ---------------------------------------------------------------------------
# Upload validation still enforced
# ---------------------------------------------------------------------------

def test_rejects_bad_file_type_and_stores_nothing():
    before = len(os.listdir(storage.UPLOAD_DIR))
    r = client.post("/api/properties", data=AD, files=[("photos", ("x.txt", b"hello", "text/plain"))])
    assert r.status_code == 400
    assert len(os.listdir(storage.UPLOAD_DIR)) == before


def test_rejects_too_many_photos():
    files = [("photos", (f"{i}.png", PNG, "image/png")) for i in range(6)]
    assert client.post("/api/properties", data=AD, files=files).status_code == 400


def test_rejects_oversized_photo():
    big = PNG + b"0" * (5 * 1024 * 1024 + 10)
    r = client.post("/api/properties", data=AD, files=[("photos", ("big.png", big, "image/png"))])
    assert r.status_code == 400 and "5 MB" in r.json()["detail"]


def test_bad_second_file_does_not_orphan_first():
    """Validation happens before storing, so a bad file can't leave a good one behind."""
    before = set(os.listdir(storage.UPLOAD_DIR))
    files = [("photos", ("ok.png", PNG, "image/png")), ("photos", ("bad.txt", b"x", "text/plain"))]
    assert client.post("/api/properties", data=AD, files=files).status_code == 400
    assert set(os.listdir(storage.UPLOAD_DIR)) == before


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

def test_admin_requires_auth():
    assert client.get("/admin/properties").status_code == 401
    assert client.get("/admin/properties", auth=("admin", "wrong")).status_code == 401


def test_admin_sees_user_posted_ad():
    pid = client.post("/api/properties", data=AD).json()["property_id"]
    rows = client.get("/admin/properties", auth=AUTH).json()
    assert pid in [r["id"] for r in rows]
    assert next(r for r in rows if r["id"] == pid)["posted_by"] == "user"


def test_admin_create_without_photo_gets_logo():
    r = client.post("/admin/properties", data=AD, auth=AUTH)
    assert r.status_code == 200
    prop = client.get(f"/api/properties/{r.json()['property_id']}").json()
    assert "hosur-all-property-logo" in prop["images"][0]["url"]


def test_admin_edit_adding_photo_removes_logo_placeholder():
    pid = client.post("/admin/properties", data=AD, auth=AUTH).json()["property_id"]
    r = client.put(
        f"/admin/properties/{pid}", data=AD, auth=AUTH,
        files=[("photos", ("new.png", PNG, "image/png"))],
    )
    assert r.status_code == 200, r.text
    imgs = client.get(f"/api/properties/{pid}").json()["images"]
    assert len(imgs) == 1 and "hosur-all-property-logo" not in imgs[0]["url"]


def test_admin_removing_last_photo_restores_logo():
    pid = client.post(
        "/admin/properties", data=AD, auth=AUTH,
        files=[("photos", ("a.png", PNG, "image/png"))],
    ).json()["property_id"]
    img_id = client.get(f"/api/properties/{pid}").json()["images"][0]["id"]
    r = client.put(f"/admin/properties/{pid}", data=dict(AD, remove_image_ids=str(img_id)), auth=AUTH)
    assert r.status_code == 200
    imgs = client.get(f"/api/properties/{pid}").json()["images"]
    assert len(imgs) == 1 and "hosur-all-property-logo" in imgs[0]["url"]


def test_deleting_listing_never_deletes_shared_logo_file():
    pid = client.post("/api/properties", data=AD).json()["property_id"]
    logo_path = os.path.join(storage.UPLOAD_DIR, "hosur-all-property-logo.png")
    assert os.path.exists(logo_path)
    assert client.delete(f"/admin/properties/{pid}", auth=AUTH).status_code == 200
    assert os.path.exists(logo_path), "shared logo must survive deleting a listing"
    # ...and other listings using it still work
    pid2 = client.post("/api/properties", data=AD).json()["property_id"]
    url = client.get(f"/api/properties/{pid2}").json()["images"][0]["url"]
    assert client.get(url).status_code == 200


def test_admin_test_email_endpoint(sent_emails):
    r = client.post("/admin/test-email", auth=AUTH)
    assert r.status_code == 200, r.text
    assert len(sent_emails) == 1 and sent_emails[0]["to"] == ["nostalgic1235@gmail.com"]
    assert client.post("/admin/test-email").status_code == 401


# ---------------------------------------------------------------------------
# Supabase Storage mode (in-memory fake bucket)
# ---------------------------------------------------------------------------

class FakeBucket:
    def __init__(self, store):
        self.store = store

    def upload(self, path, file, file_options=None):
        if file_options and file_options.get("upsert") == "false" and path in self.store:
            raise RuntimeError("duplicate")
        self.store[path] = bytes(file)
        return {"path": path}

    def get_public_url(self, path):
        return f"https://proj.supabase.co/storage/v1/object/public/property-images/{path}"

    def remove(self, paths):
        for p in paths:
            self.store.pop(p, None)
        return []


class FakeSupabase:
    def __init__(self):
        self.objects = {}
        self.storage = self

    def from_(self, bucket):
        assert bucket == "property-images"
        return FakeBucket(self.objects)


@pytest.fixture()
def supabase_mode(monkeypatch):
    fake = FakeSupabase()
    monkeypatch.setattr(storage, "USE_SUPABASE", True)
    monkeypatch.setattr(storage, "_client", fake)
    monkeypatch.setattr(storage, "_logo_url_cache", None)
    return fake


def test_supabase_upload_returns_public_https_url(supabase_mode, sent_emails):
    r = client.post("/api/properties", data=AD, files=[("photos", ("a.png", PNG, "image/png"))])
    assert r.status_code == 200, r.text
    img = client.get(f"/api/properties/{r.json()['property_id']}").json()["images"][0]
    assert img["url"].startswith("https://proj.supabase.co/storage/v1/object/public/property-images/")
    assert len(supabase_mode.objects) == 1
    # email carries the same absolute Supabase URL so photos show in the inbox
    assert img["url"] in sent_emails[0]["html"]


def test_supabase_logo_uploaded_once_and_reused(supabase_mode, sent_emails):
    r1 = client.post("/api/properties", data=AD)
    r2 = client.post("/api/properties", data=AD)
    u1 = client.get(f"/api/properties/{r1.json()['property_id']}").json()["images"][0]["url"]
    u2 = client.get(f"/api/properties/{r2.json()['property_id']}").json()["images"][0]["url"]
    assert u1 == u2 and u1.startswith("https://proj.supabase.co/")
    assert list(supabase_mode.objects) == [storage.LOGO_OBJECT_NAME]
    # the logo URL is absolute, so it renders in the email too
    assert u1 in sent_emails[0]["html"]


def test_supabase_delete_removes_photo_but_keeps_logo(supabase_mode):
    a = client.post("/api/properties", data=AD, files=[("photos", ("a.png", PNG, "image/png"))]).json()["property_id"]
    b = client.post("/api/properties", data=AD).json()["property_id"]  # uses logo
    assert len(supabase_mode.objects) == 2
    client.delete(f"/admin/properties/{a}", auth=AUTH)
    client.delete(f"/admin/properties/{b}", auth=AUTH)
    assert list(supabase_mode.objects) == [storage.LOGO_OBJECT_NAME]


def test_supabase_upload_failure_returns_clean_error(supabase_mode, monkeypatch):
    def boom(self, path, file, file_options=None):
        raise RuntimeError("bucket not found")
    monkeypatch.setattr(FakeBucket, "upload", boom)
    r = client.post("/api/properties", data=AD, files=[("photos", ("a.png", PNG, "image/png"))])
    assert r.status_code == 502
    assert "try again" in r.json()["detail"].lower()
    assert "bucket not found" not in r.text, "internal error details must not leak to users"


def test_supabase_logo_upload_failure_still_publishes_listing(supabase_mode, monkeypatch):
    def boom(self, path, file, file_options=None):
        raise RuntimeError("storage down")
    monkeypatch.setattr(FakeBucket, "upload", boom)
    r = client.post("/api/properties", data=AD)
    assert r.status_code == 200, "listing must not be blocked just because the logo couldn't upload"


# ---------------------------------------------------------------------------
# Email failure explanations (exact wording taken from Resend's docs/errors)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, must_contain", [
    ("You can only send testing emails to your own email address (ab***@gmail.com). "
     "To send emails to other recipients, please verify a domain at resend.com/domains",
     "ADMIN_NOTIFY_EMAIL"),
    ("API key is invalid", "RESEND_API_KEY"),
    ("The example.com domain is not verified", "EMAIL_FROM"),
    ("Expected JSON response but got: text/plain", "network"),
])
def test_email_errors_are_explained_in_plain_english(raw, must_contain):
    assert must_contain in notifications.explain_send_error(RuntimeError(raw))


def test_test_email_endpoint_reports_the_real_reason(monkeypatch):
    import resend

    def deny(params, options=None):
        raise RuntimeError("You can only send testing emails to your own email address (x@y.com). "
                           "To send emails to other recipients, please verify a domain")

    monkeypatch.setattr(resend.Emails, "send", staticmethod(deny))
    monkeypatch.setattr(notifications, "RESEND_API_KEY", "re_test_key")
    r = client.post("/admin/test-email", auth=AUTH)
    assert r.status_code == 502
    assert "ADMIN_NOTIFY_EMAIL" in r.json()["detail"], "admin should be told exactly how to fix it"


# ---------------------------------------------------------------------------
# Terms & Policy
# ---------------------------------------------------------------------------

def test_posting_ad_requires_terms_accepted():
    no_terms = {k: v for k, v in AD.items() if k != "terms_accepted"}
    r = client.post("/api/properties", data=no_terms)
    assert r.status_code == 400
    assert "Terms" in r.json()["detail"]

    r = client.post("/api/properties", data=dict(AD, terms_accepted="false"))
    assert r.status_code == 400

    r = client.post("/api/properties", data=AD)
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# Listing types (Sell / Buy / Rental & Lease / Tenants / JV/JD) + price type
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sent, expected", [
    ("Sell", "Sell"), ("Buy", "Buy"), ("Rental & Lease", "Rental & Lease"),
    ("Tenants", "Tenants"), ("JV/JD", "JV/JD"),
    # legacy values from older pages / saved ads are mapped to the new ones
    ("Sale", "Sell"), ("Rental", "Rental & Lease"), ("Lease", "Rental & Lease"),
])
def test_all_listing_types_are_accepted_and_normalised(sent, expected, sent_emails):
    r = client.post("/api/properties", data={**AD, "type": sent})
    assert r.status_code == 200, r.text
    pid = r.json()["property_id"]
    assert client.get(f"/api/properties/{pid}").json()["type"] == expected


def test_unknown_listing_type_is_rejected(sent_emails):
    r = client.post("/api/properties", data={**AD, "type": "Barter"})
    assert r.status_code == 400
    assert "Sell" in r.json()["detail"]


def test_price_type_is_saved_and_emailed(sent_emails):
    r = client.post("/api/properties", data={**AD, "price_type": "Fixed"})
    assert r.status_code == 200
    pid = r.json()["property_id"]
    got = client.get(f"/api/properties/{pid}").json()
    assert got["price"] == 15000 and got["price_type"] == "Fixed"
    assert "Fixed" in sent_emails[0]["html"]


def test_price_requires_fixed_or_negotiable(sent_emails):
    ad = {k: v for k, v in AD.items() if k != "price_type"}
    r = client.post("/api/properties", data=ad)
    assert r.status_code == 400
    assert "Fixed or Negotiable" in r.json()["detail"]


def test_no_price_means_no_price_type(sent_emails):
    ad = {k: v for k, v in AD.items() if k != "price"}
    r = client.post("/api/properties", data={**ad, "price_type": "Negotiable"})
    assert r.status_code == 200
    got = client.get(f"/api/properties/{r.json()['property_id']}").json()
    assert got["price"] is None and got["price_type"] is None


def test_list_filter_accepts_new_and_legacy_type_names(sent_emails):
    client.post("/api/properties", data={**AD, "type": "Tenants", "title": "Need 2BHK on rent"})
    for q in ("Tenants", "tenants"):
        rows = client.get("/api/properties", params={"type": q}).json()
        assert rows and all(p["type"] == "Tenants" for p in rows)
    rows = client.get("/api/properties", params={"type": "Rental"}).json()
    assert rows and all(p["type"] == "Rental & Lease" for p in rows)


def test_legacy_rows_are_migrated_on_startup():
    import database
    db = SessionLocal()
    try:
        db.add_all([
            Property(title="old sale", type="Sale", category="Apartments", description="x",
                     location="Hosur", phone="9000000000", status="approved"),
            Property(title="old lease", type="Lease", category="Apartments", description="x",
                     location="Hosur", phone="9000000000", status="approved"),
        ])
        db.commit()
    finally:
        db.close()
    database._run_light_migrations()
    db = SessionLocal()
    try:
        assert db.query(Property).filter(Property.title == "old sale").one().type == "Sell"
        assert db.query(Property).filter(Property.title == "old lease").one().type == "Rental & Lease"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Price unit (Total / Per Sq.ft / Per Cent / Per Acre)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("unit", ["Total", "Per Sq.ft", "Per Cent", "Per Acre"])
def test_price_unit_is_saved(sent_emails, unit):
    r = client.post("/api/properties", data={**AD, "price_unit": unit})
    assert r.status_code == 200
    got = client.get(f"/api/properties/{r.json()['property_id']}").json()
    assert got["price_unit"] == unit


def test_price_unit_defaults_to_total_and_is_emailed(sent_emails):
    r = client.post("/api/properties", data=AD)  # AD has no price_unit
    assert r.status_code == 200
    got = client.get(f"/api/properties/{r.json()['property_id']}").json()
    assert got["price_unit"] == "Total"

    sent_emails.clear()
    r = client.post("/api/properties", data={**AD, "price_unit": "Per Cent"})
    assert r.status_code == 200
    assert "/ Cent" in sent_emails[0]["html"]


def test_unknown_price_unit_is_rejected(sent_emails):
    r = client.post("/api/properties", data={**AD, "price_unit": "Per Bucket"})
    assert r.status_code == 400
    assert "Per Acre" in r.json()["detail"]


def test_no_price_means_no_price_unit(sent_emails):
    ad = {k: v for k, v in AD.items() if k not in ("price", "price_type")}
    r = client.post("/api/properties", data={**ad, "price_unit": "Per Acre"})
    assert r.status_code == 200
    got = client.get(f"/api/properties/{r.json()['property_id']}").json()
    assert got["price"] is None and got["price_unit"] is None


def test_budget_filter_only_matches_total_prices(sent_emails):
    client.post("/api/properties", data={**AD, "title": "RATE-PER-SQFT", "price": "2500", "price_unit": "Per Sq.ft"})
    client.post("/api/properties", data={**AD, "title": "TOTAL-PRICED", "price": "2600", "price_unit": "Total"})
    titles = {p["title"] for p in client.get("/api/properties", params={"max_price": 3000}).json()}
    assert "TOTAL-PRICED" in titles and "RATE-PER-SQFT" not in titles


def test_admin_can_set_and_edit_price_unit(sent_emails):
    data = {**AD, "price_unit": "Per Acre"}
    r = client.post("/admin/properties", data=data, auth=AUTH)
    assert r.status_code == 200, r.text
    pid = r.json()["property_id"]
    r = client.put(f"/admin/properties/{pid}", data={**AD, "price_unit": "Per Sq.ft"}, auth=AUTH)
    assert r.status_code == 200, r.text
    db = SessionLocal()
    try:
        assert db.query(Property).get(pid).price_unit == "Per Sq.ft"
    finally:
        db.close()


def test_old_priced_rows_are_backfilled_to_total():
    import database
    db = SessionLocal()
    try:
        db.add(Property(title="legacy priced", type="Sell", category="Apartments", description="x",
                        location="Hosur", phone="9000000000", status="approved", price=500000))
        db.commit()
    finally:
        db.close()
    database._run_light_migrations()
    db = SessionLocal()
    try:
        assert db.query(Property).filter(Property.title == "legacy priced").one().price_unit == "Total"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Visitor entry form + admin visitors / blocking
# ---------------------------------------------------------------------------

VISITOR = {
    "name": "Ravi Kumar",
    "phone": "98765 43210",
    "email": "ravi@example.com",
    "place": "Hosur",
    "category": "Buyers",
}


def _visitors():
    return client.get("/admin/visitors", auth=AUTH).json()


def test_visitor_entry_is_saved_and_visible_to_admin():
    r = client.post("/api/visitors/register", data={**VISITOR, "phone": "9000000001"})
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True

    rows = [v for v in _visitors()["visitors"] if v["phone"] == "9000000001"]
    assert len(rows) == 1
    v = rows[0]
    assert (v["name"], v["email"], v["place"], v["category"]) == (
        "Ravi Kumar", "ravi@example.com", "Hosur", "Buyers")
    assert v["visit_count"] == 1 and v["blocked"] is False
    assert v["last_visit"].endswith("Z")


def test_visitor_email_is_optional():
    # field left out entirely
    no_email = {k: v for k, v in VISITOR.items() if k != "email"}
    r = client.post("/api/visitors/register", data={**no_email, "phone": "9000000070"})
    assert r.status_code == 200, r.text
    # field sent but empty / spaces only
    r = client.post("/api/visitors/register", data={**VISITOR, "email": "   ", "phone": "9000000071"})
    assert r.status_code == 200, r.text
    rows = {v["phone"]: v for v in _visitors()["visitors"]}
    assert rows["9000000070"]["email"] is None
    assert rows["9000000071"]["email"] is None


def test_visitor_email_if_given_must_still_be_valid():
    r = client.post("/api/visitors/register", data={**VISITOR, "email": "not-an-email", "phone": "9000000072"})
    assert r.status_code == 400
    assert "email" in r.json()["detail"].lower()


def test_old_database_with_required_email_is_relaxed_on_startup(tmp_path):
    import sqlite3, subprocess, sys
    db_file = tmp_path / "property.db"
    con = sqlite3.connect(db_file)
    con.executescript("""
        CREATE TABLE visitors (
            id INTEGER PRIMARY KEY, name VARCHAR(120) NOT NULL, phone VARCHAR(20) NOT NULL UNIQUE,
            email VARCHAR(255) NOT NULL, place VARCHAR(120) NOT NULL, category VARCHAR(30) NOT NULL,
            visit_count INTEGER NOT NULL, first_visit DATETIME, last_visit DATETIME);
        INSERT INTO visitors VALUES (1,'Old Timer','9111111111','old@example.com','Hosur','Buyers',3,NULL,NULL);
    """)
    con.commit(); con.close()
    code = (
        "import database as d, sqlalchemy as sa;"
        "s=d.SessionLocal();"
        "s.add(d.Visitor(name='New One',phone='9222222222',email=None,place='Hosur',category='Buyers',visit_count=1));"
        "s.commit();"
        "print(sorted((v.phone, v.email) for v in s.query(d.Visitor)))"
    )
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, env={**env, "PYTHONPATH": os.path.dirname(os.path.abspath(__file__))},
                         capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    assert "('9111111111', 'old@example.com')" in out.stdout and "('9222222222', None)" in out.stdout


def test_same_phone_in_any_format_is_one_visitor_and_counts_visits():
    for typed in ("9000000002", "+91 90000 00002", "09000000002", "91-9000000002"):
        assert client.post("/api/visitors/register",
                           data={**VISITOR, "phone": typed}).status_code == 200
    rows = [v for v in _visitors()["visitors"] if v["phone"] == "9000000002"]
    assert len(rows) == 1
    assert rows[0]["visit_count"] == 4


def test_returning_visitor_details_are_refreshed():
    client.post("/api/visitors/register", data={**VISITOR, "phone": "9000000003"})
    client.post("/api/visitors/register", data={
        **VISITOR, "phone": "9000000003", "name": "Ravi K", "place": "Chennai", "category": "Agent"})
    v = [x for x in _visitors()["visitors"] if x["phone"] == "9000000003"][0]
    assert (v["name"], v["place"], v["category"], v["visit_count"]) == ("Ravi K", "Chennai", "Agent", 2)


@pytest.mark.parametrize("category", ["Sellers", "Buyers", "Rent & Lease", "Tenants", "JV/JD", "Agent"])
def test_all_six_visitor_categories_are_accepted(category):
    r = client.post("/api/visitors/register", data={**VISITOR, "phone": "9000000010", "category": category})
    assert r.status_code == 200, r.text
    v = [x for x in _visitors()["visitors"] if x["phone"] == "9000000010"][0]
    assert v["category"] == category


@pytest.mark.parametrize("bad", [
    {"name": " "}, {"name": "A"}, {"phone": "12345"}, {"phone": "abcdefghij"},
    {"email": "not-an-email"}, {"place": ""}, {"category": "Wizard"},
])
def test_visitor_entry_rejects_bad_input(bad):
    r = client.post("/api/visitors/register", data={**VISITOR, "phone": "9000000020", **bad})
    assert r.status_code in (400, 422), r.text
    assert not [v for v in _visitors()["visitors"] if v["phone"] == "9000000020"]


def test_blocked_phone_cannot_enter_and_unblock_restores_access():
    phone = "9000000030"
    assert client.post("/api/visitors/register", data={**VISITOR, "phone": phone}).status_code == 200

    r = client.post("/admin/visitors/block", data={"phone": "+91 90000 00030", "reason": "spam"}, auth=AUTH)
    assert r.status_code == 200 and r.json()["success"] is True

    # asked on every page load -> already-browsing people are kicked out too
    assert client.post("/api/visitors/check", data={"phone": phone}).json() == {"blocked": True}

    # and cannot get in again, however the number is typed
    for typed in (phone, "+91" + phone, "0" + phone):
        r = client.post("/api/visitors/register", data={**VISITOR, "phone": typed})
        assert r.status_code == 403
        assert r.json()["blocked"] is True

    v = [x for x in _visitors()["visitors"] if x["phone"] == phone][0]
    assert v["blocked"] is True and v["blocked_reason"] == "spam"
    assert v["visit_count"] == 1, "blocked attempts must not be counted as visits"

    client.post("/admin/visitors/unblock", data={"phone": phone}, auth=AUTH)
    assert client.post("/api/visitors/check", data={"phone": phone}).json() == {"blocked": False}
    assert client.post("/api/visitors/register", data={**VISITOR, "phone": phone}).status_code == 200
    assert [x for x in _visitors()["visitors"] if x["phone"] == phone][0]["blocked"] is False


def test_admin_can_block_a_number_that_never_registered():
    r = client.post("/admin/visitors/block", data={"phone": "9000000040"}, auth=AUTH)
    assert r.status_code == 200
    row = [v for v in _visitors()["visitors"] if v["phone"] == "9000000040"][0]
    assert row["blocked"] is True and row["id"] is None
    assert client.post("/api/visitors/register",
                       data={**VISITOR, "phone": "9000000040"}).status_code == 403
    client.post("/admin/visitors/unblock", data={"phone": "9000000040"}, auth=AUTH)


def test_block_rejects_invalid_phone():
    assert client.post("/admin/visitors/block", data={"phone": "123"}, auth=AUTH).status_code == 400


def test_visitor_admin_routes_need_login():
    assert client.get("/admin/visitors").status_code == 401
    assert client.post("/admin/visitors/block", data={"phone": "9000000050"}).status_code == 401
    assert client.post("/admin/visitors/unblock", data={"phone": "9000000050"}).status_code == 401
    assert client.get("/admin/visitors", auth=("admin", "wrong")).status_code == 401


def test_visitor_stats_and_public_counter():
    before = _visitors()["stats"]
    pub_before = client.get("/api/visitor-count").json()
    client.post("/api/visitors/register", data={**VISITOR, "phone": "9000000060"})
    client.post("/api/visitors/register", data={**VISITOR, "phone": "9000000060"})
    after = _visitors()["stats"]
    assert after["total_visitors"] == before["total_visitors"] + 1
    assert after["total_visits"] == before["total_visits"] + 2
    assert after["new_today"] >= 1 and after["visited_today"] >= 1
    pub = client.get("/api/visitor-count").json()
    assert pub["total_visitors"] == pub_before["total_visitors"] + 1
    assert pub["total_visits"] == pub_before["total_visits"] + 2


def test_check_with_garbage_phone_is_not_blocked():
    assert client.post("/api/visitors/check", data={"phone": "xx"}).json() == {"blocked": False}
