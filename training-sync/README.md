# Training Calendar

Your plan lives in TrainingPeaks. Your bike is a Garmin Edge 520. Life happens in between.

This is a calendar you open in a web browser. It shows your TrainingPeaks plan, lets you say
"not Tuesday, I'm working — Wednesday instead", suggests how to rearrange the rest of the week
around that, and then — only when you explicitly tell it to — puts the session on your Garmin
so the Edge can pick it up.

It **reads** TrainingPeaks. It **reads** Garmin. The only thing it ever **writes** is a workout
onto your Garmin calendar — and it will not do that until you have said yes three separate
times, one of which is a switch that ships turned off.

Read the [Honest limitations](#honest-limitations) section before you rely on this. It is the
most important section in this file. In particular: **nothing in here has ever run against a
real Garmin account.**

---

## What it actually does

**The calendar is the product.** Everything else in this folder exists to feed it.

**Week and month views of your plan.** The week view gives you the seven days with each
session's title, how long it is, and how hard it is (Rest / Recovery / Endurance / Tempo /
Threshold / VO2 / Race). The month view is the same thing zoomed out. Buttons top-left step
back and forward; "Today" jumps home. There is also a plain table view (the "Table view"
button, or press `T`) that shows every number on screen as text, which is the honest fallback
if the colours don't read for you.

**Click a day to block it out.** Clicking a day header asks why — Work, Travel, Family,
Other — and offers "Mark unavailable" or "Only ~1h". There is a tick-box for *"Every Tuesday
for 8 weeks"* if it's a recurring work pattern rather than a one-off, and a tick-box for
*"Suggest where the session should go"*, which is on by default.

**Drag a session to another day.** Grab the card and drop it on a different day. Keyboard
works too: focus a card, press space to pick it up, arrow left/right to choose a day, space
to drop. Your move is stored locally as an override on top of the plan — the original
TrainingPeaks entry is never modified (it can't be; see limitations).

**Garmin's read of you, next to the plan.** A side panel shows your Training Readiness score
and Garmin's own feedback sentence, your Training Status (Productive, Maintaining, and so on),
and a seven-day load chart. When you actually ride, the completed activity shows on the day
next to what was planned.

**Suggestions, with the reason attached, that you have to approve.** Press the "Ask Alfred"
button (or block out a day with the suggest box ticked) and it re-plans that week and proposes
changes — move Tuesday's threshold to Wednesday, swap two sessions, shorten one, make it the
easier version, or drop it. Each suggestion is a card that states in a sentence *why*: which
day you marked unavailable, what it was trying to protect (recovery spacing, not stacking two
hard days back to back, the week's total load), and what it gave up to do it. A "Why?" button
opens the full working. Nothing on the calendar changes until you press **Approve**. You can
Reject instead, and it won't suggest the same thing again.

If it genuinely can't place a session, it says so — that appears as an unplaceable session
with the days it tried and why each was rejected, and there is nothing to approve. An
unsolvable week is shown, not hidden.

If the world has moved on since a suggestion was made — you already moved the session, or
un-blocked the day — the suggestion is marked spent and **cannot** be approved. The reason
printed on the card is no longer true, so approving it would act on a lie. It offers "Ask
again" instead.

**A separate, explicit push to Garmin.** Approving a suggestion changes your local plan. It
does **not** touch Garmin. To get a session onto the Edge you tick "Approve for push" on that
workout (its `⋮` menu), then press **Push to Garmin**, which opens a drawer showing exactly
what would be sent, row by row, with the action for each one (create / reschedule / update /
nothing-changed) and the reason. That preview makes no network calls at all. Then you press
**Send to Garmin**.

After a successful send, sync the Edge 520 with Garmin Connect — the app puts the workout on
your Garmin *calendar*; your head unit picks it up on its next sync. The app cannot tell you
whether it reached the bike, and it doesn't pretend to: it distinguishes "Garmin Connect
accepted this" from "this is on your Edge", and only claims the first.

---

## Have a look before you set anything up

Demo mode runs the whole interface on a sample training week with a sample Garmin signal. It
needs **no credentials at all** — no `.env`, no Garmin login, no TrainingPeaks feed. It never
contacts Garmin or TrainingPeaks, and it cannot send anything anywhere.

```bash
cd training-sync
pip install -r requirements.txt
python3 web_app.py --demo
```

Then open <http://127.0.0.1:8765> in a browser. A "Demo data" badge sits in the toolbar the
whole time so you can't mistake it for your real plan. Click around, block out a Tuesday, ask
for suggestions, open the push drawer. Press `?` for the in-app help.

---

## Running it on your own data

### 1. Install

```bash
cd training-sync
pip install -r requirements.txt
```

Python 3.9 or newer. Developed and tested on 3.11.

### 2. Make your settings file

```bash
cp .env.example .env
```

`.env` holds your passwords. Never commit it, never paste it into a chat. Every setting is
explained in the comments in `.env.example`.

### 3. Get your TrainingPeaks calendar feed URL

This is the read-only link TrainingPeaks gives you so that Google Calendar or Outlook can
subscribe to your schedule. In TrainingPeaks (the website, on a computer), go into your
account/calendar settings and look for the option about a **calendar feed** or **subscribing
to your calendar** — the exact wording and menu position have moved around over the years, so
hunt for those words rather than a specific path. What you want is a URL that starts with
`webcal://` or ends in `.ics`.

Paste it into `.env`:

```
TRAININGPEAKS_CALENDAR_FEED_URL=webcal://...
```

Treat that URL like a password. Anyone who has it can read your whole training schedule.

### 4. Garmin login

Your ordinary Garmin Connect email and password:

```
GARMIN_EMAIL=you@example.com
GARMIN_PASSWORD=...
```

These are stored only in your local `.env` and only ever sent to Garmin's own login endpoint.
Having them here makes a push *possible*; it does not make one happen — see the next section.

If your Garmin account has two-factor authentication turned on, expect login to fail. This
hasn't been tested against a 2FA account.

### 5. Two settings worth filling in

```
TRAINING_TZ=Europe/London       # your timezone, as an IANA name
GARMIN_FTP=250                  # your FTP in watts
```

`TRAINING_TZ` decides which *day* a session falls on. A workout timed at 23:30 UTC is Saturday
in London in winter and Sunday in summer, and only a named zone knows which. If you leave it
blank it falls back to the machine's own timezone, which is usually right and occasionally
very wrong.

`GARMIN_FTP` is currently **not used by anything this app sends** — see the limitations below.
Fill it in anyway; it costs nothing and it's what the setting is for.

### 6. Start it

```bash
python3 web_app.py
```

Open <http://127.0.0.1:8765>. It only listens on your own machine.

If a credential is missing or a service is unreachable, the page still loads and tells you
which source is missing and what to add. It will not fail to start because of a blank `.env`.

---

## Nothing reaches Garmin by accident

Four separate things must all be true before a single byte goes to your Garmin account:

1. **Your Garmin login is in `.env`.** Without it there is no session to write through.
2. **You approved that specific workout for push.** Per-session, via its `⋮` menu. Approval
   is checked again at the moment of writing, not just when the list was built — if you
   un-approve something after pressing Send but before it's reached, it is not sent.
3. **You pressed Send to Garmin.** Approving a *suggestion* only changes your local plan.
   Sending is a separate, deliberate action in a separate drawer.
4. **`TRAINING_SYNC_ALLOW_GARMIN_WRITES=1` is set in `.env`.** This defaults to **off**. With
   it off, the entire app works — reading, planning, moving sessions, previewing exactly what
   would be sent — and no request ever reaches Garmin. It is checked both at the API endpoint
   and again inside the write loop itself, so there is no route around it.

The point of separating (1) and (4) is that credentials say a write is *possible* and the
switch says it is *intended*. A stray script, a browser tab left open overnight, a mistaken
double-click on a machine where the switch is off — none of them can put a workout on your
Edge.

**The dry-run preview always works** and makes zero network calls. It works in demo mode, it
works with no Garmin login, it works with the write switch off. It decides what it *would* do
from what it recorded about previous pushes, so you can always answer "what would this send?"
before answering "should it?".

**Demo mode never writes**, full stop, regardless of every setting above.

**One push at a time.** If a push is running, starting another is refused rather than queued.

**If the app is killed mid-push**, that job is recorded as `interrupted` and is never silently
resumed — it cannot know which calls landed. Check Garmin Connect and push again if needed;
re-sending an unchanged workout is a no-op, not a duplicate.

---

## Honest limitations

This is the section to read twice.

### Nothing here has ever been run against a real Garmin account

Every test in this repo — all 308 of them — runs against fakes. No real workout has ever been
created, scheduled, or moved by this code. The first time you press Send is genuinely
untested territory, and the way Garmin's unofficial endpoints respond in practice is exactly
where this kind of code goes wrong.

So: **start small.**

1. Run the dry-run preview and read every row.
2. Approve **one** workout, for a day you don't mind getting wrong, and send that.
3. Open Garmin Connect in a browser and look at the calendar with your own eyes. Confirm the
   session is on the right day, with roughly the right name and duration.
4. Sync the Edge and confirm it arrives there too.
5. Only after that, trust it with a week.

Do not set up anything automatic or unattended.

### TrainingPeaks is read-only — adjustments do not go back into it

This is the single most important thing to understand about how this works.

The app reads your plan from your TrainingPeaks calendar feed. When you move Tuesday's session
to Wednesday, that move is stored **locally**, in this app, and pushed to **Garmin**. Garmin is
what your Edge actually executes, so what you ride will be right.

**TrainingPeaks will still show the original plan.** It will show the session on Tuesday, and
when you don't ride on Tuesday it will count that as missed. Your compliance and completion
statistics in TrainingPeaks compare what you did against **the plan as written, not the plan as
ridden**. If you or a coach look at that dashboard, it will understate how well you followed
the intent of the week. If you have a coach, tell them this app exists and that TrainingPeaks
compliance is not going to reflect reality.

**Why it works this way, and not a nicer way:** TrainingPeaks does not offer a self-serve API
key to individual athletes. There is no "generate a token" button in your settings. The only
supported programmatic access is their Partner API — how TrainerRoad, Zwift and the rest
integrate — which requires being approved as an integration partner, and that programme is not
open to new partners as things stand. So there is no legitimate route to write back to your own
TrainingPeaks account. (Worth re-checking occasionally; it's a business decision, not a
technical one, and business decisions change.)

The illegitimate route — driving a headless browser at trainingpeaks.com, or working around
their bot detection — is not built here and will not be. It violates their terms of service,
it breaks the moment they change their site, and it puts your account at risk. The personal
ICS calendar feed is a feature TrainingPeaks provides deliberately, it is read-only by nature,
and that is the boundary this app stays inside.

`trainingpeaks_client.py` is a skeleton for the Partner API, sitting unused. If TrainingPeaks
ever reopen partner access, that's where credentials would go. Until then it raises an error
pointing back here.

### The plan feed only gives text, and the details are guessed from it

The calendar feed gives three things per session: a **date**, a **title**, and a
**description** — the same free text your coach or plan author typed. That's it. There are no
structured fields in it: no target power, no target heart rate, no TSS, no interval structure.

So everything else is **parsed heuristically from that text**. "3x12min @ 95-100% FTP" is read
as a threshold session of about 60 minutes. "Recovery spin 45min, Z1" is read as recovery,
45 minutes. It handles zones, `NxM` interval notation, stated IF and TSS values, durations in
several formats, and common sport words.

**It can be wrong.** Unusual phrasing, a coach's private shorthand, a title in another
language, or a description that describes the *feel* rather than the numbers will all
mislead it. When it can't tell, it says "I couldn't tell how hard that is" rather than
guessing — but a confident wrong guess is possible, and a session it sizes wrongly is a
session it may schedule wrongly.

Look at what it thinks each session is. If it's reading your plan badly, that will be visible
immediately in the week view, and you should trust your own eyes over its label.

There is currently no way to correct it permanently through the interface. The engine supports
learned corrections internally, but nothing in the app writes them yet.

### What lands on your Edge is a timed placeholder, not a structured workout

Because the feed has no structured intervals, **every workout this app sends to Garmin is a
duration-only placeholder**: the right name, on the right day, for roughly the right length,
carrying the description text. There are no power targets, no heart-rate targets, and no
interval steps for the Edge to step you through.

The push preview says so on each row it is about to send ("Sent as a 60 min timed placeholder;
no power targets in the feed"), before you send it. It is not a bug being hidden — it is the
honest consequence of the feed containing only prose.

Practically: the Edge will show you the session name and the planned duration, and the
description text is carried across, but it will not beep at you at the end of each interval.
You still need to know the session. `GARMIN_FTP` is therefore currently inert — the code that
turns FTP into watt targets exists, but nothing in the app hands it structured steps to apply
those targets to.

The workout name on Garmin also carries a short ownership tag, like `[ts:1a2b3c4d]`. That is
how the app recognises its own work on your calendar instead of guessing from titles and
overwriting something you built by hand. You will see it in Garmin Connect and on the Edge.
It's slightly ugly and it is deliberate.

### There is no way to take a session off the Garmin calendar from this app

Sending is the only Garmin write it makes. There is no Remove button, no unpush, no delete.

**If you want a session off your Garmin calendar, delete it in Garmin Connect.** That takes one
click and tells you plainly whether it worked.

This used to be a feature and it was removed, because it kept reporting removals it had not
actually made. When Garmin wouldn't answer clearly, the app could not tell "I took it off" from
"I don't know whether I took it off", and it reported the wrong one — including, at one point,
offering a "Retry" button that would have *sent* the session the athlete was trying to get rid
of. Each fix was another guard on the same surface; rather than add one more, it's gone.

Marking a session skipped or moving it in this app therefore changes nothing on Garmin. Where
the app knows a copy of it is still sitting on your Garmin calendar, it says so on the card and
points you at Garmin Connect. Where it *couldn't read* your Garmin calendar, it says that
instead — "I could not check" is never drawn as "you're clear".

### Undo works over the API but there is no undo button

The server records every change and can reverse one: `GET /api/history` lists them and
`POST /api/history/{entry_id}/undo` reverses one. There is **no undo button in the calendar**,
on purpose. The reversal currently understands one session at a time, and an approved *swap*
moves two — so undoing half a swap would put both sessions on the same day, which is precisely
what the swap was calculated to avoid. Until it can undo both together, the button isn't there
rather than being there and being wrong.

What you can do in the interface: drag a session back to reverse a move, or use a session's
`⋮` menu → **"Revert to the coach's plan"** to throw away your changes to it.

### Readiness may simply be empty for you

The readiness panel reads Garmin's **Training Readiness** score. That is produced by newer
Garmin *watches* — an Edge 520 does not generate it. If your account has no readiness data, the
panel says "No readiness reading in the last 7 days" and the suggestions fall back to your plan
and the days you've blocked out, which is still the main thing. Training Status and completed
activities do not depend on it.

### Smaller things

- **Suggestions cover one week at a time.** Ask on the month view and it re-plans the single
  Monday-anchored week it anchored on, not the whole month. The response tells you which range
  it actually re-planned.
- **Strava is wired up but unused.** `strava_client.py` and its `.env` settings exist, but the
  calendar never reads from it — Garmin is the source of truth for what you actually rode. If
  you fill the Strava settings in, the status panel will say exactly that rather than pretend.
- **This is a local, single-user app.** It listens on `127.0.0.1` by default and there are no
  user accounts, no login and no permissions. Don't expose it to a network.
- **Your local data is in two places.** `.plan_state.json` holds your moves, blocked days,
  approvals and push records; `state/` holds a cached copy of the plan and the push log. Both
  contain your training data. Keep them out of git and off shared machines.
- **Garmin is unofficial.** The `garminconnect` library authenticates as you against the same
  endpoints the Garmin Connect mobile app uses. It's legitimate access to your own account, not
  bot evasion — but it is not a supported public API, and Garmin can change it without notice.

---

## For an assistant or a script talking to this system

The pieces are plain Python modules with no package wrapper. The folder name contains a hyphen,
so `import training_sync` does **not** work — run from inside `training-sync/`, or add that
folder to `sys.path`.

The two useful entry points are in `alfred_adapter.py`:

```python
from garmin_client import GarminClient
from trainingpeaks_calendar_feed import TrainingPeaksCalendarFeed
from alfred_adapter import get_live_feed, suggest_plan_adjustment

plan = TrainingPeaksCalendarFeed().upcoming_workouts(limit=14)   # read-only
recent = get_live_feed(GarminClient(), limit=5)                  # what he rode

proposals = suggest_plan_adjustment(
    plan,
    unavailable_days={"2026-07-28": "work"},
)
for p in proposals:
    print(p["action"], p["title"], "->", p["proposed_date"], "--", p["reason"])
```

`suggest_plan_adjustment` is a thin wrapper over `scheduler.propose_adjustments`, which is the
real engine. It is **pure**: same inputs, same proposal ids, no state written, no network call,
nothing applied. Every proposal comes back with `requires_approval: True`, and that is not
decoration — approving and pushing are things a **person** does in the calendar interface.

**Anything that changes state or reaches Garmin goes through the running web app's HTTP API,
not through these modules.** That is where the approval gates, the write switch and the audit
history live, and routing around them is how this app would start lying about what it did. The
adapter deliberately does not wrap the write endpoints.

With the app running on `127.0.0.1:8765`:

| What | Call |
|---|---|
| The whole view for a range | `GET /api/calendar?start=…&end=…` |
| Configuration and health | `GET /api/status` |
| Block out a day | `PUT /api/days/{date}/availability` |
| Move a session | `POST /api/workouts/{uid}/move` |
| Ask for suggestions | `POST /api/proposals` |
| Approve / reject one | `POST /api/proposals/{id}/approve` · `/reject` |
| Tick a session for push | `PATCH /api/workouts/{uid}` `{"push_approved": true}` |
| Preview a push (writes nothing) | `POST /api/push` `{"dry_run": true}` |
| Actually send | `POST /api/push` `{"dry_run": false}` |
| Change history / undo one | `GET /api/history` · `POST /api/history/{id}/undo` |

Interactive docs are at `/api/docs` while the app is running.

`GET /api/status` reports `capabilities.can_write_to_garmin`, which is false unless credentials
*and* the write switch *and* not-demo-mode are all true. An assistant should read that before
telling anyone a push is possible.

---

## The files

| File | What it is |
|---|---|
| `web_app.py` | The backend. Serves the calendar and the `/api` surface. Run this. |
| `static/index.html` | The whole front-end — one self-contained file, no build step, no CDN. |
| `scheduler.py` | The adjustment engine. Classifies sessions from text, and works out what to move where and why. Pure; no network, no state. |
| `plan_store.py` | Your local overrides: blocked days, moves, approvals, push records, change history. |
| `garmin_workout_writer.py` | The only code in this project that sends anything to Garmin. Nothing else in the folder writes to any outside service. |
| `trainingpeaks_calendar_feed.py` | Reads the ICS feed. Read-only, and the only thing that touches trainingpeaks.com. |
| `garmin_client.py` | Reads activities, readiness and training status from Garmin. |
| `trainingpeaks_client.py` | Unused skeleton for the Partner API. Raises if called. |
| `strava_client.py` | Present, but the calendar doesn't use it. |
| `alfred_adapter.py` | Glue for an external assistant. |

## Tests

```bash
cd training-sync
python3 -m pytest -q
```

308 tests, all against fakes. They cover the write gates, the ownership rules, the text parser,
the local store and the API surface. Two of them nail the network shut at the `garminconnect`
library boundary and assert that a full calendar render, and a full push with the write switch
actually turned on, make no outbound call at all.

**They do not prove this works against Garmin.** They prove it behaves correctly against a
fake that behaves how we *believe* Garmin behaves. That is a different claim, and the
difference is the whole of the first limitation above.
