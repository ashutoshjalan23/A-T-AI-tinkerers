"""Orchestration. Every decision lives here and runs without Telegram."""
import logging
from dataclasses import dataclass

import db
import services
from config import TRAVEL_MODES
from geo import ERRAND_MINUTES, MAX_DISTANCE_M, eta_minutes, haversine

log = logging.getLogger("detour.core")

db.init_db()


@dataclass
class Err:
    """A user-safe failure. Never carries a traceback or an API detail."""

    message: str


def should_fire(task: dict, distance_m: float, mode: str, free_min: int) -> tuple[bool, dict]:
    if task["fired_at"] or task["done_at"]:
        return False, {}
    eta = eta_minutes(distance_m, mode)
    round_trip = eta * 2 + ERRAND_MINUTES
    if distance_m > MAX_DISTANCE_M:
        return False, {}
    if round_trip > free_min:
        return False, {}
    return True, {"distance_m": round(distance_m), "eta_min": eta, "free_min": free_min}


def ensure_user(chat_id: int) -> dict:
    return db.get_user(chat_id) or db.create_user(chat_id)


def set_travel_mode(chat_id: int, mode: str) -> None:
    if mode not in TRAVEL_MODES:
        return
    ensure_user(chat_id)
    db.update_user(chat_id, travel_mode=mode)


def set_calendar(chat_id: int, value: str) -> str | None:
    """Store an ICS URL or an Ambiguous calendar id, then echo back the next event.

    Returns the event name so the user can see the connection actually worked.
    None if the source is unreachable or has nothing scheduled.
    """
    ensure_user(chat_id)
    value = value.strip()
    if value.lower().startswith("webcal://"):
        value = "https://" + value[len("webcal://") :]

    if value.lower().startswith(("http://", "https://")):
        db.update_user(chat_id, ics_url=value)
    else:
        db.update_user(chat_id, ambiguous_cal_id=value)

    _, source, title = services.calendar_status(db.get_user(chat_id))
    log.info("set_calendar chat=%s source=%s next=%r", chat_id, source, title)
    return title


def create_task(chat_id: int, text: str) -> dict | Err:
    """parse_task -> resolve_place -> enrich_hours -> insert."""
    ensure_user(chat_id)

    parsed = services.parse_task(text)
    if not parsed:
        return Err("I couldn't work out an errand from that. Try: "
                   "\"pick up my jacket at Central Cleaners\".")

    place = services.resolve_place(parsed["place_query"])
    if not place:
        return Err(f"I couldn't find \"{parsed['place_query']}\". "
                   "Try naming the place more precisely.")

    hours = services.enrich_hours(place["name"], place.get("address"))
    task = db.insert_task(
        chat_id=chat_id,
        title=parsed["title"],
        place_name=place["name"],
        place_address=place.get("address"),
        lat=place["lat"],
        lng=place["lng"],
        hours=hours,
    )
    log.info("created task %s for chat %s: %r @ %r", task["id"], chat_id, task["title"],
             task["place_name"])
    return task


def on_location(chat_id: int, lat: float, lng: float) -> list[dict]:
    """Check every open task against this position. Stamps fired_at as it goes."""
    user = ensure_user(chat_id)
    mode = user["travel_mode"]
    candidates = db.unfired_tasks(chat_id)
    if not candidates:
        return []

    # One calendar read per location update, not one per task.
    free_min, source, event_title = services.calendar_status(user)

    fires = []
    for task in candidates:
        distance = haversine(lat, lng, task["lat"], task["lng"])
        fire, metrics = should_fire(task, distance, mode, free_min)
        log.info(
            "task %s %s: %dm, mode=%s, free=%dmin -> %s",
            task["id"], task["title"], round(distance), mode, free_min,
            "FIRE" if fire else "hold",
        )
        if not fire:
            continue
        # Stamped in the same call, so repeated live-location updates can't double-fire.
        db.mark_fired(task["id"])
        fires.append({
            "task": db.get_task(task["id"]),
            "mode": mode,
            "event_title": event_title,
            "calendar_source": source,
            **metrics,
        })
    return fires


def complete_task(task_id: int) -> None:
    db.mark_done(task_id)


def open_tasks(chat_id: int) -> list[dict]:
    return db.open_tasks(chat_id)
