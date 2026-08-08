# PlaySlot Control Server

The venue-side session engine: state machine, billing, alerts and sales, on SQLite.

This is Phase 3 — the part a café can actually buy. The agent and watchdog enforce a lock;
this decides *when* to lock, *what* to charge, and *what the floor is worth right now*.

---

## The rule everything else follows from

**The dashboard is a view, not the engine.** Timers, state transitions and billing all run
server-side, in this process. Close the browser tab and every session keeps running and
every unit stays correctly locked or unlocked.

Two consequences worth stating plainly:

- The `/units` response carries `remaining_seconds` and `running_total_paise` already
  computed. The dashboard renders them; it derives nothing.
- The engine ticks in the same process as the API, so no cross-process hop sits between
  a state change and a lock command.

---

## Running it

```bash
python -m venv .venv && ./.venv/bin/pip install -e ".[dev]"
./.venv/bin/uvicorn playslot.main:app --reload
```

The manager dashboard is served at `http://127.0.0.1:8000/`, with interactive API docs at
`/docs`. Both come from this one process — a counter PC needs nothing but a browser
pointed at localhost. No separate web server, no build step, no Node.

Defaults work with no configuration at all — a counter PC is not a place to debug missing
environment variables. Override with `PLAYSLOT_`-prefixed variables or a `.env`:

| Setting | Default | Notes |
|---|---|---|
| `PLAYSLOT_VENUE_ID` | `venue-local` | Written on every row from day one so enabling cloud sync later is not a backfill |
| `PLAYSLOT_DATABASE_URL` | `sqlite:///./data/playslot.db` | |
| `PLAYSLOT_TICK_SECONDS` | `1.0` | How often the floor is recomputed |
| `PLAYSLOT_WARNING_SECONDS` | `300` | The doc says exactly 300, not "about five minutes" |
| `PLAYSLOT_NO_SHOW_TIMEOUT_MINUTES` | `15` | |
| `PLAYSLOT_BUSINESS_DAY_STARTS_HOUR` | `6` | An 11pm session belongs to that evening's shift report |

```bash
./.venv/bin/python -m pytest        # 123 tests
```

---

## Getting a floor running

```bash
# Price the unit type first — starting a session without pricing fails loudly,
# which beats billing everyone zero and finding out at closing time.
curl -X POST localhost:8000/pricing -H 'Content-Type: application/json' \
  -d '{"unit_type":"pc","hourly_rate_paise":12000,"overtime_rate_paise_per_minute":500}'

curl -X POST localhost:8000/units -H 'Content-Type: application/json' \
  -d '{"name":"Nova","type":"pc","zone":"Battle Zone"}'

curl -X POST localhost:8000/sessions -H 'Content-Type: application/json' \
  -d '{"unit_id":"<id>","duration_minutes":60,"customer_ref":"Rohan M."}'

curl localhost:8000/units        # live countdown + running total
curl localhost:8000/sales/today  # closed + what is owed on the floor
```

---

## The state machine

One state per unit at all times. No skipping — and it is enforced, not merely documented:
an illegal transition raises rather than being written.

```
available ──▶ scheduled ──▶ active ──▶ warning ──▶ overtime ──▶ locked
     ▲            │            ▲          │           │           │
     │            │            └──────────┴───────────┴───────────┘
     │            │                   extension returns to active
     │            └──▶ available  (no-show after 15 min)
     └──▶ maintenance  (idle units only)
```

Three of these are counter-intuitive on purpose:

- **`warning` stays unlocked.** It is informational, so the manager can walk over and
  offer an extension.
- **`overtime` stays unlocked.** The grace period only means something if the machine
  still works during it. Cutting someone off mid-match is how you lose a regular.
- **`locked` is recoverable.** An extension returns the unit straight to `active`.

`active → locked` is *illegal*. The only route is through `overtime`, so grace can never
be skipped. `tests/test_lifecycle.py` asserts this directly.

**Missed ticks.** If the server is down across an expiry, a unit can be `active` while the
clock says `locked`. Rather than relaxing the rule, `lifecycle.path_to` walks the
intermediate states — the customer *did* get their grace, it elapsed while nothing was
watching — and the whole sequence lands in the activity log.

---

## Money

Every amount is **integer paise**. Never a float.

A float rupee total drifts as sessions, extensions and surcharges accumulate, and a café
running fifteen units for twelve hours does enough arithmetic for that drift to reach the
counter. Rounding is half-up, once, at the end: a customer shown ₹62.50 pays ₹63, where
Python's `round()` would give 62.

**The rate is snapshotted onto the session at start and never looked up again.** A manager
raising PS5 pricing at 6pm must not retroactively change a bill for someone who started at
5pm. The billing engine is never even given the current rate, so it cannot do otherwise.

The bill:

```
base            booked minutes at the snapshot rate
+ extensions    each one its own line, at the rate captured with it
+ surcharge     extra PS5 controllers, prorated
+ overtime      per minute, only once grace is consumed — grace itself is free
```

The total is the sum of the lines and nothing else, and the breakdown is **stored on the
sale** rather than recomputed. Pricing may have changed by the time anyone asks why the
total was what it was.

---

## The seven tables

`units` · `sessions` · `sales` · `pricing` · `agents` · `sync_outbox` · `activity_log`

Defined once in `playslot/models.py`. The same classes drive local SQLite and Supabase
Postgres, and both migrations generate from here — defining the schema twice is how the two
drift within a month.

- **UUID primary keys**, not autoincrement. A venue's local `session 41` would collide with
  every other venue's on sync.
- **`venue_id` on every table.** Unused standalone, but it is the column Supabase
  row-level security scopes on. Adding it later means a migration across every table at
  exactly the point there is real data.
- **`sync_outbox` fills even standalone**, where nothing drains it. `event_id` is the
  idempotency key: without it a flaky connection that retries mid-flush writes the same
  sale twice and inflates revenue.

---

## On payments

There is **no payment gateway here, and nothing stubbed for one.** Standalone means
payment is `cash` / `upi` / `card`, recorded at the counter — those are the permanent
values, not placeholders. `paid_online` exists in the enum but only ever appears on Zone 1
app bookings, which are not built. Nothing in this server calls Cashfree or Razorpay.

---

## The agent link

`ws://<server>/agent/{unit_id}` — one persistent connection per unit, in the same process
as the engine, so nothing sits between a lock decision and the command that carries it.

### Enrolment

```bash
curl -X POST 'localhost:8000/agents/enroll?unit_id=<id>'
# {"unit_id": "...", "device_token": "<64 hex chars>"}
```

The token is returned **once**. Re-enrolling rotates it and invalidates the old one —
that is the revocation path for a stolen token.

### Signing

Every message, **in both directions**, is HMAC-SHA256 signed with that token. Signing only
agent→server would leave the same hole viewed from the other end: a laptop on the café
wifi could send an agent a forged unlock without ever touching the control server.

The signed bytes are exactly:

```
unit_id \n message_type \n timestamp \n nonce \n body_json
```

`body_json` is JSON with **keys sorted, no whitespace** — `{"a":1,"b":2}`. The signature
is lowercase hex. Newline is the separator because it cannot appear in a UUID, a type
name or a number, so no field can be shifted into its neighbour.

The C# side must produce identical bytes:

```csharp
var bodyJson = JsonSerializer.Serialize(body, new JsonSerializerOptions
{
    // Sorted keys, no indentation. A custom converter or an ordered
    // dictionary is required — System.Text.Json does not sort by default.
});

var canonical = $"{unitId}\n{type}\n{timestamp}\n{nonce}\n{bodyJson}";

using var hmac = new HMACSHA256(Encoding.UTF8.GetBytes(deviceToken));
var sig = Convert.ToHexString(
    hmac.ComputeHash(Encoding.UTF8.GetBytes(canonical))).ToLowerInvariant();
```

Replay is blocked two ways: messages more than 30s from server time are rejected, and each
nonce is accepted once per device inside that window. A signature alone would not stop a
captured `unlock` being re-sent an hour later.

### Messages

**Agent → server.** First message must be `hello`; nothing is registered until it verifies,
so an unauthenticated socket can never receive a state push. Then `heartbeat` every 5s.

**Server → agent.** Exactly one type, `state`:

```json
{
  "locked": true,
  "unit_state": "locked",
  "session_end_utc": "2026-08-06T18:00:00+00:00",
  "grace_end_utc":   "2026-08-06T18:05:00+00:00"
}
```

One idempotent message means reconnection needs no special handling — the agent
reconnects, receives current state, and is correct. No missed-command replay, no ordering
to reason about.

### Why the end time is in there

That field is what makes **Zone 5's fail-safe** possible. The agent caches it, so when it
loses the server it waits sixty seconds, reads the cache and answers one question: is
there paid time remaining? Yes — stay unlocked and keep counting down locally. No — lock.

Without a pushed end time the agent has nothing to fail safe *on*, and a network blip
either strands a paying customer behind a lock screen or hands out free play.

⚠️ `session_end_utc: null` means **no deadline** (an open-ended walk-in), never "expired".
An agent that reads null as expired would lock out a customer paying by the minute the
moment the network hiccuped.

### Delivery is best-effort, deliberately

If an agent is not connected the command is logged and dropped, never queued. A dead link
is the agent's problem to survive — it fails safe on its own. Queueing would deliver a
stale lock to a machine whose customer has since paid for another hour.

Failed verifications are written to `activity_log` as `agent.verification_failed` and
increment `agents.failed_verifications`, so attempts are visible rather than merely
rejected. A climbing count on one unit is an attack in progress, not a flaky clock.

---

## Not built yet

- **The C# side of the agent link.** The server end is built and tested; the agent still
  speaks its local control pipe. It needs a WebSocket client, the canonical signing above,
  and the cached end time driving its fail-safe.
- **Alerts reaching the dashboard.** The alert engine raises the three events, but the
  dashboard polls state rather than subscribing to them, so a five-minute warning shows
  as an amber card rather than a toast. Server-sent events would close that.
- **Cloud sync.** The outbox fills; nothing drains it. Additive by design.
- **Alembic migrations.** `create_all` covers local SQLite; migrations land before anything
  ships to a second venue.
