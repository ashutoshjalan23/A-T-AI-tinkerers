import pytest

import core
import db
from geo import haversine

CHAT = 424242
PLACE = (22.28190, 114.15760)   # Central Cleaners
NEAR = (22.28320, 114.15890)    # ~197m away
FAR = (22.31930, 114.17020)     # Mong Kok, ~4.4km away


def make_task(fired_at=None, done_at=None):
    return {"id": 1, "fired_at": fired_at, "done_at": done_at}


# --- should_fire: the product rule ------------------------------------------

def test_fires_when_close_and_free():
    fire, metrics = core.should_fire(make_task(), 200, "walk", 60)
    assert fire is True
    assert metrics == {"distance_m": 200, "eta_min": 3, "free_min": 60}


def test_does_not_fire_beyond_max_distance():
    fire, metrics = core.should_fire(make_task(), 801, "walk", 1440)
    assert fire is False and metrics == {}


def test_fires_exactly_at_max_distance():
    fire, _ = core.should_fire(make_task(), 800, "walk", 1440)
    assert fire is True


def test_does_not_fire_when_round_trip_exceeds_free_time():
    # 750m walking = 10 min each way + 10 min errand = 30 min round trip.
    assert core.should_fire(make_task(), 750, "walk", 29)[0] is False
    assert core.should_fire(make_task(), 750, "walk", 30)[0] is True


def test_travel_mode_changes_the_answer_at_the_same_distance():
    """The whole point: same distance, same free time, different verdict."""
    distance, free = 750, 20
    assert core.should_fire(make_task(), distance, "walk", free)[0] is False
    assert core.should_fire(make_task(), distance, "mtr", free)[0] is True
    assert core.should_fire(make_task(), distance, "taxi", free)[0] is True


def test_never_fires_an_already_fired_task():
    assert core.should_fire(make_task(fired_at="2026-01-01T00:00:00"), 100, "walk", 999)[0] is False


def test_never_fires_a_done_task():
    assert core.should_fire(make_task(done_at="2026-01-01T00:00:00"), 100, "walk", 999)[0] is False


def test_no_free_time_means_no_reminder():
    assert core.should_fire(make_task(), 100, "walk", 0)[0] is False


# --- user lifecycle ----------------------------------------------------------

def test_ensure_user_is_idempotent(fresh_db):
    first = core.ensure_user(CHAT)
    second = core.ensure_user(CHAT)
    assert first["chat_id"] == second["chat_id"] == CHAT
    assert first["travel_mode"] == "walk"


def test_set_travel_mode(fresh_db):
    core.ensure_user(CHAT)
    core.set_travel_mode(CHAT, "taxi")
    assert db.get_user(CHAT)["travel_mode"] == "taxi"


def test_set_travel_mode_rejects_junk(fresh_db):
    core.ensure_user(CHAT)
    core.set_travel_mode(CHAT, "teleport")
    assert db.get_user(CHAT)["travel_mode"] == "walk"


def test_set_calendar_routes_url_to_ics(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "calendar_status", lambda user: (30, "ics", "Lecture"))
    assert core.set_calendar(CHAT, "https://example.com/basic.ics") == "Lecture"
    user = db.get_user(CHAT)
    assert user["ics_url"] == "https://example.com/basic.ics"
    assert user["ambiguous_cal_id"] is None


def test_set_calendar_rewrites_webcal(fresh_db, offline):
    core.set_calendar(CHAT, "webcal://example.com/basic.ics")
    assert db.get_user(CHAT)["ics_url"] == "https://example.com/basic.ics"


def test_set_calendar_routes_plain_id_to_ambiguous(fresh_db, offline):
    core.set_calendar(CHAT, "cal_abc123")
    user = db.get_user(CHAT)
    assert user["ambiguous_cal_id"] == "cal_abc123"
    assert user["ics_url"] is None


def test_set_calendar_returns_none_when_source_is_empty(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "calendar_status", lambda user: (1440, "none", None))
    assert core.set_calendar(CHAT, "https://example.com/dead.ics") is None


# --- create_task -------------------------------------------------------------

def test_create_task_stores_the_place(fresh_db, offline):
    task = core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    assert not isinstance(task, core.Err)
    assert task["title"] == "Pick up jacket"
    assert task["place_name"] == "Central Cleaners"
    assert task["hours"] == "19:00"
    assert task["fired_at"] is None and task["done_at"] is None
    assert task["place_source"] == "OpenStreetMap via Nominatim"
    assert task["place_source_url"].startswith("https://www.openstreetmap.org/")
    assert task["hours_source"] == "Exa web search"


def test_create_task_on_nonsense_returns_friendly_err(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "parse_task", lambda text, context="": None)
    result = core.create_task(CHAT, "asdfgh")
    assert isinstance(result, core.Err)
    assert "couldn't" in result.message
    assert core.open_tasks(CHAT) == []


def test_create_task_on_unfindable_place_returns_friendly_err(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "resolve_place", lambda query: None)
    result = core.create_task(CHAT, "buy a thing at nowhere")
    assert isinstance(result, core.Err)
    assert "find" in result.message


def test_task_survives_exa_being_unavailable(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "enrich_hours", lambda name, address: None)
    task = core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    assert task["hours"] is None

    fires = core.on_location(CHAT, *NEAR)
    assert len(fires) == 1  # reminder still fires, just without the hours line
    assert fires[0]["task"]["hours"] is None


# --- on_location -------------------------------------------------------------

def test_on_location_with_no_tasks_is_quiet(fresh_db, offline):
    core.ensure_user(CHAT)
    assert core.on_location(CHAT, *NEAR) == []


def test_on_location_fires_when_near(fresh_db, offline):
    core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    fires = core.on_location(CHAT, *NEAR)
    assert len(fires) == 1
    fire = fires[0]
    assert fire["distance_m"] == round(haversine(*NEAR, *PLACE))
    assert fire["eta_min"] == 3
    assert fire["free_min"] == 42
    assert fire["event_title"] == "Team standup"
    assert fire["calendar_source"] == "ics"
    assert fire["mode"] == "walk"


def test_on_location_stays_quiet_when_far(fresh_db, offline):
    core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    assert core.on_location(CHAT, *FAR) == []
    assert db.get_task(1)["fired_at"] is None  # still eligible later


def test_repeated_location_updates_fire_once(fresh_db, offline):
    core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    assert len(core.on_location(CHAT, *NEAR)) == 1
    assert core.on_location(CHAT, *NEAR) == []
    assert core.on_location(CHAT, *NEAR) == []


def test_fired_at_is_stamped_in_the_same_call(fresh_db, offline):
    core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    fires = core.on_location(CHAT, *NEAR)
    assert fires[0]["task"]["fired_at"] is not None
    assert db.get_task(fires[0]["task"]["id"])["fired_at"] is not None


def test_no_calendar_still_fires(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "calendar_status", lambda user: (1440, "none", None))
    core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    fires = core.on_location(CHAT, *NEAR)
    assert len(fires) == 1
    assert fires[0]["event_title"] is None
    assert fires[0]["calendar_source"] == "none"


def test_busy_calendar_suppresses_the_reminder(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "calendar_status", lambda user: (5, "ics", "Standup"))
    core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    assert core.on_location(CHAT, *NEAR) == []  # 197m walk round trip is 16 min


def test_travel_mode_is_applied_from_the_user_row(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "calendar_status", lambda user: (14, "ics", "Standup"))
    core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    assert core.on_location(CHAT, *NEAR) == []  # walking: 3+3+10 = 16 > 14

    core.set_travel_mode(CHAT, "taxi")
    fires = core.on_location(CHAT, *NEAR)
    assert len(fires) == 1  # taxi: 1+1+10 = 12 <= 14
    assert fires[0]["mode"] == "taxi"


def test_multiple_tasks_each_judged_separately(fresh_db, offline, monkeypatch):
    core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    monkeypatch.setattr(
        offline, "resolve_place",
        lambda query: {"name": "KeyPro Repair", "address": "Mong Kok",
                       "lat": FAR[0], "lng": FAR[1]},
    )
    core.create_task(CHAT, "collect keyboard at KeyPro Repair")

    fires = core.on_location(CHAT, *NEAR)
    assert [f["task"]["place_name"] for f in fires] == ["Central Cleaners"]
    assert db.get_task(2)["fired_at"] is None


# --- completion --------------------------------------------------------------

def test_complete_task_closes_it(fresh_db, offline):
    task = core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    core.complete_task(task["id"])
    assert core.open_tasks(CHAT) == []
    assert db.get_task(task["id"])["done_at"] is not None


def test_completing_twice_is_harmless(fresh_db, offline):
    task = core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    core.complete_task(task["id"])
    first = db.get_task(task["id"])["done_at"]
    core.complete_task(task["id"])  # double-tap on Done
    assert db.get_task(task["id"])["done_at"] == first


def test_completing_a_missing_task_is_harmless(fresh_db, offline):
    core.complete_task(9999)


def test_done_task_never_fires_again(fresh_db, offline):
    task = core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    core.complete_task(task["id"])
    assert core.on_location(CHAT, *NEAR) == []


# --- persistence -------------------------------------------------------------

def test_task_survives_a_restart(fresh_db, offline):
    """Acceptance 4: kill mid-task, restart, the task is still open and still fires."""
    import importlib

    core.create_task(CHAT, "pick up my jacket at Central Cleaners")

    # Simulate the process dying and coming back: fresh module objects, same file.
    importlib.reload(db)
    reloaded = importlib.reload(core)

    assert len(reloaded.open_tasks(CHAT)) == 1
    fires = reloaded.on_location(CHAT, *NEAR)
    assert len(fires) == 1
    assert fires[0]["task"]["title"] == "Pick up jacket"


def test_user_settings_survive_a_restart(fresh_db, offline):
    import importlib

    core.ensure_user(CHAT)
    core.set_travel_mode(CHAT, "mtr")
    core.set_calendar(CHAT, "https://example.com/basic.ics")

    importlib.reload(db)
    user = db.get_user(CHAT)
    assert user["travel_mode"] == "mtr"
    assert user["ics_url"] == "https://example.com/basic.ics"


def test_fresh_chat_id_completes_the_whole_flow(fresh_db, offline):
    """Acceptance 7: a chat_id the bot has never seen, start to Done."""
    new_chat = 987654321
    core.ensure_user(new_chat)
    core.set_travel_mode(new_chat, "walk")
    core.set_calendar(new_chat, "https://example.com/basic.ics")

    task = core.create_task(new_chat, "pick up my jacket at Central Cleaners")
    assert not isinstance(task, core.Err)

    fires = core.on_location(new_chat, *NEAR)
    assert len(fires) == 1

    core.complete_task(fires[0]["task"]["id"])
    assert core.open_tasks(new_chat) == []


def test_users_do_not_see_each_others_tasks(fresh_db, offline):
    core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    other = 111222333
    core.ensure_user(other)
    assert core.open_tasks(other) == []
    assert core.on_location(other, *NEAR) == []


# --- near-you category errands ------------------------------------------------

CYBERPORT = (22.26060, 114.13010)


def category_parse(text, context=""):
    return {"intent": "create", "title": "Buy groceries",
            "place_query": "supermarket", "kind": "category"}


def fake_nearby(query, lat, lng, radius_m=2000, limit=3):
    return [
        {"name": "PARKnSHOP", "address": "Cyberport Arcade", "lat": 22.2610,
         "lng": 114.1310, "distance_m": 117.4},
        {"name": "Wellcome", "address": "Pokfulam", "lat": 22.2650,
         "lng": 114.1350, "distance_m": 640.2},
    ]


def test_category_without_a_known_location_asks_for_one(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "parse_task", category_parse)
    result = core.create_task(CHAT, "i need to buy groceries")
    assert isinstance(result, core.Err)
    assert "where you are" in result.message
    assert core.open_tasks(CHAT) == []


def test_category_offers_nearby_choices(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "parse_task", category_parse)
    monkeypatch.setattr(offline, "nearby_places", fake_nearby)
    core.ensure_user(CHAT)
    db.set_last_location(CHAT, *CYBERPORT)

    result = core.create_task(CHAT, "i need to buy groceries")
    assert isinstance(result, core.Choices)
    assert result.title == "Buy groceries"
    assert [o["place_name"] for o in result.options] == ["PARKnSHOP", "Wellcome"]
    assert result.options[0]["distance_m"] == 117
    assert core.open_tasks(CHAT) == []  # nothing saved until they pick


def test_category_searches_around_the_user_not_the_territory(fresh_db, offline, monkeypatch):
    """The Lantau bug: the search must be anchored to the user's position."""
    seen = {}

    def spy(query, lat, lng, radius_m=2000, limit=3):
        seen.update(query=query, lat=lat, lng=lng)
        return fake_nearby(query, lat, lng)

    monkeypatch.setattr(offline, "parse_task", category_parse)
    monkeypatch.setattr(offline, "nearby_places", spy)
    core.ensure_user(CHAT)
    db.set_last_location(CHAT, *CYBERPORT)
    core.create_task(CHAT, "buy groceries")

    assert (seen["lat"], seen["lng"]) == CYBERPORT
    assert seen["query"] == "supermarket"


def test_picking_a_candidate_creates_the_task(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "parse_task", category_parse)
    monkeypatch.setattr(offline, "nearby_places", fake_nearby)
    core.ensure_user(CHAT)
    db.set_last_location(CHAT, *CYBERPORT)
    choices = core.create_task(CHAT, "buy groceries")

    task = core.choose_candidate(CHAT, choices.options[0]["id"])
    assert not isinstance(task, core.Err)
    assert task["place_name"] == "PARKnSHOP"
    assert task["hours"] == "19:00"
    assert len(core.open_tasks(CHAT)) == 1
    assert db.list_candidates(CHAT) == []  # options cleared after the pick


def test_picking_twice_is_harmless(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "parse_task", category_parse)
    monkeypatch.setattr(offline, "nearby_places", fake_nearby)
    core.ensure_user(CHAT)
    db.set_last_location(CHAT, *CYBERPORT)
    choices = core.create_task(CHAT, "buy groceries")
    cid = choices.options[0]["id"]

    core.choose_candidate(CHAT, cid)
    second = core.choose_candidate(CHAT, cid)
    assert isinstance(second, core.Err)
    assert len(core.open_tasks(CHAT)) == 1


def test_cannot_pick_another_users_candidate(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "parse_task", category_parse)
    monkeypatch.setattr(offline, "nearby_places", fake_nearby)
    core.ensure_user(CHAT)
    db.set_last_location(CHAT, *CYBERPORT)
    choices = core.create_task(CHAT, "buy groceries")

    other = 888777
    core.ensure_user(other)
    assert isinstance(core.choose_candidate(other, choices.options[0]["id"]), core.Err)


def test_no_shops_nearby_is_a_friendly_error(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "parse_task", category_parse)
    monkeypatch.setattr(offline, "nearby_places", lambda *a, **k: [])
    core.ensure_user(CHAT)
    db.set_last_location(CHAT, *CYBERPORT)
    result = core.create_task(CHAT, "buy groceries")
    assert isinstance(result, core.Err)
    assert "2km" in result.message


def test_location_update_records_position(fresh_db, offline):
    core.ensure_user(CHAT)
    core.on_location(CHAT, *CYBERPORT)
    user = db.get_user(CHAT)
    assert (user["last_lat"], user["last_lng"]) == CYBERPORT
    assert user["last_seen_at"] is not None


def test_named_shop_still_resolves_directly(fresh_db, offline):
    """A named place must not go through the choice flow."""
    result = core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    assert not isinstance(result, (core.Err, core.Choices))
    assert result["place_name"] == "Central Cleaners"


def test_candidates_survive_a_restart(fresh_db, offline, monkeypatch):
    import importlib

    monkeypatch.setattr(offline, "parse_task", category_parse)
    monkeypatch.setattr(offline, "nearby_places", fake_nearby)
    core.ensure_user(CHAT)
    db.set_last_location(CHAT, *CYBERPORT)
    choices = core.create_task(CHAT, "buy groceries")
    cid = choices.options[0]["id"]

    importlib.reload(db)
    reloaded = importlib.reload(core)
    task = reloaded.choose_candidate(CHAT, cid)
    assert not isinstance(task, core.Err)
    assert task["place_name"] == "PARKnSHOP"


# --- conversation context -----------------------------------------------------

def test_context_is_empty_for_a_brand_new_user(fresh_db, offline):
    user = core.ensure_user(CHAT)
    assert core.build_context(user) == ""


def test_context_carries_area_open_tasks_and_recent_turns(fresh_db, offline):
    core.ensure_user(CHAT)
    core.on_location(CHAT, *CYBERPORT)          # learns the area
    core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    db.add_message(CHAT, "user", "buy groceries")
    db.add_message(CHAT, "bot", "which one?")

    context = core.build_context(db.get_user(CHAT))
    assert "NEAR: Southern District, Hong Kong" in context
    assert "Pick up jacket at Central Cleaners" in context
    assert "user: buy groceries" in context
    assert "bot: which one?" in context


def test_context_is_passed_to_the_parser(fresh_db, offline, monkeypatch):
    seen = {}

    def spy(text, context=""):
        seen["context"] = context
        return {"intent": "create", "title": "Pick up jacket", "kind": "place",
                "place_query": "Central Cleaners Hong Kong"}

    monkeypatch.setattr(offline, "parse_task", spy)
    core.ensure_user(CHAT)
    core.on_location(CHAT, *CYBERPORT)
    core.handle_message(CHAT, "pick up the jacket")
    assert "NEAR: Southern District, Hong Kong" in seen["context"]


def test_area_is_not_regeocoded_for_small_movements(fresh_db, offline, monkeypatch):
    calls = []
    monkeypatch.setattr(offline, "reverse_place",
                        lambda lat, lng: calls.append((lat, lng)) or "Southern District")
    core.ensure_user(CHAT)
    core.on_location(CHAT, *CYBERPORT)
    core.on_location(CHAT, CYBERPORT[0] + 0.001, CYBERPORT[1])   # ~110m
    assert len(calls) == 1  # Nominatim allows 1 req/sec; don't waste them


def test_area_is_regeocoded_after_a_real_move(fresh_db, offline, monkeypatch):
    calls = []
    monkeypatch.setattr(offline, "reverse_place",
                        lambda lat, lng: calls.append((lat, lng)) or "Somewhere")
    core.ensure_user(CHAT)
    core.on_location(CHAT, *CYBERPORT)
    core.on_location(CHAT, 22.3193, 114.1702)   # Mong Kok
    assert len(calls) == 2


# --- completing an errand by talking -------------------------------------------

def complete_parse(ref):
    return lambda text, context="": {"intent": "complete", "task_ref": ref}


def test_saying_done_closes_the_errand(fresh_db, offline, monkeypatch):
    task = core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    monkeypatch.setattr(offline, "parse_task", complete_parse("Pick up jacket"))

    result = core.handle_message(CHAT, "got the jacket")
    assert isinstance(result, core.Completed)
    assert result.task["id"] == task["id"]
    assert result.task["done_at"] is not None
    assert core.open_tasks(CHAT) == []


def test_completion_matches_on_the_place_too(fresh_db, offline, monkeypatch):
    core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    monkeypatch.setattr(offline, "parse_task", complete_parse("Central Cleaners"))
    assert isinstance(core.handle_message(CHAT, "done at the cleaners"), core.Completed)


def test_completion_with_no_match_is_a_friendly_error(fresh_db, offline, monkeypatch):
    core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    monkeypatch.setattr(offline, "parse_task", complete_parse("buy a boat"))
    result = core.handle_message(CHAT, "done with the boat")
    assert isinstance(result, core.Err)
    assert len(core.open_tasks(CHAT)) == 1  # nothing closed by mistake


def test_completion_only_touches_your_own_errands(fresh_db, offline, monkeypatch):
    core.create_task(CHAT, "pick up my jacket at Central Cleaners")
    other = 555444
    core.ensure_user(other)
    monkeypatch.setattr(offline, "parse_task", complete_parse("Pick up jacket"))

    assert isinstance(core.handle_message(other, "done"), core.Err)
    assert len(core.open_tasks(CHAT)) == 1


def test_handle_message_records_the_turn(fresh_db, offline):
    core.ensure_user(CHAT)
    core.handle_message(CHAT, "pick up my jacket at Central Cleaners")
    core.remember_reply(CHAT, "confirmed: Pick up jacket")
    recent = db.recent_messages(CHAT)
    assert recent[0] == {"role": "user", "text": "pick up my jacket at Central Cleaners"}
    assert recent[-1]["role"] == "bot"


def test_history_is_capped(fresh_db, offline):
    core.ensure_user(CHAT)
    for i in range(20):
        db.add_message(CHAT, "user", f"message {i}")
    assert len(db.recent_messages(CHAT)) == 6
    assert db.recent_messages(CHAT)[-1]["text"] == "message 19"  # newest last


def test_handle_message_on_nonsense_still_friendly(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "parse_task", lambda text, context="": None)
    result = core.handle_message(CHAT, "hello there")
    assert isinstance(result, core.Err)
    assert core.open_tasks(CHAT) == []
