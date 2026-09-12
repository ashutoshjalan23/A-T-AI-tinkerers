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
    NOMINATIM_URL,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    OPENROUTER_MODEL,
    USER_AGENT,
)

log = logging.getLogger("detour.services")
TZ = ZoneInfo(LOCAL_TZ)

PARSE_SYSTEM_PROMPT = """You extract errands from casual messages.

Return JSON only. No prose, no markdown fences, no explanation.

Schema:
{"title": "<short imperative errand, max 6 words>",
 "place_query": "<searchable place name, add 'Hong Kong' if no city is given>"}

Examples:
"remind me to pick up my jacket at Central Cleaners when I am nearby"
-> {"title": "Pick up jacket", "place_query": "Central Cleaners Hong Kong"}
"grab detergent from the Watsons in Central"
-> {"title": "Buy detergent", "place_query": "Watsons Central Hong Kong"}
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
        return {"title": title, "place_query": place_query}
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
