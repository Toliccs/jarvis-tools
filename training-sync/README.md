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
real Garmin account.** When you are ready to change that, follow [The first real push, step by
step](#the-first-real-push-step-by-step) rather than improvising.

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

Before you put your password in here, read [How this reaches
Garmin](#how-this-reaches-garmin) in the limitations. There is no consumer API for Garmin, and
the library this uses to log in does things on the way in that you should know about rather
than find out about.

**If your Garmin account has two-factor authentication turned on, login will fail.** Not "may"
— the library is given no way to ask you for a code, so it stops with *"MFA Required but no
prompt_mfa mechanism supplied"*. Never tested against a real 2FA account, because nothing here
has been tested against a real account at all.

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

## The first real push, step by step

Nothing here has ever run against a real Garmin account. The first time you press Send, you
are the test. Do it in this order, on a day that doesn't matter, and stop at the first step
that doesn't behave. It takes half an hour and it is worth all of it.

**Pick your crash-test day first.** A rest day, or a day a week or two out that you already
know you're not riding. Not tomorrow's session.

1. **Leave the write switch off.** In `.env`, check that `TRAINING_SYNC_ALLOW_GARMIN_WRITES`
   is absent or `0`. Steps 2 to 4 all work with it off, and with it off nothing can reach your
   account no matter what you click.

2. **Start it and check it can read your Garmin calendar.** `python3 web_app.py`, then open
   <http://127.0.0.1:8765>. Under each day in the week view there's a Garmin panel. On a day
   with something on your Garmin calendar it says *"1 session on Garmin"* (or 2, or 3), and
   lists them. On a genuinely empty day it says nothing.

   What you are hunting for is the third thing: a day marked **"Garmin calendar unread"**, or
   a banner above the grid saying this app couldn't read your Garmin calendar. That is not an
   empty calendar, it's a blind app, and everything below depends on it not being blind.

3. **Check more than one month.** Step forward a few weeks and switch to the month view, so
   you cross at least two month boundaries. Garmin hands its calendar over one month at a
   time, so "this month reads fine" is a different claim from "next month reads fine", and
   this app has to read the month either side of a day to be sure about it. If a month comes
   back unread, sort that out before going on — a push that can't read the month it's writing
   into refuses rather than writes, so you'd just be collecting refusals.

4. **Preview a push, with the switch still off.** Tick **Approve for push** on your one
   crash-test workout (its `⋮` menu), press **Push to Garmin**, and read the drawer. One row,
   action *create*, with a reason. It should say it'll go as a timed placeholder. Nothing has
   been sent — this preview makes no network calls at all, in any configuration. If the drawer
   shows rows you didn't approve, or a date you didn't expect, close it and work out why
   before turning anything on.

5. **Now turn the switch on and send that one workout.** Put
   `TRAINING_SYNC_ALLOW_GARMIN_WRITES=1` in `.env`, restart the app, approve the same single
   workout, preview it again, and press **Send to Garmin**. One workout. Not the week.

6. **Go and look at Garmin Connect with your own eyes, and count.** Open Garmin Connect in a
   browser, go to that day, and count the rows. You are checking three things, in this order:
   **exactly one** session (not two — two is the failure that matters most), on the **right
   day**, with roughly the right name and length. The name will have a tag like `[ts:1a2b3c4d]`
   on it; that's deliberate, it's how this app recognises its own work.

   If you see two copies, stop there. Delete both in Garmin Connect, and don't push anything
   else until you know why it happened — a duplicate is the one failure this app cannot clean
   up after itself, because it never removes anything.

7. **Sync the Edge 520 and check it arrives there too.** The app puts things on your Garmin
   *calendar*; the head unit picks them up on its next sync, and this app cannot see whether
   that happened. Only you can.

8. **Now break it on purpose: delete that workout in Garmin Connect, and push again.** This is
   the most important step, because it's the thing that used to fail silently. Delete the
   session in Garmin Connect, come back to the app, and push the same workout again.

   The preview will say *"Unchanged since the last push"* — it doesn't know, because it makes
   no network calls. **Press Send anyway.** The real run looks at your calendar before it
   trusts its own record, and should report the row as a **create**, with a reason along the
   lines of *"The copy pushed earlier is no longer on your Garmin calendar for … , so it was
   sent again."*

   Go back to Garmin Connect and confirm it is there again, and there once. If instead the job
   reports success and your calendar is still empty, then the defect described in [Three ways it
   lost track](#three-ways-it-lost-track-of-what-was-actually-on-your-garmin-calendar) is back,
   the app is lying to you, and the "✓ On Garmin" label cannot be trusted on any card. Stop
   there.

9. **Only then, a week.** If steps 6 and 8 both came out right, approve a week and push it.
   Count the rows in Garmin Connect again afterwards. Do not skip the counting for the first
   few weeks; a duplicate is easy to fix on the day it appears and confusing a month later.

Do not set up anything automatic, scheduled or unattended. Not now, and not once it's working.

---

## Honest limitations

This is the section to read twice.

### Nothing here has ever been run against a real Garmin account

Every test in this repo runs against fakes. No real workout has ever been created, scheduled
or moved by this code, no real Garmin login has ever been performed by it, and no real Garmin
error response has ever been seen by it. The first time you press Send is genuinely untested
territory, and the way Garmin's unofficial endpoints answer in practice is exactly where this
kind of code goes wrong.

Every defect in the round-6 list below was found by reading the code and reasoning about it —
not by anything failing on a real account, because nothing has ever been tried on one. That
cuts both ways: there are almost certainly others of the same kind still in here that reading
did not catch.

So: **start small, and follow [The first real push, step by
step](#the-first-real-push-step-by-step)** above. It exists so you don't have to invent a safe
order under pressure.

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

### How this reaches Garmin

The section above refuses to build anything that works around TrainingPeaks' bot detection.
That refusal stands. It would be inconsistent to say it and then stay quiet about the Garmin
side, so — plainly, because an earlier version of this file said the opposite:

Garmin has no consumer API. There is no "generate a token" button on your account, and no
supported way for a program you run to read your own Garmin data. Every third-party Garmin
tool works around that the same way: by logging in with your credentials against the endpoints
Garmin's own apps use. That much this file has always said.

What it did not say is how the library gets that login accepted. `garminconnect` 0.3.2 tries
five strategies in turn — the iOS app's login endpoint, the same over plain `requests`, an SSO
embed widget, the web portal, the portal over plain `requests`. Three of them go through
`curl_cffi`, which makes the connection present the TLS fingerprint of Safari, Safari on iOS,
Chrome or Edge rather than Python's, and two of those rotate through that list when one gets
refused — because, in the library's own comment, different fingerprints land in different
Cloudflare rate-limit buckets. The portal strategy generates a randomised browser User-Agent
and matching `sec-ch-ua` headers. And two of the five wait a random 3-8 or 10-20 seconds
before posting, which the library's own constants call an *anti-WAF delay* and its own comment
explains as Cloudflare treating a fast GET-then-POST as bot-like.

That is browser impersonation, on the way in to your own account. This project writes none of
it and adds none of it — but it depends on it, and "it's in a dependency" is not a distinction
that would matter to Garmin. So the accurate statement is: your credentials, your account,
your data, sent nowhere but Garmin, over a login that presents itself as a browser to get in.

What that means for you:

- **This is not sanctioned access.** Garmin offers no supported route for it, so nothing here
  is covered by one — the same is true of every tool in this category. What that could cost you
  in practice is not something this file knows; the obvious risk is the account being
  rate-limited or locked. Their current terms of use are worth reading yourself rather than
  taking a summary from here, because it is your account.
- **Changing the dependency is your decision, not one to make quietly on your behalf.**
  `garminconnect>=0.3.2` is pinned in `requirements.txt`. If you'd rather not rely on that
  behaviour, there is one place to change: read and write both go through `GarminClient` in
  `garmin_client.py`, which is the only login in the project.
- **Logins are slow and can look like a hang.** If the first strategies get refused, those
  sleeps mean one login can take most of a minute before it succeeds or gives up.
- **Garmin can change any of it without notice**, and when they do, this stops working —
  possibly in the middle of a push.

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

### Three ways it lost track of what was actually on your Garmin calendar

Found by review in round 6. All three are fixed, and all three fixes are verified against
fakes only — none of this has been seen on a real account, because there has never been one.

They are written up rather than quietly closed because they were all the same shape: the app
believed its own record of what it had sent, in preference to the calendar Garmin would
actually show you, and reported success on the strength of it. **If this app ever tells you
something about your Garmin calendar that your own eyes disagree with, your eyes are right.**

**1. Push a workout, delete it in Garmin Connect, push again — and it reported success over an
empty calendar.** This is close to the first thing anyone would try, and it was broken. The
second push compared the plan against its own record, concluded nothing had changed, reported
*"noop, unchanged"*, made no call to Garmin at all, and left the card reading *"✓ On Garmin"*
with nothing on your calendar. Nothing in this app re-adds a session on your behalf, so it
would have stayed wrong until you found the workout missing on the bike.

What it does now: a real push no longer skips on the strength of its own record. Before it
declares there is nothing to do, it re-reads that month from Garmin — and the months either
side — and then says one of four things:

- the copy is there on the day → *"Already on your Garmin calendar for … , unchanged."*, and
  nothing is written;
- the copy is gone → the row becomes a **create**, the session is sent again, and the reason
  reads *"The copy pushed earlier is no longer on your Garmin calendar for … , so it was sent
  again."*;
- the copy is on your calendar but on a different day, because you dragged it in Garmin
  Connect → it says so and leaves it exactly where you put it. Your move wins;
- the month couldn't be read → it says it couldn't check, and writes nothing.

The part that still needs you: **the preview cannot see any of this.** It makes no network
calls by design, so a session you deleted in Garmin Connect still previews as *"Unchanged since
the last push"*. Press Send anyway — the real run is what looks. That's step 8 of the
[first-push checklist](#the-first-real-push-step-by-step), and it is the only way back.

**Still true, and worth knowing before it confuses you:** what got fixed is the *push*. The
green **"✓ On Garmin"** badge on the card is drawn from this app's push record alone, so in the
window between you deleting the session in Garmin Connect and you pressing Send again, the card
goes on claiming the session is there. The push is no longer fooled by that record; the badge
still reflects it.

The app does not hide the contradiction — on that same screen the day's Garmin panel is drawn
from the calendar Garmin actually returned, so it will list nothing for that day while the
badge above it says *"✓ On Garmin"*. **When those two disagree, the Garmin panel is the one
telling the truth**, and pushing again resolves it. Verified by running it, on a fake account,
in round 7.

**2. A session dragged across a month boundary in Garmin Connect could be duplicated.** Garmin
hands its calendar over one month at a time, and this app used to ask only about the month a
session was recorded in. Drag a workout from Aug 31 to Sep 1 in Garmin Connect — an ordinary
gesture; the month grid shows you those days side by side — and the next push looked at August,
found nothing, and sent a second copy. Two sessions on the Edge, the job reporting "done", and
the original orphaned where nothing here would ever find it again.

It now asks about the month either side as well, which is as far as one drag on a month grid
can reach. **Still true:** the fan-out stops there, deliberately — each extra read is another
chance to be rate-limited immediately before a write. A copy dragged more than a month away is
still out of reach. When a month can't be accounted for, the row names it rather than treating
it as proof your calendar is clear.

**3. A TrainingPeaks feed regeneration could make pushed workouts refuse to update, forever.**
If TrainingPeaks regenerates your calendar feed, every session's id changes. This app re-keys
its records onto the new ids by matching title and date, which is the whole thing standing
between a regeneration and losing every move you've made. But the ownership tag already written
on the Garmin workout still spelled the *old* id, so the ownership check stopped recognising
this app's own work: every later push refused, telling you the session *"carries no marker of
this app's"* — which was flatly untrue — and advising you, once per workout, to go and delete
your own synced sessions in Garmin Connect.

Now a marked row counts as this app's when the stored ids point at it, even where the id in the
tag is one the feed no longer uses, and the push after that settles into an ordinary no-op.
**Still true:** the re-keying only works when exactly one session in the new feed has the same
title on the same date. If a regeneration coincides with your coach renaming or moving that
session, or two sessions on a day share a title, the record is left orphaned rather than
guessed at — which leaves a copy on your Garmin calendar that nothing here will touch again.
The app names those on the page; only you can remove them.

### Two-factor authentication stops it at the door

If your Garmin account has 2FA turned on, login fails: the library is given no way to ask you
for a code, so it stops with *"MFA Required but no prompt_mfa mechanism supplied"*. The app
still starts and still shows your TrainingPeaks plan — it doesn't crash on a missing source —
but there is no readiness, no training status, no completed rides and no push at all. Whether
the failure is reported to you as clearly as "your account has 2FA and this can't handle it"
is itself untested; expect a generic Garmin login error.

This has never been tried against a real 2FA account, so what's above is read off the code
rather than observed. `garminconnect` does support an MFA prompt; this app simply doesn't pass
one, and wiring it up would mean deciding how a web page asks you for a code and what happens
to a push waiting on it.

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
| `garmin_client.py` | Reads activities, readiness and training status from Garmin — and holds the only Garmin login in the project; the writer comes through it too. Its docstring spells out what the login library does. |
| `trainingpeaks_client.py` | Unused skeleton for the Partner API. Raises if called. |
| `strava_client.py` | Present, but the calendar doesn't use it. |
| `alfred_adapter.py` | Glue for an external assistant. |

## Tests

```bash
cd training-sync
pip install pytest
python3 -m pytest -q
```

`pytest` is deliberately not in `requirements.txt` — running the app does not need it — so on
a fresh install it is the one extra line. Without it, `python3 -m pytest -q` answers *"No
module named pytest"*.

334 tests at the time of writing, all against fakes — `pytest -q` prints the real number. They
cover the write gates, the ownership rules, the text parser, the local store and the API
surface.

On a fresh clone you will see a handful fewer than that, reported as skips with their reason
printed: a few compare against an older commit, and two read the live `.plan_state.json`.
Neither exists in a clean checkout, so they stand aside rather than fail.

**What they establish about the network**, stated exactly, because an earlier version of this
file claimed more than they showed:

- Every test in `test_garmin_writer.py` — 71 of them, covering the only module that writes to
  Garmin — runs under an always-on guard that fails the test if anything calls
  `garminconnect.Garmin`, curl_cffi's session or the libcurl handle under it, `socket.connect`,
  or `urllib.request.urlopen`. Both layers are needed: `garminconnect` logs in through libcurl,
  which never touches a Python socket, so a socket-only guard would be watching a door the
  traffic doesn't use. One test deliberately trips the guard, so it is known to be armed rather
  than assumed to be.
- Two tests in `test_web_app.py` run the real HTTP surface against an in-process fake account
  with `garminconnect.Garmin` and `urllib.request.urlopen` blocked: a full calendar render, and
  a full push **with the write switch actually turned on**. The push runs to completion — it
  creates a workout and then moves it, so between them they make every call the writer has
  (upload, schedule, month read, unschedule) — and neither guard fires.

Those two web tests guard the library constructor and `urlopen`, not the socket, so what they
show is that no path through the app builds a real Garmin session or opens a URL of its own
during those flows. The socket-and-libcurl-level guard is the one on the writer's own test
file. Until this round the second of those two tests did not establish even that much: the
fake it ran against implemented only two of the calls the writer makes, so the push died on
its first write hop and the assertion accepted the failure. All it demonstrated was that a
push which never happens makes no outbound call — and this file cited it for the opposite.

**None of this proves the app works against Garmin.** It proves the app behaves correctly
against a fake that behaves the way we *believe* Garmin behaves. That is a different claim, and
the gap between them is the whole of the first limitation above.
