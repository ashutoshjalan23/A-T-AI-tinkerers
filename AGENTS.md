# Detour — build spec

A Telegram bot that remembers small errands and fires a reminder only when the
user is physically near the place **and** has enough free time before their next
real calendar event, scaled by how they travel.

Built in a 3-hour hackathon. Judged on whether it works **end to end** on a fresh
account, not on feature count.

## Non-negotiable constraints

- **Python 3.11+.** Polling, not webhooks. Never introduce ngrok or a tunnel.
- **SQLite.** State must survive a process restart. An in-memory dict fails the build.
- **`bot.py` contains only Telegram I/O.** All decisions live in `core.py` and are
  callable without Telegram running. This is the most important rule in this file —
  it is what makes the app testable headless.
- **The LLM parses language and nothing else.** Distance, timing, and the decision
  to fire are deterministic Python. Never ask a model whether the user is nearby.
- No web UI. No dashboard. No Docker. No async task queue. No abstractions for
  future providers — one implementation per job.
- Secrets from `.env` via `python-dotenv`. Never hardcode, never commit.

## Layout

```
config.py        env loading, constants
db.py            sqlite schema + queries
geo.py           haversine, eta_minutes
services.py      OpenRouter parse, Nominatim geocode, Exa enrich, ICS calendar
core.py          orchestration — NO telegram imports
bot.py           telegram handlers only
sim.py           offline harness: drive the whole flow with no phone
tests/test_core.py
```

## Environment

```
TELEGRAM_BOT_TOKEN=
OPENROUTER_API_KEY=
EXA_API_KEY=
AMBIGUOUS_API_KEY=      # optional — calendar falls back to ICS when absent
```

Write `.env.example` with these keys blank. Add `.env` to `.gitignore`.

## Data model

```sql
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
    hours         TEXT,                            -- from Exa, nullable
    fired_at      TEXT,
    done_at       TEXT,
    created_at    TEXT    NOT NULL
);
```

`fired_at` and `done_at` being nullable timestamps is what enforces fire-once and
survives restart. Do not track this in memory.

## geo.py

```python
SPEED_M_PER_MIN = {"walk": 75, "mtr": 200, "taxi": 350}
MAX_DISTANCE_M  = 800
ERRAND_MINUTES  = 10      # time the errand itself takes

def haversine(lat1, lng1, lat2, lng2) -> float   # metres
def eta_minutes(distance_m, mode) -> int         # min 1
```

## The fire rule — implement exactly

```python
def should_fire(task, distance_m, mode, free_min):
    if task["fired_at"] or task["done_at"]:
        return False, {}
    eta = eta_minutes(distance_m, mode)
    round_trip = eta * 2 + ERRAND_MINUTES
    if distance_m > MAX_DISTANCE_M:
        return False, {}
    if round_trip > free_min:
        return False, {}
    return True, {"distance_m": round(distance_m), "eta_min": eta,
                  "free_min": free_min}
```

`round_trip > free_min` is the product. Do not simplify it to a radius check.

## services.py

### parse_task(text) -> dict | None

One OpenRouter call. OpenAI-compatible client:

```python
client = OpenAI(base_url="https://openrouter.ai/api/v1",
                api_key=OPENROUTER_API_KEY)
```

Model `openai/gpt-4o-mini`, `temperature=0`,
`response_format={"type": "json_object"}`. Return
`{"title": str, "place_query": str}` or `None` on any failure — catch broadly and
return None rather than raising. The caller handles the user-facing error.

System prompt must instruct: output JSON only, no prose, no markdown fences.

### resolve_place(query) -> dict | None

Nominatim (`https://nominatim.openstreetmap.org/search`, `format=jsonv2`,
`limit=1`). **A `User-Agent` header is mandatory** or they will block you. Bias
results to Hong Kong. Return `{name, address, lat, lng}` or None.

Include a `DEMO_PLACES` dict in `config.py` checked before the network call, so a
flaky connection can't break the demo.

### enrich_hours(name, address) -> str | None

Exa search for opening hours. **Called at task creation only, never in the
location path.** Must be wrapped so any failure returns None silently — a reminder
must still fire with `hours = None`.

### free_minutes(user) -> tuple[int, str]

Returns `(minutes_until_next_event, source_name)`.

**Two sources, tried in order.** This is the one place a fallback is justified in
this codebase — it is live redundancy on the riskiest external dependency, not
speculative abstraction. Do not generalise it into a provider interface.

1. **Ambiguous Calendar** — if `AMBIGUOUS_API_KEY` is set and the user has an
   `ambiguous_cal_id`. Source name `"ambiguous"`.
2. **ICS feed** — if the user has an `ics_url`. Source name `"ics"`.
3. Neither available or both failed → `(1440, "none")`.

Each source wrapped so any failure falls through to the next. **Log which source
answered** — you will want to know that mid-demo.

ICS implementation: fetch the URL, parse with `icalendar`, find the next `VEVENT`
starting after now. Skip all-day events (`DTSTART` that is a `date`, not a
`datetime`). Assume `Asia/Hong_Kong` for naive datetimes. Ignore RRULE recurrence
— out of scope.

Ambiguous implementation: prefer a plain HTTP endpoint if one exists. Only reach
for the `mcp` Python SDK if there is no REST path — an MCP client session is a
long-lived subprocess or SSE connection inside a polling bot, which is a real
runtime liability for a three-hour build. If it needs more than a single
short-lived call per check, stop and use ICS.

## core.py — no telegram imports

```python
def ensure_user(chat_id) -> dict
def set_travel_mode(chat_id, mode) -> None
def set_calendar(chat_id, value) -> str | None
    """Accepts an ICS URL or an Ambiguous calendar id; stores in the right column.
    Returns the name of the user's next event, for echo-back confirmation.
    None if the source is unreachable or empty."""

def create_task(chat_id, text) -> dict | Err
    """parse_task -> resolve_place -> enrich_hours -> insert. Returns the task
    row, or Err with a user-safe message ('I couldn't find that place')."""

def on_location(chat_id, lat, lng) -> list[dict]
    """For each open task: distance, free_minutes, should_fire. Stamp fired_at
    for each that fires. Returns fire payloads (task + the metrics dict)."""

def complete_task(task_id) -> None
def open_tasks(chat_id) -> list[dict]
```

`on_location` stamping `fired_at` inside the same call is what guarantees
fire-once across repeated live-location updates.

## bot.py

python-telegram-bot v21, `run_polling()`.

Handlers:

- `/start` — `ensure_user`, then inline keyboard for travel mode
  (Walking / MTR / Taxi), then prompt for the calendar URL
- `/calendar <url>` — `set_calendar`, reply naming their next real event
  (**required** — the echo-back is what proves the connection worked)
- `/tasks` — list open errands
- Callback `mode:<walk|mtr|taxi>` — `set_travel_mode`
- Callback `done:<task_id>` — `complete_task`, edit the message to show it closed
- Free text — `create_task`, reply with the confirmation card and a
  "Share Live Location" prompt
- Location — **must catch both new and edited messages**:

```python
loc_filter = filters.LOCATION & (
    filters.UpdateType.MESSAGE | filters.UpdateType.EDITED_MESSAGE
)
```

Live location arrives as *edited* messages. Read via `update.effective_message`.

- `/sim <lat> <lng>` — inject a location update through the same `on_location`
  path. Demo insurance for venue GPS failure.

Send `ChatAction.TYPING` before any LLM call.

Reminder message format:

```
You're 180m from Central Cleaners — about 3 min walk.
You have 42 minutes before "Team standup".
Closes at 19:00.            <- omit this line when hours is None

[Done]  [Directions]
```

Directions is a URL button to
`https://www.google.com/maps/dir/?api=1&destination=<lat>,<lng>`.

## sim.py

Runs the entire flow with no phone and no Telegram token:

```
python sim.py
```

Creates a user, sets mode and a fixture ICS, creates a task from a sentence,
feeds coordinates 200m away, prints whether it fired and why. This is how you
verify work without waiting on a human tester.

## Acceptance — all must pass before the build is done

1. `pytest` green.
2. `python sim.py` fires a reminder and prints the metrics.
3. Second `on_location` call with the same coords does **not** fire again.
4. Kill the process mid-task, restart, task is still open and still fires.
5. Nonsense input, unfindable place, location with no open task, and `done` tapped
   twice all produce a friendly reply and **zero tracebacks**.
6. Exa key removed from `.env` → reminders still fire, just without the hours line.
6b. `AMBIGUOUS_API_KEY` removed from `.env` → calendar silently falls back to ICS
   and reminders still fire.
7. A chat_id never seen before completes the whole flow from `/start`.

## Style

Small functions, early returns, type hints, no class hierarchies. Comment only
where the reason isn't obvious. Match the existing file's style when editing.
