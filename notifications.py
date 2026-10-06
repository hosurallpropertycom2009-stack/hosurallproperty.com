"""
Hosur All Property — Email notifications
==========================================
Sends the site owner an email whenever someone posts a new property ad,
containing every detail of the listing plus a button that opens the admin
page.

Uses Resend (https://resend.com). Configured through environment variables:

    RESEND_API_KEY     your Resend API key                    (required)
    ADMIN_NOTIFY_EMAIL where the alert is sent                (default below)
    EMAIL_FROM         the "from" address                     (default below)
    ADMIN_PAGE_URL     link placed in the email's button      (see below)

Design notes:
  * send_new_property_email() NEVER raises. It is run as a background task
    after the ad has already been saved, so a Resend outage, a bad key, or
    a network blip can only ever lose the *email* — it can never fail or
    roll back the user's ad submission.
  * All user-supplied text is HTML-escaped before going into the email so a
    malicious title/description can't inject markup into the admin's inbox.
"""

import html
import logging
import os
from datetime import datetime
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("hosur.notifications")

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "").strip()
ADMIN_NOTIFY_EMAIL = os.environ.get("ADMIN_NOTIFY_EMAIL", "nostalgic1235@gmail.com").strip()

# "onboarding@resend.dev" is Resend's shared test sender. It works with no
# domain setup, but Resend only lets it deliver to the email address that
# owns the Resend account. To email anyone else, verify your own domain in
# Resend and set EMAIL_FROM to e.g. "Hosur All Property <alerts@yourdomain.com>".
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Hosur All Property <onboarding@resend.dev>").strip()

# Where the "Open Admin Page" button goes. Set this to your deployed admin
# URL (e.g. https://hosurallproperty.com/admin.html). The default is the
# local dev address so the button still works while testing.
ADMIN_PAGE_URL = os.environ.get("ADMIN_PAGE_URL", "http://127.0.0.1:5500/admin.html").strip()

# The most recent send failure, in plain English. The admin "Test Email" button
# reads this so it can tell you *why* an email didn't go out instead of just
# saying "it failed".
last_error: Optional[str] = None

BRAND_GREEN = "#1f5f2e"
BRAND_GOLD = "#c9a24b"


def _esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _format_price(price: Optional[float], unit: Optional[str] = None) -> str:
    if price is None:
        return "On Request"
    text = "₹ " + f"{int(price):,}"
    # "Total" (or no unit) reads "₹ 45,00,000 (Total)"; the rates read "₹ 2,500 / Sq.ft".
    if unit and unit != "Total":
        text += " / " + unit.replace("Per ", "", 1)
    else:
        text += " (Total)"
    return text


def _row(label: str, value_html: str) -> str:
    return (
        '<tr>'
        f'<td style="padding:10px 14px;border-bottom:1px solid #eee;color:#6b746e;'
        f'font-size:13px;width:130px;vertical-align:top;">{_esc(label)}</td>'
        f'<td style="padding:10px 14px;border-bottom:1px solid #eee;color:#1c2620;'
        f'font-size:15px;vertical-align:top;">{value_html}</td>'
        '</tr>'
    )


def build_email_html(prop: dict, image_urls: List[str], used_default_logo: bool) -> str:
    """Render the notification email. `prop` is a plain dict of the listing."""
    photos_html = ""
    if used_default_logo and not image_urls:
        # Local/dev mode: the logo's URL is relative, so it can't be embedded
        # in an email. Still tell the admin no photo was uploaded.
        photos_html = (
            '<div style="padding:18px 14px 6px;font-size:13px;color:#6b746e;">'
            'No photo was uploaded, so the Hosur All Property logo is being shown '
            'on the listing.</div>'
        )
    elif image_urls:
        imgs = "".join(
            f'<a href="{_esc(u)}" style="text-decoration:none;">'
            f'<img src="{_esc(u)}" alt="Property photo" width="170" '
            f'style="width:170px;height:120px;object-fit:cover;border-radius:8px;'
            f'margin:0 8px 8px 0;border:1px solid #e3e6e1;"></a>'
            for u in image_urls
        )
        caption = (
            "No photo was uploaded, so the Hosur All Property logo is being shown."
            if used_default_logo
            else f"{len(image_urls)} photo(s) uploaded"
        )
        photos_html = (
            f'<div style="padding:18px 14px 6px;">'
            f'<div style="font-size:13px;color:#6b746e;margin-bottom:8px;">{_esc(caption)}</div>'
            f'{imgs}</div>'
        )

    posted_at = prop.get("created_at")
    if isinstance(posted_at, datetime):
        posted_str = posted_at.strftime("%d %b %Y, %I:%M %p UTC")
    else:
        posted_str = _esc(posted_at or "")

    phone = _esc(prop.get("phone"))
    contact_name = prop.get("contact_name")
    email_addr = _esc(prop.get("email")) if prop.get("email") else None
    skype = prop.get("skype")
    listed_by = prop.get("listed_by")
    tags = prop.get("tags")
    address = prop.get("address")

    valid_till_raw = prop.get("valid_till")
    if isinstance(valid_till_raw, datetime):
        valid_till_str = valid_till_raw.strftime("%d %b %Y")
    else:
        valid_till_str = _esc(valid_till_raw) if valid_till_raw else None

    rows = "".join([
        _row("Title", f"<strong>{_esc(prop.get('title'))}</strong>"),
        _row("Type", _esc(prop.get("type"))),
        _row("Category", _esc(prop.get("category"))),
        _row("Location", _esc(prop.get("location"))),
        *([_row("Address", _esc(address))] if address else []),
        _row("Price", _esc(_format_price(prop.get("price"), prop.get("price_unit"))) + (f" ({_esc(prop.get('price_type'))})" if prop.get("price") is not None and prop.get("price_type") else "")),
        *([_row("Tags", _esc(tags))] if tags else []),
        *([_row("Valid till", valid_till_str)] if valid_till_str else []),
        *([_row("Contact name", _esc(contact_name))] if contact_name else []),
        *([_row("Listed by", _esc(listed_by))] if listed_by else []),
        _row("Poster's phone", f'<a href="tel:{phone}" style="color:{BRAND_GREEN};">{phone}</a>'),
        *([_row("Poster's email", f'<a href="mailto:{email_addr}" style="color:{BRAND_GREEN};">{email_addr}</a>')] if email_addr else []),
        *([_row("Poster's Skype", _esc(skype))] if skype else []),
        _row("Description", _esc(prop.get("description")).replace("\n", "<br>")),
        _row("Listing ID", f"#{_esc(prop.get('id'))}"),
        _row("Posted", posted_str),
        _row("Status", _esc(prop.get("status"))),
    ])

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f1e8;font-family:Segoe UI,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f1e8;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0"
             style="max-width:600px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;
                    border:1px solid #e3e6e1;">
        <tr>
          <td style="background:{BRAND_GREEN};padding:22px 24px;">
            <div style="color:{BRAND_GOLD};font-size:12px;letter-spacing:.14em;text-transform:uppercase;">
              Hosur All Property
            </div>
            <div style="color:#ffffff;font-size:22px;font-weight:600;margin-top:4px;">
              New property ad posted
            </div>
          </td>
        </tr>
        <tr><td>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>
        </td></tr>
        <tr><td>{photos_html}</td></tr>
        <tr>
          <td align="center" style="padding:22px 24px 28px;">
            <a href="{_esc(ADMIN_PAGE_URL)}"
               style="display:inline-block;background:{BRAND_GREEN};color:#ffffff;text-decoration:none;
                      padding:13px 26px;border-radius:9px;font-size:15px;font-weight:600;">
              Open Admin Page
            </a>
            <div style="font-size:12px;color:#8a938d;margin-top:12px;">
              Review, edit or remove this listing from the admin console.
            </div>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def build_email_text(prop: dict) -> str:
    """Plain-text fallback for mail clients that don't render HTML."""
    optional_lines = ""
    if prop.get("address"):
        optional_lines += f"Address:     {prop.get('address')}\n"
    if prop.get("tags"):
        optional_lines += f"Tags:        {prop.get('tags')}\n"
    if prop.get("valid_till"):
        vt = prop.get("valid_till")
        optional_lines += f"Valid till:  {vt.strftime('%d %b %Y') if isinstance(vt, datetime) else vt}\n"
    if prop.get("contact_name"):
        optional_lines += f"Contact:     {prop.get('contact_name')}\n"
    if prop.get("listed_by"):
        optional_lines += f"Listed by:   {prop.get('listed_by')}\n"
    if prop.get("email"):
        optional_lines += f"Email:       {prop.get('email')}\n"
    if prop.get("skype"):
        optional_lines += f"Skype:       {prop.get('skype')}\n"
    return (
        "New property ad posted on Hosur All Property\n"
        "--------------------------------------------\n"
        f"Title:       {prop.get('title')}\n"
        f"Type:        {prop.get('type')}\n"
        f"Category:    {prop.get('category')}\n"
        f"Location:    {prop.get('location')}\n"
        f"Price:       {_format_price(prop.get('price'), prop.get('price_unit'))}"
        f"{' (' + prop.get('price_type') + ')' if prop.get('price') is not None and prop.get('price_type') else ''}\n"
        f"Phone:       {prop.get('phone')}\n"
        f"{optional_lines}"
        f"Listing ID:  #{prop.get('id')}\n"
        f"Status:      {prop.get('status')}\n\n"
        f"Description:\n{prop.get('description')}\n\n"
        f"Admin page: {ADMIN_PAGE_URL}\n"
    )


def build_contact_email_html(lead: dict) -> str:
    """Render the "General Enquiry" contact-form notification email."""
    phone = _esc(lead.get("phone"))
    email = _esc(lead.get("email"))

    posted_at = lead.get("created_at")
    if isinstance(posted_at, datetime):
        posted_str = posted_at.strftime("%d %b %Y, %I:%M %p UTC")
    else:
        posted_str = _esc(posted_at or "")

    rows = "".join([
        _row("Name", f"<strong>{_esc(lead.get('name'))}</strong>"),
        _row("Phone", f'<a href="tel:{phone}" style="color:{BRAND_GREEN};">{phone}</a>'),
        _row("Email", f'<a href="mailto:{email}" style="color:{BRAND_GREEN};">{email}</a>'),
        _row("Place", _esc(lead.get("place"))),
        _row("Message", _esc(lead.get("note")).replace("\n", "<br>")),
        _row("Received", posted_str),
    ])

    return f"""<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f1e8;font-family:Segoe UI,Helvetica,Arial,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f1e8;padding:24px 12px;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0"
             style="max-width:600px;width:100%;background:#ffffff;border-radius:14px;overflow:hidden;
                    border:1px solid #e3e6e1;">
        <tr>
          <td style="background:{BRAND_GREEN};padding:22px 24px;">
            <div style="color:{BRAND_GOLD};font-size:12px;letter-spacing:.14em;text-transform:uppercase;">
              Hosur All Property
            </div>
            <div style="color:#ffffff;font-size:22px;font-weight:600;margin-top:4px;">
              New general enquiry
            </div>
          </td>
        </tr>
        <tr><td>
          <table role="presentation" width="100%" cellpadding="0" cellspacing="0">{rows}</table>
        </td></tr>
        <tr>
          <td align="center" style="padding:22px 24px 28px;">
            <a href="tel:{phone}"
               style="display:inline-block;background:{BRAND_GREEN};color:#ffffff;text-decoration:none;
                      padding:13px 26px;border-radius:9px;font-size:15px;font-weight:600;">
              Call {_esc(lead.get('name'))}
            </a>
            <div style="font-size:12px;color:#8a938d;margin-top:12px;">
              Sent from the "Send a Message" form on the Contact page.
            </div>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def build_contact_email_text(lead: dict) -> str:
    """Plain-text fallback for mail clients that don't render HTML."""
    return (
        "New general enquiry on Hosur All Property\n"
        "------------------------------------------\n"
        f"Name:     {lead.get('name')}\n"
        f"Phone:    {lead.get('phone')}\n"
        f"Email:    {lead.get('email')}\n"
        f"Place:    {lead.get('place')}\n\n"
        f"Message:\n{lead.get('note')}\n"
    )


def send_contact_enquiry_email(lead: dict) -> bool:
    """
    Email the admin about a new "General Enquiry" contact-form submission.
    Returns True if Resend accepted it, False otherwise. Never raises (see
    module docstring) — the enquiry is already saved to the database before
    this runs, so an email hiccup can never lose the lead itself.
    """
    global last_error

    if not RESEND_API_KEY:
        last_error = "RESEND_API_KEY is not set on the server."
        log.warning("RESEND_API_KEY is not set — skipping contact-enquiry email for lead #%s", lead.get("id"))
        return False

    try:
        import resend

        resend.api_key = RESEND_API_KEY
        params = {
            "from": EMAIL_FROM,
            "to": [ADMIN_NOTIFY_EMAIL],
            "reply_to": lead.get("email") or None,
            "subject": f"New enquiry: {lead.get('name')} ({lead.get('place') or 'Hosur'})",
            "html": build_contact_email_html(lead),
            "text": build_contact_email_text(lead),
        }
        # Resend rejects a null reply_to key outright — drop it if there's no email.
        params = {k: v for k, v in params.items() if v is not None}
        response = resend.Emails.send(params)
        last_error = None
        log.info("Contact-enquiry email sent for lead #%s (resend id=%s)", lead.get("id"), response.get("id"))
        return True
    except Exception as exc:  # noqa: BLE001 - must never break the enquiry submission
        last_error = explain_send_error(exc)
        log.error("Failed to send contact-enquiry email for lead #%s: %s", lead.get("id"), exc)
        return False


def explain_send_error(exc: Exception) -> str:
    """Turn a raw Resend/network exception into advice the admin can act on."""
    raw = str(exc)
    low = raw.lower()
    if "only send testing emails to your own email" in low or "verify a domain" in low:
        return (
            "Resend only lets the shared 'onboarding@resend.dev' sender deliver to the email "
            "address your Resend account was created with. Either set ADMIN_NOTIFY_EMAIL to "
            "that address, or verify your own domain at resend.com/domains and set EMAIL_FROM "
            "to an address on it."
        )
    if "api key is invalid" in low or "invalid api key" in low or "unauthorized" in low or "401" in low:
        return "Resend rejected the API key. Check RESEND_API_KEY on the server."
    if "domain is not verified" in low or "not verified" in low:
        return "The EMAIL_FROM domain isn't verified in Resend yet (resend.com/domains)."
    if "expected json response but got" in low or "host_not_allowed" in low:
        return "The server couldn't reach Resend (network blocked). Check outbound internet access."
    return f"Resend error: {raw[:200]}"


def send_new_property_email(prop: dict, image_urls: List[str], used_default_logo: bool = False) -> bool:
    """
    Email the admin about a newly posted ad. Returns True if Resend accepted
    it, False otherwise. Never raises (see module docstring).

    `image_urls` must be absolute https URLs — email clients can't load
    relative paths like /property_images/x.jpg. Anything that isn't absolute
    is silently dropped from the email rather than showing a broken image.
    """
    global last_error

    if not RESEND_API_KEY:
        last_error = "RESEND_API_KEY is not set on the server."
        log.warning("RESEND_API_KEY is not set — skipping new-property email for #%s", prop.get("id"))
        return False

    try:
        import resend

        resend.api_key = RESEND_API_KEY
        absolute_urls = [u for u in image_urls if u.lower().startswith(("http://", "https://"))]

        params = {
            "from": EMAIL_FROM,
            "to": [ADMIN_NOTIFY_EMAIL],
            "subject": f"New property ad: {prop.get('title')} ({prop.get('location')})",
            "html": build_email_html(prop, absolute_urls, used_default_logo),
            "text": build_email_text(prop),
        }
        response = resend.Emails.send(params)
        last_error = None
        log.info("New-property email sent for #%s (resend id=%s)", prop.get("id"), response.get("id"))
        return True
    except Exception as exc:  # noqa: BLE001 - must never break the ad submission
        last_error = explain_send_error(exc)
        log.error("Failed to send new-property email for #%s: %s", prop.get("id"), exc)
        return False
