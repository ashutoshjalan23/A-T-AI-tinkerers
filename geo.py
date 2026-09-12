"""Distance and travel-time maths. Deterministic — no model ever touches this."""
from math import asin, cos, radians, sin, sqrt

SPEED_M_PER_MIN = {"walk": 75, "mtr": 200, "taxi": 350}
MAX_DISTANCE_M = 800
ERRAND_MINUTES = 10  # time the errand itself takes

EARTH_RADIUS_M = 6371000.0


def haversine(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres."""
    p1, p2 = radians(lat1), radians(lat2)
    dp = p2 - p1
    dl = radians(lng2 - lng1)
    a = sin(dp / 2) ** 2 + cos(p1) * cos(p2) * sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * asin(sqrt(a))


def eta_minutes(distance_m: float, mode: str) -> int:
    """Travel time in whole minutes, never less than 1."""
    speed = SPEED_M_PER_MIN.get(mode, SPEED_M_PER_MIN["walk"])
    return max(1, round(distance_m / speed))
