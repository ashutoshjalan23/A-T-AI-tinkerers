import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import config  # noqa: E402


@pytest.fixture
def fresh_db(tmp_path, monkeypatch):
    """Point every db call at a throwaway file. Yields the path so restart is testable."""
    import db

    path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(path))
    db.init_db()
    return str(path)


@pytest.fixture
def offline(monkeypatch):
    """No network in tests. Each service returns a controllable stub."""
    import services

    monkeypatch.setattr(
        services, "parse_task",
        lambda text, context="": {"intent": "create", "title": "Pick up jacket",
                                  "place_query": "Central Cleaners Hong Kong",
                                  "kind": "place"},
    )
    monkeypatch.setattr(
        services, "resolve_place",
        lambda query: {
            "name": "Central Cleaners",
            "address": "12 Queen's Road Central",
            "lat": 22.28190,
            "lng": 114.15760,
        },
    )
    monkeypatch.setattr(services, "enrich_hours", lambda name, address: "19:00")
    monkeypatch.setattr(services, "reverse_place", lambda lat, lng: "Southern District, Hong Kong")
    monkeypatch.setattr(
        services, "calendar_status", lambda user: (42, "ics", "Team standup")
    )
    return services
