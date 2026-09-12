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


def test_create_task_on_nonsense_returns_friendly_err(fresh_db, offline, monkeypatch):
    monkeypatch.setattr(offline, "parse_task", lambda text: None)
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
