# Detour

A Telegram bot that remembers small errands and fires a reminder only when you're
physically near the place **and** have enough free time before your next real
calendar event, scaled by how you travel.

The LLM parses language. Distance, timing, and the decision to fire are
deterministic Python.

## Agent flow

Free-text messages go to a bounded OpenRouter orchestration agent using
`openai/gpt-4o-mini`. It can keep a normal conversation and call three focused
workers:

- **Scheduler agent** — an LLM that explains suitable time using verified
  calendar facts.
- **Map agent** — deterministic Nominatim lookup, nearby-place search, and
  distance calculations.
- **Live-location agent** — deterministic persistence of each Telegram live
  location update.

The scheduler and orchestrator are limited to the Agent SDK's tool loop. The
map and live-location workers never call a model. Crucially, neither LLM can
fire a reminder: the existing deterministic distance-and-round-trip rule in
`core.on_location` remains the only notification gate.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
copy .env.example .env          # cp on macOS/Linux
```

Fill in `.env`:

| key | where it comes from | required |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/newbot` | yes |
| `OPENROUTER_API_KEY` | openrouter.ai | yes — without it nothing parses |
| `EXA_API_KEY` | exa.ai | no — reminders just lose the "Closes at" line |
| `AMBIGUOUS_API_KEY` | Ambiguous | no — calendar falls back to ICS |

## Run

```bash
python bot.py
```

Polling, no webhook, no tunnel. `Ctrl-C` to stop; state is in `detour.db` and
survives a restart.

## Verify without a phone

```bash
python sim.py
```

Creates a user, serves a fixture calendar over local HTTP, creates a task from a
sentence, feeds coordinates 200m away, and prints whether it fired and why. Then
re-feeds the same coordinates to prove it doesn't fire twice.

```bash
pytest
```

## BotFather setup

`/newbot` for the token, then:

- `/setdescription` — Remembers your errands and reminds you only when you're
  near the place with time to spare.
- `/setabouttext` — Location- and calendar-aware errand reminders.
- `/setcommands` — paste this, it makes the blue Menu button appear:

```
start - Set up travel mode and calendar
calendar - Connect an ICS calendar url
tasks - List open errands
sim - Simulate a location: /sim 22.2832 114.1589
```

## Using it

1. `/start` → pick Walking / MTR / Taxi.
2. `/calendar <secret ICS url>` → the bot replies naming your next event, which
   is how you know the connection actually worked.
3. Send an errand in plain English: *"pick up my jacket at Central Cleaners"*.
4. Tap **Share Live Location** (live location arrives as edited messages; the bot
   handles both).
5. When you're within 800m and the round trip fits before your next event, one
   reminder fires with **Done** and **Directions**.

`/sim 22.2832 114.1589` injects a location through the identical code path — demo
insurance if venue GPS misbehaves.

## The fire rule

```
eta        = distance / speed_for_your_mode      (walk 75, MTR 200, taxi 350 m/min)
round_trip = eta * 2 + 10 min errand time
fire when  distance <= 800m  AND  round_trip <= minutes_until_next_event
```

Same distance, same free time, different travel mode → different answer. That's
the product.

## Layout

| file | job |
|---|---|
| `config.py` | env loading, constants, `DEMO_PLACES` offline fallback |
| `db.py` | SQLite schema and queries |
| `geo.py` | haversine, `eta_minutes` |
| `services.py` | OpenRouter parse, Nominatim geocode, Exa hours, calendar |
| `agents.py` | OpenRouter orchestrator and scheduler; deterministic map and location workers |
| `core.py` | orchestration — **no telegram imports**, callable headless |
| `bot.py` | Telegram handlers only |
| `sim.py` | offline harness |

`core.py` never imports Telegram, which is what makes `sim.py` and the tests
possible.

## Degradation

| pull this | what happens |
|---|---|
| `EXA_API_KEY` | reminders fire without the "Closes at" line |
| `AMBIGUOUS_API_KEY` | calendar silently falls back to the ICS feed |
| both calendars | assumes you're free (1440 min), reminders still fire |
| the network, at a demo place | `DEMO_PLACES` answers instead of Nominatim |
