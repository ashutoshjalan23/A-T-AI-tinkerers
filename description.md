# Detour

*A chat-based errand agent that remembers the small tasks your calendar never captures — and nudges you only when you're actually in the right place, with enough time, and it actually fits how you get around.*

Built for the AI Tinkerers agentic AI hackathon (Hong Kong).

> "Detour" is a working name — swap it if the team prefers something else.

---

## The problem

Calendars are built for appointments. They are terrible at errands.

Picking up a repaired laptop, returning a parcel, grabbing detergent, collecting a library book before leaving campus — these tasks have no fixed time. They have a *place*, a rough deadline, and a requirement that you happen to be free. So people either don't record them at all, or bury them in a reminder app that fires at 9am on a Tuesday when they're nowhere near the shop.

The result is a familiar failure: you remember the errand the moment it becomes impossible to do.

## What we're building

A Telegram bot you talk to in plain language. You tell it about an errand once. It works out the place, the time window, and the conditions under which the errand is actually doable — then it stays quiet until those conditions are met.

```
You:  Remind me to pick up my jacket at Central Cleaners when I'm nearby after work.

Bot:  Found Central Cleaners, 12 Queen's Road Central. Remind you after your
      "Work" event once you're within 300m?
      [Confirm]  [Change place]  [Change time]

...later...

Bot:  You're 180m from Central Cleaners and you have 42 minutes before your
      next event. It's a 3 min walk. Pick up your jacket now?
      [Done]  [Snooze 20m]  [Directions]  [Not today]
```

The differentiator is not the language model. It's **reliable context-aware follow-through**: task × place × free time × personal constraints.

## What makes it ours: the user profile layer

Most proximity reminders treat every user identically — 300 metres is 300 metres. Ours doesn't. Each user has a lightweight profile that changes what "nearby" and "doable" even mean.

**Preferred mode of travel.** "Nearby" is not a radius, it's a travel-time budget.

- Walker in Hong Kong: 300m, but only if it isn't a 12-minute detour around a podium block.
- MTR user: a shop two stations away can be "nearby" if there's a 40-minute gap; a shop 200m away across a flyover might not be.
- Cyclist / driver: different radius again, plus parking is a real cost the agent should mention.

The agent converts geofence distance into *minutes by your mode*, then checks that against your free time before firing. A 400m alert that means a 20-minute round trip when you have 25 minutes free is a bad reminder.

**Budget.** Errands with a purchase attached get filtered and enriched against what the user is willing to spend.

- "Buy a USB-C cable" → the agent suggests a nearby retailer in the user's price band, not the first premium store it finds.
- Discretionary errands can be deprioritised or held when the user has flagged a tight week.
- Reminders can carry the useful number: *"Watson's has it for ~$59, the shop across the road is ~$120."*

**Other profile signals** we can use if time allows: quiet hours, home/work/campus anchors, whether the user is willing to carry bulky items (don't remind me to buy a 5kg bag of rice when I'm on my way to class), and per-category radius preferences.

This is what turns a geofenced reminder into something that feels like it knows you.

## How it works

```
Telegram
   ↓ webhook
Agent orchestration layer
   ├── LLM task parser      (natural language → structured task)
   ├── Task store            (lifecycle, status, cooldowns)
   ├── User profile          (travel mode, budget band, quiet hours, anchors)
   ├── Calendar / availability
   ├── Place resolver + geocoder
   ├── Travel-time estimator (mode-aware)
   ├── Location event handler (geofence + dwell check)
   ├── Exa enrichment        (hours, merchants, prices, alternatives)
   └── Notification + action loop
```

**The LLM handles language. The system handles decisions.** The model extracts structure from messy input; it never decides whether you're nearby, whether you're free, or whether to fire an alert. That's deterministic code. This keeps the agent predictable and eliminates hallucinated "you're near X" events — which would be fatal in a live demo.

Example extraction:

```json
{
  "task": "Pick up repaired phone",
  "place_query": "phone repair shop in Mong Kok",
  "time_constraint": { "type": "after_calendar_event", "reference": "lecture tomorrow" },
  "trigger": { "type": "near_location", "travel_budget_min": 8, "mode": "walking" },
  "purchase": null,
  "notification": { "channel": "telegram_message", "max_reminders": 1 },
  "status": "needs_place_confirmation"
}
```

Every task is echoed back for confirmation before it's saved. Parsing is imperfect; showing the interpretation is both better UX and a safeguard.

## Why Telegram

The HTTP Bot API is fast to build against and easy to iterate on during a one-day build. More importantly, Telegram supports **live location sharing with a bot in a private chat** — latitude, longitude, accuracy, and a fixed active duration — which is exactly what proximity triggers need.

WhatsApp's Cloud API setup would eat hours we don't have.

## Privacy as a feature

The bot never tracks anyone passively. It *can't* — it only receives location while the user has an active live-location session running, which the user starts and stops.

1. User creates a location-sensitive task.
2. Bot asks: *"To alert you near this place, share Live Location for the next 2 hours."*
3. User opts in through Telegram's native control.
4. Bot checks proximity during that session only.
5. Precise location updates are discarded when the session ends or the user says stop.

Temporary, task-scoped, user-initiated. We'll say this explicitly in the pitch — it's a design choice, not a limitation we're apologising for.

## Firing rules

A reminder that goes off while you're in a taxi is worse than no reminder.

- **Radius:** 250–400m default in dense areas, tuned by travel mode and user override.
- **Dwell check:** inside the zone across at least two location updates a few minutes apart, so pass-throughs don't trigger.
- **Time buffer:** estimated round-trip travel time + task time + 15 min slack must fit before the next calendar event.
- **Cooldown:** one alert per task per day unless snoozed.
- **Quiet hours** from the profile.

Hong Kong's vertical geography makes raw GPS noisy — same 300m radius, different building, different floor, or inside an MTR station. We'll surface this honestly and let users tune their own "nearby" threshold rather than pretending we've solved it.

## Action loop

The notification is not the end of the interaction. Every alert carries: **Done** (close, stop alerts) · **Snooze** (15/30/60 min) · **Skip today** · **Directions** (maps deep link) · **Wrong place** (re-resolve destination).

That's what makes it an assistant rather than a notification.

## Where Exa fits

Retrieval and enrichment, not ground truth.

- Resolving vague places: *"the repair shop near Mong Kok."*
- Opening hours, contact details, official pages.
- Budget-aware merchant suggestions for purchase errands.
- Contextual detail in the reminder: *"closes at 7pm, you have 48 minutes free."*

Confirmed locations are stored as lat/lng and geofenced deterministically. Exa never decides whether you're nearby.

## MVP scope (must work flawlessly)

1. Create an errand by chat.
2. Resolve the place, confirm with the user.
3. Capture travel mode + budget in a short profile setup.
4. Opt into live location.
5. Check geofence + availability + travel time.
6. Fire one well-timed, actionable reminder.
7. Close the loop with Done.

If Google Calendar OAuth is stable, use it. If not, availability falls back to a manual rule — `/free_until 19:00`, or "I'm free for 45 minutes." The project must work end to end without it. Judges reward a robust narrow demo over a broad fragile one.

## Out of scope

Always-on background tracking · every messaging platform · full calendar management · multi-stop route optimisation · autonomous purchases or bookings · "life operating system" positioning · any dashboard built before the chat flow works.

## Stack

Telegram Bot API · Python + FastAPI webhook · SQLite or Supabase for tasks and profiles · a lightweight scheduler/worker · a maps/geocoding provider for lat/lng and travel-time estimates · Exa for place and merchant enrichment · an LLM with structured JSON output for task extraction.

## Team split

| Owner | Scope |
|---|---|
| Backend / agent | Telegram webhook, intent extraction, task schema, task lifecycle |
| Integrations | Calendar or simulated availability, place lookup + geocoding, Exa enrichment |
| Location / logic | Live location ingestion, geofence + dwell, travel-time budget, firing rules, cooldowns |
| Profile / product | Profile capture flow, budget logic, conversation design, buttons, pitch and live demo |

Each component should be independently demoable so a single broken piece doesn't sink the demo.

## Demo script (under 90 seconds)

1. Show yesterday's message: *"Pick up my repaired keyboard in Mong Kok after class."*
2. Show the bot's confirmation and the saved task, including the profile it applied (walking, mid budget).
3. Share live location.
4. Class ends / "I'm free now."
5. Trigger: *"You're 6 minutes' walk from KeyPro Repair and you have 55 minutes before your next event."*
6. Tap Directions.
7. Tap Done — task completed, tracking stopped.

## Bottom line

Tell the bot about an errand once. It stays quiet until you're genuinely in the right place, with enough time, and the trip actually makes sense for how you travel and what you can spend.
