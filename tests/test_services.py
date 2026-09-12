from datetime import datetime, timedelta

import pytest

import services
from services import TZ


def ics_bytes(events: list[tuple[str, str]]) -> bytes:
    body = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Detour//Test//EN"]
    for uid, (dtstart, summary) in enumerate(events):
        body += ["BEGIN:VEVENT", f"UID:{uid}@test", dtstart, f"SUMMARY:{summary}",
                 "END:VEVENT"]
    body.append("END:VCALENDAR")
    return "\n".join(body).encode()


def at(minutes: int) -> str:
    stamp = (datetime.now(TZ) + timedelta(minutes=minutes)).strftime("%Y%m%dT%H%M%S")
    return f"DTSTART;TZID=Asia/Hong_Kong:{stamp}"


class FakeResponse:
    def __init__(self, content=b"", payload=None):
        self.content = content
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


# --- Exa hours extraction ----------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("Open Mon-Fri 9:00 am - 7:00 pm", "19:00"),
        ("Hours: 10:30 to 21:00 daily", "21:00"),
        ("Opening hours 8am-8pm", "20:00"),
        ("Open 11 until 12 am", "00:00"),
        ("No hours published on this page", None),
        ("", None),
    ],
)
def test_closing_time(text, expected):
    assert services._closing_time(text) == expected


def test_enrich_hours_without_a_key_returns_none(monkeypatch):
    monkeypatch.setattr(services, "EXA_API_KEY", "")
    assert services.enrich_hours("Central Cleaners", "Queen's Road") is None


# --- Nominatim ---------------------------------------------------------------

def test_demo_places_short_circuit_the_network(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("network was called for a demo place")

    monkeypatch.setattr(services.requests, "get", explode)
    place = services.resolve_place("Central Cleaners Hong Kong")
    assert place["name"] == "Central Cleaners"


def test_resolve_place_returns_none_when_the_network_fails(monkeypatch):
    def explode(*a, **k):
        raise ConnectionError("down")

    monkeypatch.setattr(services.requests, "get", explode)
    assert services.resolve_place("somewhere unlisted 99999") is None


# --- Calendar chain ----------------------------------------------------------

def test_ics_picks_the_next_event_and_skips_all_day(monkeypatch):
    calendar = ics_bytes([
        (at(-120), "Already finished"),
        ("DTSTART;VALUE=DATE:20260912", "All-day placeholder"),
        (at(42), "Team standup"),
        (at(300), "Dinner"),
    ])
    monkeypatch.setattr(services.requests, "get", lambda *a, **k: FakeResponse(calendar))
    minutes, source, title = services.calendar_status({"ics_url": "https://x/c.ics"})
    assert source == "ics"
    assert title == "Team standup"
    assert 40 <= minutes <= 42


def test_ics_with_only_past_events_falls_through_to_none(monkeypatch):
    calendar = ics_bytes([(at(-30), "Finished")])
    monkeypatch.setattr(services.requests, "get", lambda *a, **k: FakeResponse(calendar))
    assert services.calendar_status({"ics_url": "https://x/c.ics"}) == (1440, "none", None)


def test_ambiguous_wins_when_both_are_configured(monkeypatch):
    monkeypatch.setattr(services, "AMBIGUOUS_API_KEY", "key")
    payload = {"events": [{
        "title": "Client call",
        "start": (datetime.now(TZ) + timedelta(minutes=25)).isoformat(),
    }]}

    def fake_get(url, **kwargs):
        if "calendars" in url:
            return FakeResponse(payload=payload)
        return FakeResponse(ics_bytes([(at(90), "Should not be used")]))

    monkeypatch.setattr(services.requests, "get", fake_get)
    minutes, source, title = services.calendar_status(
        {"ambiguous_cal_id": "cal_1", "ics_url": "https://x/c.ics"}
    )
    assert (source, title) == ("ambiguous", "Client call")
    assert 23 <= minutes <= 25


def test_ambiguous_failure_falls_back_to_ics(monkeypatch):
    """Acceptance 6b: pull the Ambiguous key and the calendar keeps working."""
    monkeypatch.setattr(services, "AMBIGUOUS_API_KEY", "key")

    def fake_get(url, **kwargs):
        if "calendars" in url:
            raise ConnectionError("ambiguous down")
        return FakeResponse(ics_bytes([(at(60), "Lecture")]))

    monkeypatch.setattr(services.requests, "get", fake_get)
    minutes, source, title = services.calendar_status(
        {"ambiguous_cal_id": "cal_1", "ics_url": "https://x/c.ics"}
    )
    assert (source, title) == ("ics", "Lecture")


def test_no_ambiguous_key_skips_straight_to_ics(monkeypatch):
    monkeypatch.setattr(services, "AMBIGUOUS_API_KEY", "")
    monkeypatch.setattr(
        services.requests, "get", lambda *a, **k: FakeResponse(ics_bytes([(at(60), "Lecture")]))
    )
    _, source, _ = services.calendar_status({"ambiguous_cal_id": "cal_1",
                                             "ics_url": "https://x/c.ics"})
    assert source == "ics"


def test_no_sources_configured(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("no network should be attempted")

    monkeypatch.setattr(services.requests, "get", explode)
    assert services.calendar_status({}) == (1440, "none", None)


def test_free_minutes_matches_calendar_status(monkeypatch):
    monkeypatch.setattr(services, "calendar_status", lambda user: (42, "ics", "Standup"))
    assert services.free_minutes({}) == (42, "ics")


def test_a_broken_calendar_never_raises(monkeypatch):
    monkeypatch.setattr(services.requests, "get",
                        lambda *a, **k: FakeResponse(b"this is not a calendar"))
    assert services.calendar_status({"ics_url": "https://x/c.ics"}) == (1440, "none", None)


# --- OpenRouter --------------------------------------------------------------

def test_parse_task_without_a_key_returns_none(monkeypatch):
    monkeypatch.setattr(services, "OPENROUTER_API_KEY", "")
    assert services.parse_task("pick up my jacket") is None
