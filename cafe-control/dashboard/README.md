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

`localStorage` now holds theme and sort preference only. Those are presentation, and
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

## When the server goes away

A red banner appears and the figures stay on screen, frozen, rather than blanking.

That is deliberate. Mid-shift, a stale number plus a visible warning is more useful than
an empty grid — but the manager has to know which they are looking at, which is what the
banner is for. It clears itself on reconnect.

Note the floor keeps running regardless. The engine is in the server; the dashboard being
shut, crashed or disconnected changes nothing about sessions, billing or locks.

---

## Files

| File | |
|---|---|
| `index.html` | Shell and icon sprite |
| `app.js` | Fetch, render, act. ~450 lines, no dependencies |
| `styles.css` | Prototype styling plus the seven-state palette |
