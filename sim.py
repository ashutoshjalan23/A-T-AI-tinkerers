"""Offline harness: drive the whole flow with no phone and no Telegram token.

    python sim.py
"""
import functools
import http.server
import logging
import os
import socketserver
import sys
import tempfile
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8")
    except AttributeError:
        pass

logging.basicConfig(format="%(levelname)s %(name)s: %(message)s", level=logging.INFO)

import config  # noqa: E402

config.DB_PATH = os.getenv("DETOUR_DB", "sim.db")

import core  # noqa: E402  (imports db, which reads config.DB_PATH at call time)
import db  # noqa: E402
import services  # noqa: E402
from geo import haversine  # noqa: E402

TZ = ZoneInfo(config.LOCAL_TZ)
CHAT_ID = 999_000_001

# Central Cleaners, from DEMO_PLACES.
PLACE_LAT, PLACE_LNG = 22.28190, 114.15760
# ~200m north-east of it.
NEAR_LAT, NEAR_LNG = 22.28320, 114.15890
# Kowloon — far outside MAX_DISTANCE_M.
FAR_LAT, FAR_LNG = 22.31930, 114.17020


def fixture_ics(minutes_ahead: int = 42) -> str:
    """Serve a calendar over real HTTP so the ICS code path is genuinely exercised."""
    now = datetime.now(TZ)
    stamp = lambda d: d.strftime("%Y%m%dT%H%M%S")  # noqa: E731
    body = "\n".join([
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Detour//Sim//EN",
        "BEGIN:VEVENT",
        "UID:past@detour",
        f"DTSTART;TZID={config.LOCAL_TZ}:{stamp(now - timedelta(hours=2))}",
        "SUMMARY:Already finished",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:allday@detour",
        f"DTSTART;VALUE=DATE:{now.strftime('%Y%m%d')}",
        "SUMMARY:All-day placeholder",
        "END:VEVENT",
        "BEGIN:VEVENT",
        "UID:next@detour",
        f"DTSTART;TZID={config.LOCAL_TZ}:{stamp(now + timedelta(minutes=minutes_ahead))}",
        "SUMMARY:Team standup",
        "END:VEVENT",
        "END:VCALENDAR",
        "",
    ])
    folder = tempfile.mkdtemp(prefix="detour-sim-")
    with open(os.path.join(folder, "demo.ics"), "w", encoding="utf-8") as f:
        f.write(body)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=folder)
    handler.log_message = lambda *a, **k: None
    server = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/demo.ics"


def stub_parse_if_offline() -> None:
    """No OpenRouter key? Stub the parser so the rest of the flow still runs."""
    if config.OPENROUTER_API_KEY:
        return
    print("! No OPENROUTER_API_KEY — stubbing parse_task so the flow still runs.\n")
    services.parse_task = lambda text, context="": {
        "intent": "create",
        "title": "Pick up jacket",
        "place_query": "Central Cleaners Hong Kong",
        "kind": "place",
    }


def show(fires: list[dict]) -> None:
    if not fires:
        print("   -> nothing fired")
        return
    for fire in fires:
        task = fire["task"]
        print(f"   -> FIRED: {task['title']} at {task['place_name']}")
        print(f"      {fire['distance_m']}m, {fire['eta_min']} min by {fire['mode']}, "
              f"{fire['free_min']} min free before {fire['event_title']!r} "
              f"(calendar: {fire['calendar_source']})")
        print(f"      hours: {task['hours']}")


def main() -> int:
    if os.path.exists(config.DB_PATH):
        os.remove(config.DB_PATH)
    db.init_db()
    stub_parse_if_offline()

    print("1. ensure_user + travel mode")
    core.ensure_user(CHAT_ID)
    core.set_travel_mode(CHAT_ID, "walk")
    print(f"   mode = {db.get_user(CHAT_ID)['travel_mode']}")

    print("\n2. connect fixture calendar")
    next_event = core.set_calendar(CHAT_ID, fixture_ics())
    print(f"   next event = {next_event!r}")

    print("\n3. create task from a sentence")
    task = core.create_task(
        CHAT_ID, "remind me to pick up my jacket at Central Cleaners when I'm nearby"
    )
    if isinstance(task, core.Err):
        print(f"   FAILED: {task.message}")
        return 1
    print(f"   #{task['id']} {task['title']!r} at {task['place_name']!r} "
          f"({task['lat']:.5f}, {task['lng']:.5f}) hours={task['hours']}")

    print("\n4. location far away (Mong Kok)")
    show(core.on_location(CHAT_ID, FAR_LAT, FAR_LNG))

    distance = haversine(NEAR_LAT, NEAR_LNG, PLACE_LAT, PLACE_LNG)
    print(f"\n5. location {round(distance)}m away")
    fires = core.on_location(CHAT_ID, NEAR_LAT, NEAR_LNG)
    show(fires)
    if not fires:
        print("   FAILED: expected a reminder")
        return 1

    print("\n6. same coordinates again (fire-once check)")
    again = core.on_location(CHAT_ID, NEAR_LAT, NEAR_LNG)
    show(again)
    if again:
        print("   FAILED: fired twice")
        return 1

    print("\n7. tap Done")
    core.complete_task(fires[0]["task"]["id"])
    print(f"   open tasks = {len(core.open_tasks(CHAT_ID))}")

    print("\n8. nonsense input and an unfindable place")
    services.parse_task = lambda text, context="": None
    print(f"   nonsense -> {core.create_task(CHAT_ID, 'asdfgh').message}")
    services.parse_task = lambda text, context="": {
        "intent": "create", "title": "Buy thing",
        "place_query": "zzzqq nowhere 99999", "kind": "place"}
    print(f"   bad place -> {core.create_task(CHAT_ID, 'buy a thing at nowhere').message}")

    print("\nSIM PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
