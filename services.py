"""External integrations. Every function returns a safe fallback instead of raising."""
import json
import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

from config import (
    AMBIGUOUS_API_KEY,
    AMBIGUOUS_BASE_URL,
    DEMO_PLACES,
    EXA_API_KEY,
    LOCAL_TZ,
    NO_CALENDAR_MINUTES,
    NEARBY_RADIUS_M,
    NOMINATIM_URL,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
    USER_AGENT,
)
from geo import haversine

log = logging.getLogger("detour.services")
TZ = ZoneInfo(LOCAL_TZ)

PARSE_SYSTEM_PROMPT = """You extract errands from casual messages.

Return JSON only. No prose, no markdown fences, no explanation.

Schema:
{"title": "<short imperative errand, max 6 words>",
 "kind": "place" | "category",
 "place_query": "<see below>"}

Use "place" when the user names a specific shop. place_query is that shop plus
the city.

Use "category" when the user names only a product or an errand type, with no
shop. place_query must then be a single OpenStreetMap tag word for the kind of
shop that sells it - supermarket, pharmacy, chemist, bakery, laundry,
convenience, hardware, books, greengrocer. No city, no product name, no
adjectives. Prefer "supermarket" over "grocery"; OSM has almost nothing tagged
"grocery".

Examples:
"remind me to pick up my jacket at Central Cleaners when I am nearby"
-> {"title": "Pick up jacket", "kind": "place", "place_query": "Central Cleaners Hong Kong"}
"grab detergent from the Watsons in Central"
-> {"title": "Buy detergent", "kind": "place", "place_query": "Watsons Central Hong Kong"}
"i need to buy groceries today"
-> {"title": "Buy groceries", "kind": "category", "place_query": "supermarket"}
"pick up my prescription"
-> {"title": "Pick up prescription", "kind": "category", "place_query": "pharmacy"}
"""

# OSM tags almost nobody uses, mapped to the one that actually has data.
CATEGORY_FIXES = {
    "grocery": "supermarket",
    "grocery store": "supermarket",
    "groceries": "supermarket",
    "drugstore": "pharmacy",
    "dry cleaner": "laundry",
    "dry cleaning": "laundry",
}

RETAILER_SYSTEM_PROMPT = """You read search results and list shops that sell a product.

Return JSON only. No prose, no markdown fences.

Schema:
{"retailers": ["<shop or chain name>", ...]}

Rules:
- At most 3, most likely first.
- Real shop or chain names only. Never a category like "pharmacy" or
  "skincare store". Never a website, marketplace or delivery app.
- Prefer chains with physical branches in the given city.
- If the text names no real shop, return {"retailers": []}.
"""


# --- OpenRouter -------------------------------------------------------------

def parse_task(text: str) -> dict | None:
    """Natural language -> {"title", "place_query"}. None on any failure."""
    if not OPENROUTER_API_KEY:
        log.warning("parse_task: no OPENROUTER_API_KEY")
        return None
    try:
        from openai import OpenAI

        client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)
        resp = client.chat.completions.create(
            model=OPENROUTER_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": PARSE_SYSTEM_PROMPT},
                {"role": "user", "content": text},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        title = str(data.get("title", "")).strip()
        place_query = str(data.get("place_query", "")).strip()
        if not title or not place_query:
            return None
        kind = "category" if str(data.get("kind", "")).lower() == "category" else "place"
        if kind == "category":
            place_query = CATEGORY_FIXES.get(place_query.lower(), place_query)
        return {"title": title, "place_query": place_query, "kind": kind}
    except Exception as e:
        log.warning("parse_task failed: %s", e)
        return None


# --- Nominatim --------------------------------------------------------------

def resolve_place(query: str) -> dict | None:
    """Place name -> {"name", "address", "lat", "lng"}. None if unfindable."""
    demo = _demo_place(query)
    if demo:
        log.info("resolve_place: DEMO_PLACES hit for %r", query)
        return dict(demo)
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={
                "q": query,
                "format": "jsonv2",
                "limit": 1,
                # Bias to Hong Kong. countrycodes=hk is NOT safe here — HK entries
                # carry cn country codes and it filters every result away.
                "viewbox": "113.83,22.56,114.44,22.15",
            },
            headers={"User-Agent": USER_AGENT},  # mandatory or Nominatim blocks us
            timeout=8,
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            return None
        hit = results[0]
        display = hit.get("display_name", query)
        return {
            "name": hit.get("name") or display.split(",")[0],
            "address": display,
            "lat": float(hit["lat"]),
            "lng": float(hit["lon"]),
        }
    except Exception as e:
        log.warning("resolve_place failed for %r: %s", query, e)
        return None


def nearby_places(query: str, lat: float, lng: float, radius_m: int = NEARBY_RADIUS_M,
                  limit: int = 3) -> list[dict]:
    """Shops matching `query` within `radius_m` of a point, nearest first.

    Bounded to a box around the user. Unbounded, Nominatim happily returns a
    match on the far side of the territory, which is how a grocery run ends up
    pinned to Lantau.
    """
    deg = radius_m / 111_000  # rough metres-per-degree; fine at this scale
    viewbox = f"{lng - deg},{lat + deg},{lng + deg},{lat - deg}"
    try:
        resp = requests.get(
            NOMINATIM_URL,
            params={
                "q": query,
                "format": "jsonv2",
                "limit": 10,
                "viewbox": viewbox,
                "bounded": 1,  # the whole point: hard-restrict to the box
            },
            headers={"User-Agent": USER_AGENT},
            timeout=8,
        )
        resp.raise_for_status()
        hits = resp.json()
    except Exception as e:
        log.warning("nearby_places failed for %r: %s", query, e)
        return []

    places = []
    for hit in hits:
        try:
            hit_lat, hit_lng = float(hit["lat"]), float(hit["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        distance = haversine(lat, lng, hit_lat, hit_lng)
        if distance > radius_m:
            continue
        display = hit.get("display_name", query)
        places.append({
            "name": hit.get("name") or _unnamed_label(hit, display, query),
            "address": display,
            "lat": hit_lat,
            "lng": hit_lng,
            "distance_m": distance,
        })
    places.sort(key=lambda p: p["distance_m"])
    log.info("nearby_places(%r) within %dm: %d hits", query, radius_m, len(places))
    return places[:limit]


def _unnamed_label(hit: dict, display: str, query: str) -> str:
    """OSM has real shops with no name tag. "18-20" is a useless button, so
    label them by what they are and the street they're on."""
    kind = (hit.get("type") or query).replace("_", " ").title()
    parts = [p.strip() for p in display.split(",") if p.strip()]
    street = parts[1] if len(parts) > 1 else (parts[0] if parts else "")
    return f"{kind} on {street}" if street else kind


def _demo_place(query: str) -> dict | None:
    q = query.lower()
    for key, place in DEMO_PLACES.items():
        if key in q or all(word in q for word in key.split()):
            return place
    return None


# --- Exa --------------------------------------------------------------------

TIME_RANGE = re.compile(
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*(?:-|–|—|to|until)\s*"
    r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
    re.I,
)


def enrich_hours(name: str, address: str | None) -> str | None:
    """Closing time as HH:MM. Called at task creation only, never on location."""
    if not EXA_API_KEY:
        return None
    try:
        from exa_py import Exa

        query = f"{name} {address or ''} opening hours".strip()
        results = Exa(EXA_API_KEY).search_and_contents(
            query, num_results=2, text={"max_characters": 2000}
        )
        for item in getattr(results, "results", []):
            closing = _closing_time(getattr(item, "text", "") or "")
            if closing:
                return closing
        return None
    except Exception as e:
        log.warning("enrich_hours failed for %r: %s", name, e)
        return None


def _closing_time(text: str) -> str | None:
    for m in TIME_RANGE.finditer(text):
        hour = int(m.group(4))
        minute = int(m.group(5) or 0)
        meridiem = (m.group(6) or m.group(3) or "").lower()
        if meridiem == "pm" and hour < 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    return None


def discover_merchants(product: str, city: str = "Hong Kong") -> list[dict]:
    """Product -> up to 3 real, geocoded shops that sell it. [] on any failure.

    Exa finds pages about where to buy it, the LLM pulls shop names out of that
    prose, and Nominatim turns each name into a pin. The user picks; nothing here
    decides anything on their behalf.
    """
    names = _retailer_names(product, city)
    if not names:
        return []
    found = []
    seen = set()
    for name in names:
        place = resolve_place(f"{name} {city}")
        if not place:
            continue
        key = (round(place["lat"], 4), round(place["lng"], 4))
        if key in seen:
            continue
        seen.add(key)
        found.append(place)
    log.info("discover_merchants(%r): %s -> %d geocoded", product, names, len(found))
    return found[:3]


def _retailer_names(product: str, city: str) -> list[str]:
    if not EXA_API_KEY or not OPENROUTER_API_KEY:
        return []
    text = _search_text(f"where to buy {product} in {city} shops")
    if not text:
        return []
    try:
        from openai import OpenAI

        client = OpenAI(base_url=OPENROUTER_BASE_URL, api_key=OPENROUTER_API_KEY)
        resp = client.chat.completions.create(
            model=OPENROUTER_MODEL,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": RETAILER_SYSTEM_PROMPT},
                {"role": "user",
                 "content": f"Product: {product}\nCity: {city}\n\n{text[:6000]}"},
            ],
        )
        data = json.loads(resp.choices[0].message.content)
        names = [str(n).strip() for n in data.get("retailers", []) if str(n).strip()]
        return names[:3]
    except Exception as e:
        log.warning("_retailer_names failed for %r: %s", product, e)
        return []


def _search_text(query: str) -> str:
    try:
        from exa_py import Exa

        results = Exa(EXA_API_KEY).search_and_contents(
            query, num_results=3, text={"max_characters": 2500}
        )
        return "\n\n".join(
            getattr(r, "text", "") or "" for r in getattr(results, "results", [])
        )
    except Exception as e:
        log.warning("exa search failed for %r: %s", query, e)
        return ""


# --- Calendar: Ambiguous, then ICS ------------------------------------------

def free_minutes(user: dict) -> tuple[int, str]:
    """(minutes_until_next_event, source_name)."""
    minutes, source, _ = calendar_status(user)
    return minutes, source


def calendar_status(user: dict) -> tuple[int, str, str | None]:
    """(minutes_until_next_event, source_name, event_title).

    Two live sources tried in order - Ambiguous, then ICS. Whichever answers wins.
    """
    for reader, source in ((_ambiguous_next_event, "ambiguous"), (_ics_next_event, "ics")):
        event = reader(user)
        if event:
            minutes = _minutes_until(event["start"])
            log.info("calendar: %s answered - %r in %d min", source, event["title"], minutes)
            return minutes, source, event["title"]
    log.info("calendar: no source answered, assuming free")
    return NO_CALENDAR_MINUTES, "none", None


def _minutes_until(start: datetime) -> int:
    delta = start - datetime.now(TZ)
    return max(0, int(delta.total_seconds() // 60))


def _ambiguous_next_event(user: dict) -> dict | None:
    """Single short-lived HTTP call. No MCP session inside a polling bot."""
    cal_id = user.get("ambiguous_cal_id")
    if not AMBIGUOUS_API_KEY or not cal_id:
        return None
    try:
        now = datetime.now(TZ)
        resp = requests.get(
            f"{AMBIGUOUS_BASE_URL}/v1/calendars/{cal_id}/events",
            params={
                "start": now.isoformat(),
                "end": (now + timedelta(days=1)).isoformat(),
            },
            headers={
                "Authorization": f"Bearer {AMBIGUOUS_API_KEY}",
                "User-Agent": USER_AGENT,
            },
            timeout=8,
        )
        resp.raise_for_status()
        payload = resp.json()
        events = payload if isinstance(payload, list) else payload.get("events", [])
        return _earliest_future(_normalise(e) for e in events)
    except Exception as e:
        log.warning("ambiguous calendar failed: %s", e)
        return None


def _normalise(raw: dict) -> dict | None:
    """Ambiguous field names are not pinned down - accept the usual spellings."""
    start_raw = raw.get("start") or raw.get("start_time") or raw.get("dtstart")
    if isinstance(start_raw, dict):
        start_raw = start_raw.get("dateTime") or start_raw.get("date")
    if not start_raw:
        return None
    try:
        start = datetime.fromisoformat(str(start_raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if start.tzinfo is None:
        start = start.replace(tzinfo=TZ)
    title = raw.get("title") or raw.get("summary") or raw.get("name") or "your next event"
    return {"title": str(title), "start": start}


def _ics_next_event(user: dict) -> dict | None:
    ics_url = user.get("ics_url")
    if not ics_url:
        return None
    try:
        from icalendar import Calendar

        resp = requests.get(ics_url, headers={"User-Agent": USER_AGENT}, timeout=10)
        resp.raise_for_status()
        cal = Calendar.from_ical(resp.content)
        events = []
        for comp in cal.walk("VEVENT"):
            dtstart = comp.get("DTSTART")
            if dtstart is None:
                continue
            start = dtstart.dt
            if not isinstance(start, datetime):  # all-day event, skip
                continue
            if start.tzinfo is None:
                start = start.replace(tzinfo=TZ)
            events.append({"title": str(comp.get("SUMMARY", "your next event")), "start": start})
        return _earliest_future(events)
    except Exception as e:
        log.warning("ics calendar failed: %s", e)
        return None


def _earliest_future(events) -> dict | None:
    now = datetime.now(TZ)
    future = [e for e in events if e and e["start"] > now]
    return min(future, key=lambda e: e["start"]) if future else None
