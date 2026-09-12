import math

import pytest

from geo import ERRAND_MINUTES, MAX_DISTANCE_M, SPEED_M_PER_MIN, eta_minutes, haversine


def test_haversine_zero_distance():
    assert haversine(22.2819, 114.1576, 22.2819, 114.1576) == pytest.approx(0.0, abs=1e-6)


def test_haversine_known_distance_central_to_tst():
    # Central Pier to Tsim Sha Tsui Star Ferry — roughly 1.6 km across the harbour.
    d = haversine(22.28720, 114.16110, 22.29380, 114.16880)
    assert 900 < d < 1200


def test_haversine_one_degree_latitude():
    # A degree of latitude is ~111.2 km anywhere on the globe.
    d = haversine(22.0, 114.0, 23.0, 114.0)
    assert d == pytest.approx(111195, rel=0.01)


def test_haversine_is_symmetric():
    a = haversine(22.2819, 114.1576, 22.3193, 114.1702)
    b = haversine(22.3193, 114.1702, 22.2819, 114.1576)
    assert a == pytest.approx(b)


@pytest.mark.parametrize(
    "mode,distance,expected",
    [
        ("walk", 750, 10),
        ("mtr", 750, 4),
        ("taxi", 750, 2),
        ("walk", 300, 4),
        ("mtr", 2000, 10),
        ("taxi", 3500, 10),
    ],
)
def test_eta_minutes_all_modes(mode, distance, expected):
    assert eta_minutes(distance, mode) == expected


def test_eta_minutes_never_below_one():
    for mode in SPEED_M_PER_MIN:
        assert eta_minutes(0, mode) == 1
        assert eta_minutes(5, mode) == 1


def test_eta_minutes_unknown_mode_falls_back_to_walk():
    assert eta_minutes(750, "hovercraft") == eta_minutes(750, "walk")


def test_faster_mode_is_never_slower():
    d = 800
    assert eta_minutes(d, "taxi") <= eta_minutes(d, "mtr") <= eta_minutes(d, "walk")


def test_constants():
    assert MAX_DISTANCE_M == 800
    assert ERRAND_MINUTES == 10
    assert SPEED_M_PER_MIN == {"walk": 75, "mtr": 200, "taxi": 350}
