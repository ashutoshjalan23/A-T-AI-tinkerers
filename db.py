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
"""


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
