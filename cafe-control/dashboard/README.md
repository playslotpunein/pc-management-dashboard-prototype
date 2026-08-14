# Manager dashboard

A **view** over the control server. Nothing here decides anything.

Served by the FastAPI process at `http://<server>:8000/`. Open a browser on the counter PC
and that is the whole deployment — no build step, no Node, no second web server. Three
files, opened directly if you like.

---

## What changed from the prototype

The prototype this replaces kept the fleet in `localStorage` and computed each session's
elapsed time and running cost in the browser. That is the one thing the architecture is
most insistent about removing: **if the timer lives in the tab, closing the tab loses the
floor.**

| | Prototype | Now |
|---|---|---|
| Source of truth | `localStorage` | The control server |
| Countdown | computed in the browser | `remaining_seconds` from `/units` |
| Running total | computed in the browser | `running_total` from `/units` |
| Closing the tab | loses sessions | changes nothing |
| Two managers, two screens | diverge | identical |

There is **no arithmetic on time or money anywhere in `app.js`**. Not "recalculated
often" — none. Search it for `setInterval` and you will find exactly one, and it fetches
rather than counts.

`localStorage` now holds theme, sort and the kind filter only. Those are presentation, and
losing them costs nothing — unlike a cached rupee figure, which would be confidently
wrong at the counter.

---

## Reading the floor

Seven states, and the three that matter form an escalation ramp a manager reads from
across a room:

```
active (blue) ──▶ warning (amber) ──▶ overtime (orange) ──▶ locked (red)
```

Units sort by urgency by default, so anything needing attention floats to the top of the
grid. `overtime` and `locked` also get a ring and a slow pulse.

Colour never carries the meaning alone: every card pairs its colour with an icon and a
written label, so the ramp still reads for a colourblind manager and under the washed-out
light of a counter.

**"Owed on the floor"** is the KPI worth watching. It is the sum of in-progress bills — what
the floor owes *right now*, not what has already been collected. A rollup of closed
sessions alone reconciles at midnight and is useless at 8pm.

**An open-ended walk-in shows `open`, not a countdown.** It has no deadline and can never
run into overtime; it bills for time used.

---

## Pool and snooker tables

A table is a unit like any other — same clock, same rates, same sales — with one thing it
cannot do, which the card says outright.

**A grey `Manual` badge** marks any unit nothing can lock: the tables, and any PC whose
agent is not installed yet. Only the exception is marked; stamping "Agent lock" on all
thirty PCs would be noise. It is drawn in neutral ink and does not pulse on an urgent
card, because it is a fact about the unit rather than something newly happening.

**Past its time, a table reads `over by · nothing will lock it` and stays in overtime.**
It never shows `locked`, however far over it runs. A padlock next to four people still
playing would say the floor is under control when it is not — and a manager who believes
it waits for something that is never going to happen.

**The overdue alert repeats every five minutes**, unlike every other alert here, which
fires once. On a table the reminder is the only enforcement there is.

**The `Kind` filter** narrows the floor to one type — the tables upstairs, say — and the
choice survives a refresh, so whoever is running them sets it once a shift. Everything at
or below the filter row then describes that selection: the zone list drops to the zones
those units are actually in, and the state chips count only them, so a chip never claims
a number the grid below it does not show.

---

## When the server goes away

A red banner appears and the figures stay on screen, frozen, rather than blanking.

That is deliberate. Mid-shift, a stale number plus a visible warning is more useful than
an empty grid — but the manager has to know which they are looking at, which is what the
banner is for. It clears itself on reconnect.

Note the floor keeps running regardless. The engine is in the server; the dashboard being
shut, crashed or disconnected changes nothing about sessions, billing or locks.

---

## Sales

The shift report. Takings, what is still owed on the floor, and every individual sale.

Closed and running stay in **separate columns** rather than being added together: one is
money in the till, the other is money still on the floor, and merging them hides the
difference the manager is actually reconciling against.

Clicking a sale reveals the breakdown it was billed from — the **stored** lines, not a
recomputation. Pricing may have changed since, so recomputing would answer a different
question than "why was this ₹195?".

---

## Pricing

One card per unit type, showing the rate **in force right now**, with earlier rates kept
underneath.

A change inserts a new row; it never overwrites. Two things follow, and both are on screen:

- **Running sessions are untouched.** Each bills at the rate it captured when it started,
  so a 6pm price rise cannot change a 5pm bill.
- **Scheduled is not live.** A future-dated rate shows as `Scheduled`, never as the current
  price — a manager who cannot tell them apart will quote a session wrong.

A type with no rate says so plainly. Starting a session on it fails, which beats billing
everyone zero and finding out at closing time.

---

## Files

| File | |
|---|---|
| `index.html` | Shell and icon sprite |
| `app.js` | Fetch, render, act. ~450 lines, no dependencies |
| `styles.css` | Prototype styling plus the seven-state palette |
