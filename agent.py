"""
Hosur All Property — AI Property Agent
=================================
A conversational agent that helps site visitors find properties by chatting
naturally, backed by Groq's `openai/gpt-oss-120b` model with real tool
calls against the live listings database — it never invents locations,
listings, or details; every factual claim it makes comes from a tool
result.

APIRouter, mounted by main.py at /agent/*. Not a standalone app.

Env vars required:
    GROQ_API_KEY   — from https://console.groq.com/keys

Endpoint:
    POST /agent/chat
        body: { "message": str, "history": [{"role": "user"|"assistant", "content": str}, ...] }
        returns: { "reply": str, "listings": [PropertyOut, ...] }

The `listings` array is populated whenever the agent's tool calls returned
specific properties worth showing as cards in the chat UI (e.g. search
results or a single property's full detail) — the frontend renders these
as rich cards with photos rather than making the model describe images in
text.

CONVERSATION FLOW
------------------
The system prompt below enforces a specific, repeatable flow rather than
leaving pacing entirely to the model's judgement:

  1. Greeting  -> introduce Hosur All Property, ask what they're looking for.
  2. Location choice -> call list_locations and present ONLY the real areas
     that currently have approved listings, as a short list to choose from
     (never a fixed/hardcoded list of towns — only what's actually in the
     database right now).
  3. Once a location is picked -> call search_properties for it and lead
     with the single best/most compelling listing there (highest price or
     most complete listing first), so a real photo card appears in the UI.
  4. Once the user shows interest in a specific property -> switch into a
     warm, honest "why this one is worth a look" mode: genuine, specific,
     persuasive detail pulled only from get_property_details — never hype
     or invented superlatives.
  5. Once interest is clear -> naturally ask for name + phone to have the
     team follow up, then call capture_lead.

All prices are in Indian Rupees (INR) and should read the way a Hosur
agent would say them (e.g. "₹90,00,000" / "90 lakh"), since every listing
in this system is in Hosur-area INR terms.
"""

import json
import os
import re
from typing import List, Optional

from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from database import get_db, Property, Lead, PropertyOut, normalize_listing_type
from locations import canonical_location, location_variants, location_sort_key

load_dotenv()
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")

router = APIRouter(prefix="/agent", tags=["agent"])

# Lazy import + client so the whole app doesn't fail to start if the groq
# package or key isn't configured yet — only /agent/chat needs it.
_groq_client = None


def get_groq_client():
    global _groq_client
    if _groq_client is None:
        if not GROQ_API_KEY:
            raise HTTPException(
                status_code=503,
                detail="AI agent isn't configured yet — GROQ_API_KEY is missing on the server.",
            )
        from groq import Groq
        _groq_client = Groq(api_key=GROQ_API_KEY)
    return _groq_client


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class ChatRequest(BaseModel):
    message: str
    history: List[ChatMessage] = []


class ChatResponse(BaseModel):
    reply: str
    listings: List[PropertyOut] = []


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------
# The persuasive/psychology angle the user asked for is handled here, not by
# ever letting the model invent facts: real expertise (accurate, specific,
# helpful detail pulled from tools) is what actually builds trust and makes
# someone want to leave their number — not exaggeration. The prompt pushes
# the model toward being genuinely useful first, and only asking for contact
# info once real interest is shown, as a natural next step rather than a
# hard sell.

SYSTEM_PROMPT = """You are the property assistant for Hosur All Property, a real estate \
consultancy in Hosur, Tamil Nadu, India (est. 2009). You help website visitors find \
homes, land, and commercial property through natural conversation.

HOW YOU WORK — this is critical:
- You have NO knowledge of what properties exist beyond what your tools tell you. \
Never say a location, category, or listing exists, and never state a price, size, or \
feature, unless it came from a tool result in this conversation.
- All prices are in Indian Rupees (INR). Speak about money the way a Hosur agent would \
— e.g. "₹90,00,000" or "90 lakh" — never dollars or any other currency.
- Each listing's price has a price_unit: "Total" (the whole property), "Per Sq.ft", \
"Per Cent" or "Per Acre". Always say which one when quoting a price — e.g. \
"₹2,500 per sq.ft" — and never present a per-unit rate as if it were the total price.

CONVERSATION FLOW — follow this shape every time, in order:

STEP 1 — Greeting. On the very first message (including a plain "hi"), introduce \
yourself in one line as the Hosur All Property assistant and ask what kind of \
property they're looking for (house/villa, land, commercial, rental). Don't call any \
tool yet — just ask.

STEP 2 — Location choice. As soon as they say what TYPE of property they want (house, \
land, commercial, rental, etc.), call list_locations. Present the real areas returned \
by the tool as a short list to choose from (only areas that actually have listings — \
never mention a place list_locations didn't return). Ask which area they'd like.

STEP 3 — Best pick for that location. Once they name a location, call \
search_properties for that location (optionally combined with the type/category they \
already told you). From the results, lead with the single strongest match — the one \
with the most complete listing or most compelling combination of location/price — and \
introduce it as your top pick for that area, in a couple of genuine, specific \
sentences. A photo card for it appears automatically in the UI — don't describe the \
image, just talk about what makes the property itself worth a look. Mention 1-2 other \
options exist if there are more, and invite them to see more or ask about this one.

STEP 4 — Deeper interest. When the user shows real interest in one specific property \
(asks for more detail, asks about visiting, price, or availability), call \
get_property_details for it and switch into genuine advisor mode: explain — using only \
real facts from the tool — why this property is worth considering for someone in their \
position (family size, budget, investment, etc. based on what they've told you), any \
practical trade-offs, and what makes it stand out. This should read as honest, \
specific expertise, not hype or invented superlatives. Never claim a feature, size, or \
amenity that wasn't in the tool result.

STEP 5 — Capture the lead. Once interest is clearly established (they've asked \
follow-up questions about a specific property, pricing, or visiting), naturally ask \
for their name and phone number so the team can follow up — frame it as "so someone \
can call you about this one" rather than a form to fill in. Only call capture_lead once \
you actually have BOTH a name and a phone number the user gave you in this \
conversation. Never invent or guess either field. After saving, confirm warmly and let \
them know someone will reach out.

You don't have to restart this flow from step 1 every message — pick up wherever the \
conversation actually is. If the user jumps straight to a specific ask (e.g. "show me \
houses in Hosur"), you can skip ahead to the matching step instead of forcing every \
step in order.

HOW YOU TALK:
- Warm, concise, conversational — like a knowledgeable local agent, not a corporate \
FAQ bot. Short paragraphs. No bullet-point walls unless listing multiple places/homes.
- CRITICAL FORMATTING RULE: your reply is shown as plain conversational text in a chat \
bubble — the UI renders property photo cards separately and automatically. NEVER write \
markdown links, e.g. never write something like [Property Name](listing.html?id=7) or \
any other [text](url) pattern, and never paste raw URLs into your reply. Just talk \
about the property by name in plain sentences; the card with its photo and link \
appears on its own beneath your message.
- When you show search results or a property's detail, the photos and structured card \
already appear in the chat UI automatically — don't describe the image or repeat every \
field back as a list. Instead, add the color a good agent would: what makes this one \
worth a look, what to ask about, what's nearby, what kind of buyer it suits. Real \
detail — square footage, room count, road access, whatever the tool actually gave you \
— said plainly, is what makes people trust you. Don't oversell; don't invent \
superlatives.
- If someone asks about a property you have real detail on, give a genuine, specific \
answer: what's distinctive about it, practical trade-offs, and a natural next step \
(seeing it in person, calling to ask more). This is what actually earns trust — \
specific, honest, useful answers, not hype.

CAPTURING INTEREST:
- Once a user clearly shows interest in a specific property (asks for more detail more \
than once, asks about visiting, pricing negotiation, or availability), naturally ask \
for their name and phone number so the team can follow up — frame it as "so someone \
can call you about this" rather than a form to fill in.
- Only call capture_lead once you actually have both a name and a phone number the \
user gave you in the conversation. Never invent or guess either field.
- Don't ask for contact info in the first message or before showing them anything real.

BOUNDARIES:
- You can't post new listings, edit listings, or process payments — for posting a \
property, tell them to use the "Post an Ad" page. For anything you can't help with, \
suggest calling +91 95003 91129.
- Keep replies focused — a few sentences, not an essay, unless walking through several \
listings."""


# ---------------------------------------------------------------------------
# Tool implementations — each one queries the real database.
# ---------------------------------------------------------------------------

def tool_list_locations(db: Session) -> dict:
    rows = (
        db.query(Property.location, func.count(Property.id))
        .filter(Property.status == "approved")
        .group_by(Property.location)
        .all()
    )
    # Merge older spellings (Bangalore -> Bengaluru) and keep our display order.
    merged: dict = {}
    for loc, count in rows:
        name = canonical_location(loc)
        merged[name] = merged.get(name, 0) + count
    ordered = sorted(merged.items(), key=lambda kv: location_sort_key(kv[0]))
    return {
        "locations": [{"location": loc, "count": count} for loc, count in ordered]
    }


def tool_search_properties(
    db: Session,
    location: Optional[str] = None,
    category: Optional[str] = None,
    type_: Optional[str] = None,
    keyword: Optional[str] = None,
    limit: int = 6,
) -> dict:
    query = db.query(Property).filter(Property.status == "approved")
    if location:
        # keep the old "contains" matching, but also try older spellings
        query = query.filter(or_(*[Property.location.ilike(f"%{v}%") for v in location_variants(location)]))
    if category:
        query = query.filter(Property.category.ilike(f"%{category}%"))
    if type_:
        # Map old/loose words (sale, rental, lease...) to the canonical type.
        query = query.filter(Property.type == (normalize_listing_type(type_) or type_))
    if keyword:
        like = f"%{keyword}%"
        query = query.filter(
            (Property.title.ilike(like)) | (Property.description.ilike(like))
        )

    # Order so the strongest/most complete listing comes first — gives the
    # model a natural "top pick" to lead with in STEP 3 of the flow, rather
    # than picking one arbitrarily.
    results = (
        query.order_by(Property.price.desc().nullslast(), Property.created_at.desc())
        .limit(limit)
        .all()
    )

    return {
        "count": len(results),
        "properties": [
            {
                "id": p.id,
                "title": p.title,
                "category": p.category,
                "type": p.type,
                "location": p.location,
                "price": p.price,
                "price_type": p.price_type,
                "price_unit": p.price_unit,
                "description": p.description,
                "photo_count": len(p.images),
            }
            for p in results
        ],
    }


def tool_get_property_details(db: Session, property_id: int) -> dict:
    prop = (
        db.query(Property)
        .filter(Property.id == property_id, Property.status == "approved")
        .first()
    )
    if not prop:
        return {"found": False}

    return {
        "found": True,
        "id": prop.id,
        "title": prop.title,
        "category": prop.category,
        "type": prop.type,
        "location": prop.location,
        "price": prop.price,
        "price_type": prop.price_type,
        "price_unit": prop.price_unit,
        "description": prop.description,
        "photo_count": len(prop.images),
    }


def tool_capture_lead(
    db: Session, name: str, phone: str,
    property_id: Optional[int] = None, note: Optional[str] = None,
) -> dict:
    lead = Lead(
        name=name.strip(),
        phone=phone.strip(),
        property_id=property_id,
        note=note,
        source="ai_agent",
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    return {"saved": True, "lead_id": lead.id}


# ---------------------------------------------------------------------------
# Tool schema definitions (Groq/OpenAI function-calling format)
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_locations",
            "description": (
                "Get the list of locations that currently have approved property "
                "listings, with how many listings each has. Call this as soon as the "
                "user has said what TYPE of property they want, so you can offer them "
                "real areas to choose from (step 2 of the conversation flow)."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_properties",
            "description": (
                "Search real property listings. Use this once a user has picked a "
                "location (and optionally a type/category), to find their best "
                "matches — results come back with the strongest/highest-value match "
                "first so you can lead with a top pick. Returns compact summaries, "
                "not full listings."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City/area name, e.g. 'Hosur'"},
                    "category": {"type": "string", "description": "e.g. 'Houses & Villas', 'Agriculture Lands', 'Commercial Land'"},
                    "type_": {"type": "string", "description": "Sell, Buy, Rental & Lease, Tenants, or JV/JD (joint venture / joint development)"},
                    "keyword": {"type": "string", "description": "Free-text search in title/description"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_property_details",
            "description": (
                "Get full detail on ONE specific property by its id — use this when "
                "the user shows real interest in a specific listing (asks for more "
                "detail, pricing, or visiting). Use the id from the most recent "
                "search_properties results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "property_id": {"type": "integer"},
                },
                "required": ["property_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "capture_lead",
            "description": (
                "Save the user's name and phone number after they've shown real "
                "interest and provided both. Only call this once you actually have "
                "both values from the user — never invent them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "phone": {"type": "string"},
                    "property_id": {"type": "integer", "description": "The listing they're interested in, if any"},
                    "note": {"type": "string", "description": "One-line summary of what they're interested in"},
                },
                "required": ["name", "phone"],
            },
        },
    },
]


def run_tool(db: Session, name: str, args: dict) -> dict:
    if name == "list_locations":
        return tool_list_locations(db)
    if name == "search_properties":
        return tool_search_properties(
            db,
            location=args.get("location"),
            category=args.get("category"),
            type_=args.get("type_"),
            keyword=args.get("keyword"),
        )
    if name == "get_property_details":
        return tool_get_property_details(db, property_id=args["property_id"])
    if name == "capture_lead":
        return tool_capture_lead(
            db,
            name=args["name"],
            phone=args["phone"],
            property_id=args.get("property_id"),
            note=args.get("note"),
        )
    return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Reply sanitation
# ---------------------------------------------------------------------------
# Even with an explicit instruction not to, chat models sometimes slip into
# markdown link syntax out of habit. The frontend renders property cards as
# their own UI element (photo + title + price), so a markdown link left in
# the text would show up as broken literal "[Title](url)" text instead of a
# clean sentence. Strip that pattern defensively as a backend safety net,
# keeping just the link's visible label.
_MARKDOWN_LINK_RE = re.compile(r"\[([^\[\]]+)\]\((?:[^()\s]+)\)")
_BARE_URL_RE = re.compile(r"https?://\S+|(?<!\]\()(?<!\])\b\w[\w./-]*\.html(?:\?[^\s)]*)?")


def sanitize_reply(text: str) -> str:
    if not text:
        return text
    # [Label](url) -> Label
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    # Drop any leftover bare URLs/paths the model may have pasted in.
    text = _BARE_URL_RE.sub("", text)
    # Collapse doubled-up whitespace left behind by the removals above.
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------

MAX_TOOL_ROUNDS = 4  # safety cap on tool-call back-and-forth per message


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, db: Session = Depends(get_db)):
    client = get_groq_client()

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for m in payload.history[-12:]:  # keep recent context only
        messages.append({"role": m.role, "content": m.content})
    messages.append({"role": "user", "content": payload.message})

    listings_to_show: List[Property] = []
    seen_ids = set()

    for _ in range(MAX_TOOL_ROUNDS):
        try:
            completion = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.4,
            )
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"AI agent request failed: {exc}")

        choice = completion.choices[0]
        msg = choice.message

        if not msg.tool_calls:
            # Model gave a final answer — done.
            reply_text = msg.content or "Sorry, I didn't quite catch that — could you rephrase?"
            return ChatResponse(
                reply=sanitize_reply(reply_text),
                listings=[PropertyOut.model_validate(p) for p in listings_to_show],
            )

        # Model wants to call one or more tools — run them and feed results back.
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            result = run_tool(db, tc.function.name, args)

            # Track properties surfaced by search/detail tools so the frontend
            # can render real cards (with real photos) instead of the model
            # describing them in text.
            if tc.function.name == "search_properties":
                ids = [p["id"] for p in result.get("properties", [])]
                for pid in ids:
                    if pid not in seen_ids:
                        prop = db.query(Property).filter(Property.id == pid).first()
                        if prop:
                            listings_to_show.append(prop)
                            seen_ids.add(pid)
            elif tc.function.name == "get_property_details" and result.get("found"):
                pid = result["id"]
                if pid not in seen_ids:
                    prop = db.query(Property).filter(Property.id == pid).first()
                    if prop:
                        listings_to_show.append(prop)
                        seen_ids.add(pid)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result),
            })

    # Hit the safety cap without a final answer — return whatever we have.
    return ChatResponse(
        reply="Let me know a bit more about what you're looking for and I'll find it for you.",
        listings=[PropertyOut.model_validate(p) for p in listings_to_show],
    )
