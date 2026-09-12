# Detour

A Telegram bot that remembers small errands and reminds you only when you're
physically near the place **and** have enough free time before your next real
calendar event, scaled by how you travel.

An LLM handles the conversation. Distance, travel time, and the decision to send
a reminder are deterministic Python that no model can influence.

---

## How it works

Two moments matter: **when you tell it**, and **when you move**.

**When you tell it.** Your message goes to a bounded orchestration agent. The
agent talks back in natural language and may call tools to create an errand,
close one, schedule one, read your calendar, or check whether your location is
known. Creating an errand resolves a place to a latitude and longitude, looks up
opening hours, and writes one row to SQLite. Then the bot goes quiet.

**When you move.** Telegram streams your position while a Live Location session
is running. On every update, `core.on_location` measures the distance to each
open errand, converts it to minutes using your travel mode, reads how long until
your next calendar event, and applies one rule:

```python
eta        = distance / speed_for_your_mode      # walk 75, MTR 200, taxi 350 m/min
round_trip = eta * 2 + 10                        # 10 min for the errand itself
fire when  distance <= 800m  AND  round_trip <= minutes_until_next_event
```

Same distance, same free time, different travel mode → different answer. That is
the product.

`fired_at` is stamped inside the same call, so repeated location updates can't
double-notify, and the stamp survives a restart.

---

## Architecture

```
Telegram (polling)
      │
   bot.py ......................... Telegram I/O only, no decisions
      │
   core.py ........................ orchestration, no telegram imports
      ├── handle_conversation() → agents.OrchestrationAgent   (free text)
      └── on_location()        → the fire rule                (location updates)
                                       │
   agents.py                           │  never consulted here
      ├── OrchestrationAgent .... LLM, bounded 4-turn tool loop
      ├── CalendarAgent ......... LLM-readable calendar facts (Pydantic-validated)
      ├── MapAgent .............. deterministic, no model
      └── LiveLocationAgent ..... deterministic, no model
      │
   services.py .................... OpenRouter · Nominatim · Exa · ICS/Ambiguous
   db.py · geo.py ................. SQLite · haversine and ETA maths
```

**The agent can create, complete and schedule errands. It cannot fire one.**
Reminders are gated solely by `should_fire` in `core.py`, which the model never
calls and whose inputs it never supplies. A hallucinated *"you're 200m away"* is
structurally impossible rather than merely unlikely.

Tools return a uniform envelope so a failure is data rather than an exception:

```json
{"ok": true, "data": {...}, "error": null}
```

The loop is capped at four tool rounds, then forced to answer. Any failure
inside it degrades to a plain sentence.

---

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
copy .env.example .env          # cp on macOS/Linux
```

| key | where it comes from | required |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/newbot` | **yes** |
| `OPENROUTER_API_KEY` | openrouter.ai | **yes** — without it the bot can't converse |
| `EXA_API_KEY` | exa.ai | no — reminders lose the "Closes at" line |
| `AMBIGUOUS_API_KEY` | Ambiguous | no — calendar falls back to ICS |

A BotFather token is `<digits>:<35 characters>`. Copy the **whole** string;
double-clicking often grabs only one side of the colon.

### BotFather polish

`/setcommands`, then paste this — it makes the blue Menu button appear:

```
start - Set up travel mode and calendar
calendar - Connect an ICS calendar url
tasks - List open errands
sim - Simulate a location: /sim 22.2832 114.1589
```

---

## Run

```bash
python bot.py
```

Polling. No webhook, no tunnel. State lives in `detour.db` and survives restarts;
new columns are added in place on startup, so an existing database keeps working.

### Verify without a phone

```bash
python sim.py     # whole flow: task, location 200m away, fire, no double-fire
pytest            # 106 tests
```

`sim.py` serves a fixture calendar over local HTTP, so the ICS path is genuinely
exercised rather than mocked. It runs with no Telegram token and stubs the parser
when `OPENROUTER_API_KEY` is absent.

---

## Using it

1. `/start` → pick **Walking / MTR / Taxi**.
2. `/calendar <secret ICS url>` → it replies naming your next real event. That
   echo-back is how you know the connection worked.
3. **Share your location once.** Nothing location-aware works before this — see
   below.
4. Talk to it: *"pick up my jacket at Central Cleaners"*, *"i need to buy
   groceries today"*, *"done with the groceries"*.
5. Tap **Share Live Location** so it can watch proximity.
6. When you're close enough with time to spare, one reminder arrives with
   **Done** and **Directions**.

`/sim 22.2832 114.1589` injects a position through the identical code path —
demo insurance if venue GPS misbehaves.

### Named shops vs categories

| you say | what happens |
|---|---|
| *"at Central Cleaners"* | resolved directly to one pin |
| *"buy groceries"* | searches a **2km box around your last known position**, offers the nearest three as buttons, you tap one |

Category search is anchored to you deliberately. Unbounded, Nominatim will
happily return a match on the far side of the territory — an errand created in
Cyberport once pinned a grocery 19.5km away on Lantau, which could never fire.

The category word is an OpenStreetMap tag (`supermarket`, `pharmacy`, `laundry`).
`grocery` returns nothing in OSM, so it is remapped to `supermarket`.

### Location is mandatory for category errands

The bot cannot ask Telegram where you are. It only receives a position when you
send one, or while a Live Location session runs. Until then, *"buy groceries"*
replies asking you to share location first. **Telegram Desktop cannot send
location at all** — use the phone app.

---

## Data model

```sql
users       chat_id, travel_mode, ics_url, ambiguous_cal_id,
            last_lat, last_lng, last_seen_at,          -- where you were
            last_area, area_lat, area_lng              -- cached neighbourhood name

tasks       id, chat_id, title, place_name, place_address, lat, lng,
            hours, fired_at, done_at,
            scheduled_for, reminder_lat, reminder_lng  -- optional time gate

candidates  shop options awaiting your tap (survives a restart mid-choice)
messages    recent turns, so follow-ups have a referent
```

`fired_at` and `done_at` being nullable timestamps is what enforces fire-once
across restarts. Nothing is tracked in memory.

---

## Degradation

Every external call returns a fallback instead of raising.

| pull this | what happens |
|---|---|
| `EXA_API_KEY` | reminders fire without the "Closes at" line |
| `AMBIGUOUS_API_KEY` | calendar silently falls back to the ICS feed |
| both calendars | assumes you're free (1440 min); reminders still fire |
| the network, at a demo place | `DEMO_PLACES` in `config.py` answers instead of Nominatim |
| `OPENROUTER_API_KEY` | the bot says it needs a key; nothing crashes |
| the agent loop | falls back to a plain sentence |

Nonsense input, unfindable places, a location with no open errand, and Done
tapped twice all produce a friendly reply and zero tracebacks.

---

## Layout

| file | job |
|---|---|
| `config.py` | env, constants, `DEMO_PLACES` offline fallback |
| `db.py` | SQLite schema, migrations, queries |
| `geo.py` | haversine, `eta_minutes`, the tuning constants |
| `services.py` | OpenRouter, Nominatim (forward + reverse + bounded), Exa, calendar chain |
| `agents.py` | orchestration and calendar agents (LLM); map and location workers (deterministic) |
| `core.py` | orchestration and the fire rule — **no telegram imports** |
| `bot.py` | Telegram handlers only |
| `sim.py` | offline harness |
| `tests/` | 106 tests |

`core.py` never imports Telegram. That is what makes `sim.py` and the headless
tests possible, and it is the rule to protect when editing.

---

## Known state

Honest notes for whoever picks this up next.

- **`timezonefinder` is listed in `requirements.txt` but is not installed in the
  current `.venv`.** The code handles its absence (falls back to Hong Kong time,
  and there's a test for that), so everything passes — but a fresh
  `pip install -r requirements.txt` will install it and switch on real
  per-coordinate timezone lookup. Behaviour differs between the two setups.
- **Two entry points exist.** `core.handle_conversation` is what `bot.py` uses.
  `core.handle_message` and `core.create_task` are the earlier single-parse path,
  still exercised by the tests and `sim.py`, but no longer reachable from
  Telegram. `services.parse_task` and `PARSE_SYSTEM_PROMPT` belong to that older
  path — the live prompt is the one inside `OrchestrationAgent.respond`.
- **Dead code.** `services.discover_merchants`, `_retailer_names` and
  `RETAILER_SYSTEM_PROMPT` (Exa product→retailer lookup) are unreachable.
- **Named chains are not location-anchored.** Categories search near you, but a
  named shop still resolves territory-wide, so *"the Watsons"* can pin a branch
  3.2km away when one sits at 1.5km. The bounded search needs the bare shop name
  (`"Watsons"` finds three nearby; `"Watsons Central Hong Kong"` finds none).
- **`DEMO_PLACES` coordinates are placeholders.** "Central Cleaners" and "KeyPro
  Repair" are fictional shops from the spec's example copy. Replace with a real
  venue before a live demo.
- **The Ambiguous endpoint shape is unverified** — implemented as a single
  short-lived HTTP GET against a guessed URL, tolerant of the usual field
  spellings, and it fails closed to ICS (which is tested).
