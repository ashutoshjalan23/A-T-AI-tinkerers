"""SQLite schema and queries. State lives here so it survives a restart."""
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    chat_id          INTEGER PRIMARY KEY,
    travel_mode      TEXT    NOT NULL DEFAULT 'walk',   -- walk | mtr | taxi
    ics_url          TEXT,
    ambiguous_cal_id TEXT,
    created_at       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    title         TEXT    NOT NULL,
    place_name    TEXT    NOT NULL,
    place_address TEXT,
    lat           REAL    NOT NULL,
    lng           REAL    NOT NULL,
    hours         TEXT,
    fired_at      TEXT,
    done_at       TEXT,
    created_at    TEXT    NOT NULL
);

-- Shop options offered to a user, awaiting their tap. In SQLite rather than
-- memory so a restart mid-choice doesn't lose them.
CREATE TABLE IF NOT EXISTS candidates (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id       INTEGER NOT NULL,
    title         TEXT    NOT NULL,
    place_name    TEXT    NOT NULL,
    place_address TEXT,
    lat           REAL    NOT NULL,
    lng           REAL    NOT NULL,
    distance_m    INTEGER NOT NULL,
    created_at    TEXT    NOT NULL
);

-- Recent turns, so a follow-up like "no, the other one" has something to refer to.
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    role       TEXT    NOT NULL,   -- user | bot
    text       TEXT    NOT NULL,
    created_at TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id, id);
"""

# Columns added after the first release. CREATE TABLE IF NOT EXISTS won't add
# them to a database that already exists, so they go on separately.
MIGRATIONS = [
    ("users", "last_lat", "REAL"),
    ("users", "last_lng", "REAL"),
    ("users", "last_seen_at", "TEXT"),
    ("users", "last_area", "TEXT"),        # reverse-geocoded neighbourhood
    ("users", "area_lat", "REAL"),         # where last_area was resolved, to avoid
    ("users", "area_lng", "REAL"),         # re-geocoding every time they move a little
    ("tasks", "scheduled_for", "TEXT"),
    ("tasks", "reminder_lat", "REAL"),
    ("tasks", "reminder_lng", "REAL"),
]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(config.DB_PATH)  # read at call time so tests can redirect it
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def cursor():
    """Commit on success, and always close - sqlite3's own context manager does not."""
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db() -> None:
    with cursor() as conn:
        conn.executescript(SCHEMA)
        for table, column, coltype in MIGRATIONS:
            existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")


def get_user(chat_id: int) -> dict | None:
    with cursor() as conn:
        row = conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,)).fetchone()
    return dict(row) if row else None


def create_user(chat_id: int) -> dict:
    with cursor() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (chat_id, travel_mode, created_at) VALUES (?, 'walk', ?)",
            (chat_id, now_iso()),
        )
    return get_user(chat_id)


def update_user(chat_id: int, **fields) -> None:
    allowed = {"travel_mode", "ics_url", "ambiguous_cal_id"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{k} = ?" for k in updates)
    with cursor() as conn:
        conn.execute(
            f"UPDATE users SET {assignments} WHERE chat_id = ?",
            (*updates.values(), chat_id),
        )


def insert_task(
    chat_id: int,
    title: str,
    place_name: str,
    place_address: str | None,
    lat: float,
    lng: float,
    hours: str | None,
) -> dict:
    with cursor() as conn:
        cur = conn.execute(
            """INSERT INTO tasks
               (chat_id, title, place_name, place_address, lat, lng, hours, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (chat_id, title, place_name, place_address, lat, lng, hours, now_iso()),
        )
        task_id = cur.lastrowid
    return get_task(task_id)


def get_task(task_id: int) -> dict | None:
    with cursor() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    return dict(row) if row else None


def open_tasks(chat_id: int) -> list[dict]:
    """Tasks not yet done. A fired task stays open until the user taps Done."""
    with cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks WHERE chat_id = ? AND done_at IS NULL ORDER BY id",
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def unfired_tasks(chat_id: int) -> list[dict]:
    """Tasks eligible to fire — neither fired nor done."""
    with cursor() as conn:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE chat_id = ? AND done_at IS NULL AND fired_at IS NULL
               ORDER BY id""",
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_fired(task_id: int) -> None:
    with cursor() as conn:
        conn.execute(
            "UPDATE tasks SET fired_at = ? WHERE id = ? AND fired_at IS NULL",
            (now_iso(), task_id),
        )


def mark_done(task_id: int) -> None:
    with cursor() as conn:
        conn.execute(
            "UPDATE tasks SET done_at = ? WHERE id = ? AND done_at IS NULL",
            (now_iso(), task_id),
        )


def schedule_task(task_id: int, scheduled_for: str, lat: float | None, lng: float | None) -> None:
    with cursor() as conn:
        conn.execute(
            """UPDATE tasks SET scheduled_for = ?, reminder_lat = ?, reminder_lng = ?
               WHERE id = ? AND done_at IS NULL""",
            (scheduled_for, lat, lng, task_id),
        )


def set_last_location(chat_id: int, lat: float, lng: float) -> None:
    """Remember where the user was, so task creation can search around them."""
    with cursor() as conn:
        conn.execute(
            "UPDATE users SET last_lat = ?, last_lng = ?, last_seen_at = ? WHERE chat_id = ?",
            (lat, lng, now_iso(), chat_id),
        )


def save_candidates(chat_id: int, title: str, places: list[dict]) -> list[dict]:
    """Replace this user's pending shop options with a new set."""
    with cursor() as conn:
        conn.execute("DELETE FROM candidates WHERE chat_id = ?", (chat_id,))
        for place in places:
            conn.execute(
                """INSERT INTO candidates
                   (chat_id, title, place_name, place_address, lat, lng, distance_m, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, title, place["name"], place.get("address"), place["lat"],
                 place["lng"], round(place["distance_m"]), now_iso()),
            )
    return list_candidates(chat_id)


def list_candidates(chat_id: int) -> list[dict]:
    with cursor() as conn:
        rows = conn.execute(
            "SELECT * FROM candidates WHERE chat_id = ? ORDER BY distance_m",
            (chat_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_candidate(candidate_id: int) -> dict | None:
    with cursor() as conn:
        row = conn.execute(
            "SELECT * FROM candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
    return dict(row) if row else None


def clear_candidates(chat_id: int) -> None:
    with cursor() as conn:
        conn.execute("DELETE FROM candidates WHERE chat_id = ?", (chat_id,))


def set_area(chat_id: int, area: str, lat: float, lng: float) -> None:
    with cursor() as conn:
        conn.execute(
            "UPDATE users SET last_area = ?, area_lat = ?, area_lng = ? WHERE chat_id = ?",
            (area, lat, lng, chat_id),
        )


def add_message(chat_id: int, role: str, text: str) -> None:
    with cursor() as conn:
        conn.execute(
            "INSERT INTO messages (chat_id, role, text, created_at) VALUES (?, ?, ?, ?)",
            (chat_id, role, text[:500], now_iso()),
        )


def recent_messages(chat_id: int, limit: int = 6) -> list[dict]:
    """Last few turns, oldest first."""
    with cursor() as conn:
        rows = conn.execute(
            "SELECT role, text FROM messages WHERE chat_id = ? ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]
