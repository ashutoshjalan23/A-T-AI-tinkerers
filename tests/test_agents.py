"""The deterministic workers must stay callable without the Agent SDK installed."""
import agents


def test_map_agent_uses_the_existing_demo_place_data():
    place = agents.MapAgent().resolve("Central Cleaners Hong Kong")
    assert place["name"] == "Central Cleaners"


def test_live_location_agent_persists_the_update(fresh_db):
    import db

    db.create_user(123)
    agents.LiveLocationAgent().record(123, 22.2819, 114.1576)
    user = db.get_user(123)
    assert (user["last_lat"], user["last_lng"]) == (22.2819, 114.1576)
