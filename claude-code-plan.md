# Detour — 3-hour build with Claude Code

**Two people, two agents.** Claude Code writes the product; Codex works a
separate, non-overlapping file. Nobody hand-writes the app.

## Sponsor stack — final

| Tool | Role | Cost |
|---|---|---|
| **OpenRouter** ✅ | `parse_task` — one call, OpenAI-compatible | 0 min, you're making the call anyway |
| **Exa** ✅ | Opening hours at task creation, cached, failure-tolerant | ~15 min of agent time |
| **Ambiguous** ⚠️ | Primary calendar source, ICS as fallback | 10-min spike, then commit or drop |
| **Codex** ✅ | Second agent on `sim.py` + tests, parallel | free parallelism |
| CopilotKit ❌ | Frontend stack, no Telegram channel | 40 min for a second surface |

### Ambiguous — use it, but don't bet the demo on it

Ambiguous has a Calendar app and agents connect via CLI or MCP, so it's a real
substitute for the ICS feed, and a calendar tool call reads as more agentic than
an HTTP GET.

**The catch:** "agents connect via MCP" means your bot needs an MCP *client* at
runtime — a long-lived subprocess or SSE session living inside a polling loop.
That's a different weight class from `requests.get()`, and if it drops mid-demo
you have no calendar. A REST endpoint would be fine; MCP-only is a liability.

**So: `free_minutes()` takes two sources, Ambiguous first, ICS as fallback.**
The spec already isolates this in `services.py`, and `core.py` doesn't care which
answered. Ten extra minutes, and it kills the Google-cache risk at the same time
— if the ICS feed hasn't propagated, Ambiguous covers you, and vice versa. This
is the one fallback worth building in a three-hour project, because it's live
redundancy on your riskiest dependency rather than speculative abstraction.

**Spike it at 0:15, hard stop at 10 minutes.** One question: can you read a
calendar with a single short-lived HTTP call? Yes → wire it as primary. No → ICS
only, move on, you've lost ten minutes and nothing else.

Pitch line you get either way: *"Detour reads your calendar through Ambiguous,
where it has its own identity as a coworker — same workspace as the humans."*

---

## The split — this is the part people get wrong

Agentic coding does **not** parallelise across two humans on one repo. Two people
prompting one codebase produces merge conflicts and contradictory refactors.

So don't split the code. Split **code vs. everything else**:

| **Person A — drives Claude Code** | **Person B — everything the demo needs** |
|---|---|
| Runs the stages below, reviews diffs, fixes what the agent gets wrong | BotFather polish, calendar events, demo-place recon, video storyboard, pitch, manual phone testing as each stage lands |

Person B is not idle support. Demo prep is what always gets squeezed into the
last ten minutes and it is half your score. Front-load it.

**Codex gets `sim.py` and `tests/test_core.py` only** — files Claude Code is told
not to touch. Zero overlap, both agents useful, no conflicts. Person B drives it
between demo tasks.

---

## Timeline

### 0:00 – 0:15 — Setup (together)

```bash
mkdir detour && cd detour && git init
python -m venv .venv && source .venv/bin/activate
pip install python-telegram-bot openai exa-py icalendar requests python-dotenv pytest
```

- [ ] Drop `CLAUDE.md` in the repo root
- [ ] `.env` with `TELEGRAM_BOT_TOKEN`, `OPENROUTER_API_KEY`, `EXA_API_KEY`,
      `AMBIGUOUS_API_KEY`
- [ ] Token from @BotFather
- [ ] **Create demo calendar events now** — one ending shortly, one starting in
      ~50 min — in **both** Google (grab the secret ICS URL) and Ambiguous. Google
      caches this feed, so it cannot wait until 2:00.
- [ ] **Ambiguous spike, 10 minutes, hard stop.** Can you read a calendar with one
      short-lived HTTP call? Yes → primary source. No → ICS only, move on.
- [ ] Pick the demo place near the venue, note its real lat/lng

Split here.

### 0:15 – 1:15 — Claude Code stages

Run these as separate prompts, review the diff after each, and **don't start the
next until the acceptance line passes**. Resist the urge to hand it everything at
once — staged builds fail visibly, one-shot builds fail invisibly.

**Stage 1 — skeleton**
> Read CLAUDE.md. Create config.py, db.py, and geo.py exactly as specified.
> Include the DEMO_PLACES fallback dict. Write tests/test_geo.py covering
> haversine against a known distance and eta_minutes for all three modes. Run
> pytest and show me the output.

*Accept when:* pytest green.

**Stage 2 — services**
> Implement services.py per the spec: parse_task via OpenRouter, resolve_place via
> Nominatim with the User-Agent header and DEMO_PLACES checked first, free_minutes
> with the Ambiguous-then-ICS fallback chain, enrich_hours via Exa. Every one
> returns None on failure instead of raising. Write a scratch script that calls
> each against the real APIs and prints which calendar source answered.

*Accept when:* the scratch script resolves a real place and returns real minutes
from at least one calendar source. **Riskiest stage.** If ICS returns 1440 your
Google events likely haven't propagated yet rather than the code being wrong —
check whether Ambiguous answered, keep going, re-test at 1:00.

**Stage 3 — core**
> Implement core.py per the spec. No telegram imports anywhere in this file.
> on_location must stamp fired_at within the same call so repeated location
> updates can't double-fire.

*Accept when:* `python -c "import core"` works and grep finds no telegram import.

**Stage 4 — bot**
> Implement bot.py per the spec. Polling. Both the MESSAGE and EDITED_MESSAGE
> location filters. Include /sim. Send ChatAction.TYPING before LLM calls.

*Accept when:* the bot starts and `/start` responds on your phone.

**Stage 5 — full loop**
> Run through acceptance criteria 1-7 in CLAUDE.md. Fix everything that fails.
> Report each one as pass or fail.

### Meanwhile — Person B

- [ ] BotFather: `/setname`, `/setdescription`, `/setuserpic`, and `/setcommands`
      (the last one makes the blue Menu button appear — small touch, reads as
      finished product)
- [ ] Codex on `sim.py` + `tests/test_core.py`, against the CLAUDE.md contract
- [ ] Walk to the demo place, confirm GPS and that the pin lands sensibly
- [ ] Storyboard the video shot by shot
- [ ] Write and time the pitch out loud — 45 seconds, not 90
- [ ] Test each stage on your phone the moment it lands

### 1:15 – 1:45 — Integration + real-device testing (together)

Person B's phone is the test device; Person A drives fixes through Claude Code.
Fix only what breaks the loop.

### 1:45 – 2:15 — Hardening (add nothing)

This window *is* the judging criterion. Protect it.

- [ ] **Restart test.** Kill mid-task, restart, task still fires.
- [ ] **Fresh account test.** Second phone, account the bot has never seen,
      `/start` to Done. **Film this.** It's your strongest evidence.
- [ ] Bad input sweep: nonsense, unfindable place, location with no task, double-Done
- [ ] Pull the Exa key, confirm reminders still fire
- [ ] Pull `AMBIGUOUS_API_KEY`, confirm the calendar falls back to ICS cleanly
- [ ] Delete the demo chat so recording starts clean

### 2:15 – 2:45 — Record · 2:45 – 3:00 — Pitch

Video mechanics are in `workplan.md` — phone only (Telegram Desktop can't send
location at all), film the lock-screen notification, "3 hours later" card, say
on camera that you're dropping a pin rather than walking.

---

## Driving the agent well

- **Review every diff.** Three hours is short enough that one silent bad
  refactor is unrecoverable. Skimming a diff costs 20 seconds.
- **Reject scope creep on sight.** It will want retry decorators, a provider
  abstraction, a settings class. "Remove that, keep it direct." Every abstraction
  is time you don't have.
- **Paste real tracebacks**, not descriptions of them.
- **Commit after every green stage.** `git commit -am "stage 3 green"`. Your
  rollback point when stage 5 goes sideways.
- **If a stage fails twice, write that function yourself.** The agent is faster
  at 90% of this and slower at the last 10%. Know when to take over.

## Ranked failure modes

1. **Both people prompting the same repo.** One driver. Codex stays in its two files.
2. **Calendar events created late.** Google caches the ICS feed — 0:15 or you're
   demoing an empty calendar.
3. **One-shotting the whole build.** Stage it. A 600-line first draft that half
   works is worse than nothing at hour two.
4. **Adding features in the hardening window.** That window is the score.
5. **Leaving the video to the last ten minutes.** 2:15 is a hard start.
