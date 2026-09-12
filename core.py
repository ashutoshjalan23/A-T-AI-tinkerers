"""Orchestration. Every decision lives here and runs without Telegram."""
import logging
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import db
import services
from config import NEARBY_RADIUS_M, TRAVEL_MODES
from geo import ERRAND_MINUTES, MAX_DISTANCE_M, eta_minutes, haversine

log = logging.getLogger("detour.core")

db.init_db()


@dataclass
class Err:
    """A user-safe failure. Never carries a traceback or an API detail."""

    message: str


@dataclass
class Choices:
    """Several shops matched. The user picks; we never pick for them."""

    title: str
    options: list[dict]


@dataclass
class Completed:
    """The user said an existing errand is finished."""

    task: dict


AREA_REFRESH_M = 500  # don't re-geocode the neighbourhood for small movements


def build_context(user: dict) -> str:
    """What the parser gets to know: roughly where they are, what's outstanding,
    and the last few turns."""
    chat_id = user["chat_id"]
    titles = [f"{t['title']} at {t['place_name']}" for t in db.open_tasks(chat_id)]
    return services.build_context(
        area=user.get("last_area"),
        open_titles=titles,
        recent=db.recent_messages(chat_id),
    )


def refresh_area(chat_id: int, lat: float, lng: float) -> None:
    """Keep a human-readable neighbourhood name for the user's position."""
    user = db.get_user(chat_id)
    if user and user.get("area_lat") is not None:
        moved = haversine(lat, lng, user["area_lat"], user["area_lng"])
        if moved < AREA_REFRESH_M and user.get("last_area"):
            return
    area = services.reverse_place(lat, lng)
    if area:
        db.set_area(chat_id, area, lat, lng)


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


def handle_message(chat_id: int, text: str) -> dict | Choices | Completed | Err:
    """One parse, then route by intent. This is what the bot calls for free text."""
    user = ensure_user(chat_id)
    context = build_context(user)
    db.add_message(chat_id, "user", text)

    parsed = services.parse_task(text, context)
    if not parsed:
        return Err("I couldn't work out an errand from that. Try: "
                   "\"pick up my jacket at Central Cleaners\".")

    if parsed.get("intent") == "complete":
        return _complete_by_reference(chat_id, parsed["task_ref"])

    return _create_from_parsed(user, parsed)


async def handle_conversation(chat_id: int, text: str):
    """Use the LLM orchestration agent for free-form Telegram conversation.

    The agent can call the scheduler, map, and live-location workers, but the
    fire rule remains entirely in :func:`on_location` below.
    """
    from agents import OrchestrationAgent

    user = ensure_user(chat_id)
    db.add_message(chat_id, "user", text)
    agent = OrchestrationAgent(_create_from_parsed, _complete_by_reference, schedule_task, build_context)
    reply = await agent.respond(user, text)
    if reply.text:
        db.add_message(chat_id, "bot", reply.text)
    return reply


def remember_reply(chat_id: int, text: str) -> None:
    """Record what we said, so the next turn can resolve "the other one"."""
    db.add_message(chat_id, "bot", text)


def _complete_by_reference(chat_id: int, task_ref: str) -> Completed | Err:
    """Close the open errand the user referred to in words."""
    needle = task_ref.lower()
    for task in db.open_tasks(chat_id):
        haystack = f"{task['title']} at {task['place_name']}".lower()
        if needle in haystack or task["title"].lower() in needle:
            db.mark_done(task["id"])
            log.info("completed task %s by reference %r", task["id"], task_ref)
            return Completed(task=db.get_task(task["id"]))
    return Err(f"I couldn't match \"{task_ref}\" to an open errand. Try /tasks.")


def create_task(chat_id: int, text: str) -> dict | Choices | Err:
    """parse_task -> find the place -> enrich_hours -> insert.

    A named shop resolves straight to a pin. A category ("groceries") searches
    around the user's last known position and comes back as Choices.
    """
    user = ensure_user(chat_id)

    parsed = services.parse_task(text, build_context(user))
    if not parsed or parsed.get("intent") == "complete":
        return Err("I couldn't work out an errand from that. Try: "
                   "\"pick up my jacket at Central Cleaners\".")

    return _create_from_parsed(user, parsed)


def _create_from_parsed(user: dict, parsed: dict) -> dict | Choices | Err:
    from agents import MapAgent

    maps = MapAgent()
    if parsed.get("kind") == "category":
        return _category_choices(user, parsed, maps)

    place = maps.resolve(parsed["place_query"])
    if not place:
        return Err(f"I couldn't find \"{parsed['place_query']}\". "
                   "Try naming the place more precisely.")

    return _insert(user["chat_id"], parsed["title"], place)


def _category_choices(user: dict, parsed: dict, maps=None) -> Choices | Err:
    """Offer nearby shops of the right kind, nearest first."""
    if user["last_lat"] is None or user["last_lng"] is None:
        return Err(
            f"I can find a {parsed['place_query']} near you, but I don't know "
            "where you are yet. Share your location and send that again."
        )

    if maps is None:
        from agents import MapAgent

        maps = MapAgent()
    found = maps.nearby(parsed["place_query"], user["last_lat"], user["last_lng"])
    if not found:
        return Err(
            f"I couldn't find a {parsed['place_query']} within "
            f"{NEARBY_RADIUS_M // 1000}km of you. Try naming the shop directly."
        )

    options = db.save_candidates(user["chat_id"], parsed["title"], found)
    log.info("offering %d candidates to chat %s for %r",
             len(options), user["chat_id"], parsed["title"])
    return Choices(title=parsed["title"], options=options)


def choose_candidate(chat_id: int, candidate_id: int) -> dict | Err:
    """Turn a tapped option into a real task."""
    candidate = db.get_candidate(candidate_id)
    if not candidate or candidate["chat_id"] != chat_id:
        return Err("That option has expired. Send the errand again.")
    place = {
        "name": candidate["place_name"],
        "address": candidate["place_address"],
        "lat": candidate["lat"],
        "lng": candidate["lng"],
    }
    task = _insert(chat_id, candidate["title"], place)
    db.clear_candidates(chat_id)
    return task


def _insert(chat_id: int, title: str, place: dict) -> dict:
    hours = services.enrich_hours(place["name"], place.get("address"))
    task = db.insert_task(
        chat_id=chat_id,
        title=title,
        place_name=place["name"],
        place_address=place.get("address"),
        lat=place["lat"],
        lng=place["lng"],
        hours=hours,
    )
    log.info("created task %s for chat %s: %r @ %r", task["id"], chat_id, task["title"],
             task["place_name"])
    return _with_place_metadata(task, place.get("source"))


def _with_place_metadata(task: dict, place_source: str | None = None) -> dict:
    """Attach honest, user-visible provenance for a resolved place."""
    task["timezone"] = services.timezone_label(task["lat"], task["lng"])
    task["place_source"] = place_source or "OpenStreetMap via Nominatim"
    task["place_source_url"] = (
        "https://www.openstreetmap.org/?mlat="
        f"{task['lat']}&mlon={task['lng']}#map=18/{task['lat']}/{task['lng']}"
    )
    if task["hours"]:
        task["hours_source"] = "Exa web search"
    return task


def on_location(chat_id: int, lat: float, lng: float) -> list[dict]:
    """Check every open task against this position. Stamps fired_at as it goes."""
    user = ensure_user(chat_id)
    # The deterministic live-location agent persists every Telegram update.
    from agents import LiveLocationAgent, MapAgent

    LiveLocationAgent().record(chat_id, lat, lng)
    refresh_area(chat_id, lat, lng)
    mode = user["travel_mode"]
    candidates = db.unfired_tasks(chat_id)
    if not candidates:
        return []

    # One calendar read per location update, not one per task.
    free_min, source, event_title = services.calendar_status(user)

    fires = []
    for task in candidates:
        if task.get("scheduled_for"):
            try:
                due = datetime.fromisoformat(task["scheduled_for"])
                due = due.replace(tzinfo=ZoneInfo("Asia/Hong_Kong")) if due.tzinfo is None else due
                if datetime.now(due.tzinfo) < due:
                    continue
            except ValueError:
                log.warning("task %s has invalid scheduled_for", task["id"])
        distance = MapAgent().distance_to(lat, lng, task)
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
        fired_task = _with_place_metadata(db.get_task(task["id"]))
        fires.append({
            "task": fired_task,
            "mode": mode,
            "event_title": event_title,
            "calendar_source": source,
            **metrics,
        })
    return fires


def complete_task(task_id: int) -> None:
    db.mark_done(task_id)


def schedule_task(chat_id: int, task_ref: str, scheduled_for: str) -> dict | Err:
    """Persist a user-requested schedule after validating the LLM-supplied ISO time."""
    try:
        when = datetime.fromisoformat(scheduled_for.replace("Z", "+00:00"))
    except ValueError:
        return Err("I need a specific time before I can schedule that errand.")
    when = when.replace(tzinfo=ZoneInfo("Asia/Hong_Kong")) if when.tzinfo is None else when
    needle = task_ref.lower()
    for task in db.open_tasks(chat_id):
        if needle in f"{task['title']} {task['place_name']}".lower():
            user = ensure_user(chat_id)
            db.schedule_task(task["id"], when.isoformat(), user.get("last_lat"), user.get("last_lng"))
            return db.get_task(task["id"])
    return Err("I couldn't match that to an open errand. Try /tasks.")


def open_tasks(chat_id: int) -> list[dict]:
    return db.open_tasks(chat_id)
