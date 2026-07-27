# Unit Management Dashboard — Prototype

A self-contained prototype for managing a fleet of PCs / gaming stations ("units").
Track each unit's status, run live billed sessions, and see fleet utilization at a glance.

> No build step, no dependencies. Just open `index.html` in a browser.

## Features

- **Fleet KPIs** — occupancy meter, total units, in-use / available / down counts, and revenue closed today.
- **Live status distribution** — a stacked bar + legend showing the fleet at a glance.
- **Unit cards** — status badge, live session timer with running cost, specs, and quick actions.
- **Sessions** — start a session on an available unit (customer + hourly rate), watch the
  timer and cost tick live, then end it to bill the customer and log the session.
- **Lifecycle actions** — move units between *available → in use → maintenance → offline*.
- **Add / edit / delete units** and search + filter by status, zone, or free text; sort by ID,
  status, zone, or session time.
- **Detail view** per unit with full specs and recent session history.
- **Light / dark theme** (respects OS preference, with a manual toggle).
- **Persistence** — all changes are saved to `localStorage`, so the prototype keeps its
  state across reloads. Use **Reset demo data** in the footer to restore the seed fleet.

## Status model

| Status | Meaning | Color |
|---|---|---|
| Available | Idle and ready to allocate | green |
| In use | Running a billed session | blue |
| Maintenance | Temporarily out of service | amber |
| Offline | Down / awaiting repair | red |

Status colors follow a validated, colorblind-safe palette and are always paired with an
icon + label, so state never relies on color alone.

## Run it

Open the file directly:

```
open index.html        # macOS
xdg-open index.html    # Linux
```

Or serve the folder with any static server, e.g.:

```
python3 -m http.server 8000
# then visit http://localhost:8000
```

## Files

| File | Purpose |
|---|---|
| `index.html` | Markup, icon sprite, modal + toast hosts |
| `styles.css` | Design tokens (light/dark), layout, components |
| `app.js` | State, seed data, rendering, live timers, modals |

## Notes

This is a front-end prototype — data lives in the browser only. Wiring it to a real backend
would mean replacing the `localStorage` load/save and the seed data with API calls; the
render layer and interactions stay the same.
