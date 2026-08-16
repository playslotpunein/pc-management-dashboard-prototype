/* PlaySlot manager dashboard.
 *
 * A VIEW over the control server. Nothing here decides anything.
 *
 * The prototype this replaces held its fleet in localStorage and computed each
 * session's elapsed time and running cost in the browser. That is the one design point
 * the architecture is most insistent about getting rid of: if the timer lives in the
 * tab, closing the tab loses the floor. Every number below — the countdown, the running
 * total, the state of every unit — arrives already computed from /units, and this file
 * only draws it.
 *
 * Two consequences worth knowing while reading:
 *
 *   * There is no client-side arithmetic on time or money. Not "recalculated often" —
 *     none. Search this file for setInterval and you will find exactly one, and it
 *     fetches rather than counts.
 *   * localStorage holds theme and filter choices only. Those are presentation, and
 *     losing them costs nothing. No session, unit or amount is ever cached, because a
 *     stale cached rupee figure at the counter is worse than no figure at all.
 */

(() => {
  "use strict";

  // Same origin: the dashboard is served by the control server itself.
  const API = "";
  const POLL_MS = 1000;

  /** How each server state is drawn. Colour is never the only signal — every entry
   *  carries an icon and a written label as well. */
  const STATES = {
    available:   { label: "Available",  icon: "i-available",   rank: 6 },
    scheduled:   { label: "Booked",     icon: "i-scheduled",   rank: 4 },
    active:      { label: "In session", icon: "i-active",      rank: 3 },
    warning:     { label: "5 min left", icon: "i-warning",     rank: 2 },
    overtime:    { label: "Overtime",   icon: "i-overtime",    rank: 0 },
    locked:      { label: "Locked",     icon: "i-locked",      rank: 1 },
    maintenance: { label: "Maintenance",icon: "i-maintenance", rank: 7 },
  };

  /** What each unit type is called on screen. Also the order things are listed in. */
  const TYPE_LABEL = {
    pc: "PC",
    ps5: "PS5",
    sim: "Sim rig",
    pool: "Pool table",
    snooker: "Snooker table",
  };

  const UNIT_TYPES = Object.keys(TYPE_LABEL);

  /** How a unit's time is actually held when it runs out.
   *
   *  Worth showing on the card. A manager glancing at an overtime pool table needs to
   *  know that nothing is going to stop it — the sentence "you have to walk over" is
   *  the whole difference between this and a PC. */
  const ENFORCEMENT = {
    software: { label: "Agent lock", hint: "The agent locks this machine when grace runs out." },
    manual:   { label: "Manual",     hint: "Nothing is locked. You will be reminded every 5 minutes, and you handle it — walk over, or switch the screen off." },
  };

  /** States where a customer is mid-session and the unit cannot be sold. */
  const OCCUPIED = new Set(["active", "warning", "overtime", "locked"]);

  /** The two a manager must not miss from across the room. */
  const URGENT = new Set(["overtime", "locked"]);

  const state = {
    view: "floor",
    units: [],
    sales: null,
    saleList: [],
    pricing: [],
    openSale: null,
    linkDown: false,
    filterState: "all",
    filterZone: "all",
    filterType: "all",
    sort: "urgency",
    query: "",
    busy: new Set(),
  };

  const $ = (sel) => document.querySelector(sel);
  const el = (id) => document.getElementById(id);

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------------------------------------------------------------- transport

  async function api(path, options) {
    const response = await fetch(API + path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });

    if (!response.ok) {
      let detail = `${response.status} ${response.statusText}`;

      try {
        const body = await response.json();
        if (body.detail) detail = typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail);
      } catch { /* keep the status line */ }

      throw new Error(detail);
    }

    return response.status === 204 ? null : response.json();
  }

  async function refresh() {
    try {
      // Only the floor's data is polled every second. Sales and pricing change when a
      // manager acts, not on a timer, so they are fetched when their tab is opened —
      // re-querying every sale of the shift once a second would be pure waste.
      const [units, sales] = await Promise.all([
        api("/units"),
        api("/sales/today"),
      ]);

      state.units = units;
      state.sales = sales;

      if (state.view === "sales") state.saleList = await api("/sales");
      if (state.view === "pricing") state.pricing = await api("/pricing");

      if (state.linkDown) {
        state.linkDown = false;
        toast("Reconnected to the control server");
      }
    } catch (error) {
      // Deliberately keeps the last known values on screen rather than blanking them.
      // A frozen figure with a visible "stale" warning is more useful mid-shift than an
      // empty grid — but the manager has to be told, which is what the banner does.
      state.linkDown = true;
      el("linkbarText").textContent =
        `Control server unreachable (${error.message}) — figures below are stale.`;
    }

    render();
  }

  // ------------------------------------------------------------------ actions

  async function act(unitId, label, run) {
    if (state.busy.has(unitId)) return;

    state.busy.add(unitId);
    render();

    try {
      await run();
      await refresh();
      toast(label);
    } catch (error) {
      toast(error.message, true);
    } finally {
      state.busy.delete(unitId);
      render();
    }
  }

  const startSession = (unit, payload) =>
    act(unit.id, `Session started on ${unit.name}`, () =>
      api("/sessions", { method: "POST", body: JSON.stringify({ unit_id: unit.id, ...payload }) }));

  const extendSession = (unit, minutes) =>
    act(unit.id, `${unit.name} extended by ${minutes} min`, () =>
      api(`/sessions/${unit.current_session_id}/extend`, {
        method: "POST",
        body: JSON.stringify({ minutes }),
      }));

  const endSession = (unit, paymentMethod) =>
    act(unit.id, `${unit.name} closed`, () =>
      api(`/sessions/${unit.current_session_id}/end`, {
        method: "POST",
        body: JSON.stringify({ payment_method: paymentMethod }),
      }));

  const setMaintenance = (unit, on) =>
    act(unit.id, on ? `${unit.name} in maintenance` : `${unit.name} back in service`, () =>
      api(`/units/${unit.id}/maintenance?on=${on}`, { method: "POST" }));

  // ----------------------------------------------------------------- renderers

  /** Formats seconds the server already counted. No arithmetic on wall-clock time. */
  function clock(seconds) {
    const negative = seconds < 0;
    const total = Math.abs(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;

    const body = h > 0
      ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
      : `${m}:${String(s).padStart(2, "0")}`;

    return negative ? `+${body}` : body;
  }

  /** Everything the kind, zone and search filters allow — before the state chips.
   *
   *  Split out because the chips are counted over exactly this set. Counting them over
   *  the whole venue instead makes a chip that reads "Locked 2" while the kind filter is
   *  on the snooker tables, and clicking it lands the manager on an empty grid. */
  function candidateUnits() {
    const query = state.query.trim().toLowerCase();

    return state.units.filter((unit) => {
      if (state.filterZone !== "all" && (unit.zone || "") !== state.filterZone) return false;
      if (state.filterType !== "all" && unit.type !== state.filterType) return false;

      if (!query) return true;

      return [unit.name, unit.zone, unit.customer_ref, unit.type]
        .filter(Boolean)
        .some((field) => String(field).toLowerCase().includes(query));
    });
  }

  function visibleUnits() {
    const rows = candidateUnits().filter(
      (unit) => state.filterState === "all" || unit.state === state.filterState
    );

    const rank = (unit) => STATES[unit.state]?.rank ?? 9;

    const sorters = {
      // Default: the units needing attention float to the top of the grid.
      urgency: (a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name),
      name: (a, b) => a.name.localeCompare(b.name),
      zone: (a, b) => (a.zone || "").localeCompare(b.zone || "") || a.name.localeCompare(b.name),
      remaining: (a, b) =>
        (a.remaining_seconds ?? Infinity) - (b.remaining_seconds ?? Infinity),
    };

    return rows.sort(sorters[state.sort] || sorters.urgency);
  }

  function renderKpis() {
    const units = state.units;
    const usable = units.filter((u) => u.state !== "maintenance");
    const occupied = units.filter((u) => OCCUPIED.has(u.state));
    const available = units.filter((u) => u.state === "available");
    const urgent = units.filter((u) => URGENT.has(u.state));

    const pct = usable.length ? Math.round((occupied.length / usable.length) * 100) : 0;

    el("kpiOccupancy").textContent = `${pct}%`;
    el("kpiOccupancySub").textContent =
      `${occupied.length} of ${usable.length} usable units in session`;
    el("kpiOccupancyMeter").style.width = `${pct}%`;

    el("kpiTotal").textContent = units.length;
    el("kpiZones").textContent =
      `${new Set(units.map((u) => u.zone || "—")).size} zones`;

    el("kpiInUse").textContent = occupied.length;
    el("kpiUrgent").textContent = urgent.length
      ? `${urgent.length} need attention`
      : "all within time";

    el("kpiAvailable").textContent = available.length;

    const sales = state.sales;

    // "Owed on the floor" is the live figure. Without it the manager cannot see what is
    // actually outstanding right now, only what has already been collected.
    el("kpiLive").textContent = sales ? rupees(sales.live_paise) : "—";
    el("kpiLiveSub").textContent = sales
      ? `${sumBy(sales.by_type, "live_sessions")} sessions running`
      : "—";

    el("kpiClosed").textContent = sales ? rupees(sales.closed_paise) : "—";
    el("kpiClosedSub").textContent = sales
      ? `${sumBy(sales.by_type, "closed_sessions")} sessions closed`
      : "—";
  }

  const sumBy = (rows, key) => (rows || []).reduce((total, row) => total + (row[key] || 0), 0);

  const rupees = (paise) =>
    `₹${(paise / 100).toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

  /** The floor summarised in five bands, each one a different thing to do about it.
   *
   *  It used to draw all seven states. That was a mistake twice over. It duplicated the
   *  chips directly below — which carry every state, its own colour, its count, and a
   *  click — while being harder to read, because seven bands of colour is not something
   *  you take in at a glance. And it could not be made legible: warning, overtime and
   *  locked are three warm hues in a row, and in dark mode the usable lightness band is
   *  too narrow to separate three of them. Measured, adjacent pairs came out at ΔE 2.2
   *  under deuteranopia where 8 is the target.
   *
   *  Grouped, the bar answers the question a bar should answer — how is the floor doing
   *  — in colours that are far apart in both modes, and the chips keep the detail. */
  const BANDS = [
    { key: "available",   label: "Free",           states: ["available"] },
    { key: "active",      label: "In play",        states: ["scheduled", "active"] },
    { key: "warning",     label: "Ending soon",    states: ["warning"] },
    { key: "locked",      label: "Needs you",      states: ["overtime", "locked"] },
    { key: "maintenance", label: "Out of service", states: ["maintenance"] },
  ];

  function renderDistribution() {
    const counts = {};
    state.units.forEach((u) => { counts[u.state] = (counts[u.state] || 0) + 1; });

    const total = state.units.length || 1;

    const bands = BANDS
      .map((band) => ({
        ...band,
        count: band.states.reduce((sum, s) => sum + (counts[s] || 0), 0),
      }))
      .filter((band) => band.count);

    el("distBar").innerHTML = bands
      .map((band) =>
        `<span class="dist__seg u-${band.key}" style="width:${(band.count / total) * 100}%;background:var(--st)" title="${esc(band.label)}: ${band.count}"></span>`)
      .join("");

    // The legend is the identity channel — never colour alone. Every band on the bar
    // has a written label and a count here, which is also what lets the amber band sit
    // where it does: a mark below 3:1 on the surface is legal with visible labels.
    el("legend").innerHTML = bands
      .map((band) =>
        `<span class="legend__item u-${band.key}"><i class="dot" style="background:var(--st)"></i>${esc(band.label)} <b>${band.count}</b></span>`)
      .join("");
  }

  function renderChips() {
    const candidates = candidateUnits();
    const counts = { all: candidates.length };

    candidates.forEach((u) => { counts[u.state] = (counts[u.state] || 0) + 1; });

    // A chip can vanish when the kind changes — there is no "Locked" among the tables.
    // Left selected it would show an empty grid with nothing on screen explaining why,
    // so the selection falls back to All.
    if (state.filterState !== "all" && !counts[state.filterState]) state.filterState = "all";

    const chips = [["all", "All"]].concat(
      Object.entries(STATES)
        .filter(([key]) => counts[key])
        .map(([key, meta]) => [key, meta.label]));

    el("statusChips").innerHTML = chips
      .map(([key, label]) =>
        `<button class="chip${state.filterState === key ? " chip--on" : ""} u-${key}" data-state="${key}" role="tab" aria-selected="${state.filterState === key}">${esc(label)} <b>${counts[key] || 0}</b></button>`)
      .join("");
  }

  function renderZones() {
    // Narrowed by the kind above it: picking the snooker tables should not then offer
    // "Battle Zone", which has none in it. Kind is the outer choice, zone the inner one.
    const select = el("zoneFilter");
    const scoped = state.filterType === "all"
      ? state.units
      : state.units.filter((u) => u.type === state.filterType);

    const zones = [...new Set(scoped.map((u) => u.zone).filter(Boolean))].sort();
    const current = state.filterZone;

    select.innerHTML =
      `<option value="all">All zones</option>` +
      zones.map((z) => `<option value="${esc(z)}">${esc(z)}</option>`).join("");

    select.value = zones.includes(current) ? current : "all";
    state.filterZone = select.value;
  }

  function renderTypes() {
    /* Only lists the kinds this venue actually has, so a pure snooker parlour is not
       offered a PS5 filter and a PC café is not offered a table one. Counted alongside,
       because "Pool table 4" is the number a manager wants when the tables are the part
       of the floor they are responsible for. */
    const select = el("typeFilter");
    const counts = {};

    state.units.forEach((u) => { counts[u.type] = (counts[u.type] || 0) + 1; });

    const present = UNIT_TYPES.filter((t) => counts[t]);
    const current = state.filterType;

    select.innerHTML =
      `<option value="all">All kinds (${state.units.length})</option>` +
      present
        .map((t) => `<option value="${esc(t)}">${esc(TYPE_LABEL[t])} (${counts[t]})</option>`)
        .join("");

    select.value = present.includes(current) ? current : "all";
    state.filterType = select.value;
  }

  function renderCard(unit) {
    const meta = STATES[unit.state] || { label: unit.state, icon: "i-available" };
    const busy = state.busy.has(unit.id);
    const urgent = URGENT.has(unit.state);
    const occupied = OCCUPIED.has(unit.state);

    // Flagged only when it is the exception. Stamping "Agent lock" on every PC in the
    // room is noise; the one a manager has to walk over to is the one worth marking.
    const manual = unit.enforcement === "manual";

    let body = "";

    if (occupied) {
      // Straight from the server. The browser never works out how long is left.
      const remaining = unit.remaining_seconds;
      const over = remaining !== null && remaining <= 0;

      // "open / remaining" reads as nonsense on an open-ended walk-in, and the manager
      // needs to know at a glance that this one has no deadline to run out.
      // A table that has run past its grace stays in OVERTIME for as long as the
      // customers stay on it, so "over by" has to say plainly that no lock is coming.
      // Reading it as an ordinary overtime would leave a manager waiting for something
      // to happen that never will.
      const timeLabel = remaining === null
        ? "no deadline · billed by time used"
        : over
          ? (unit.state === "locked"
              ? "grace used up"
              : manual ? "over by · nothing will lock it" : "over by")
          : "remaining";

      body = `
        <div class="card__meta">
          <div>
            <div class="countdown${over ? " countdown--over" : ""}">${remaining === null ? "open" : esc(clock(remaining))}</div>
            <div class="card__sub">${esc(timeLabel)}</div>
          </div>
          <div style="text-align:right">
            <div class="card__total">${esc(unit.running_total || "—")}</div>
            <div class="card__sub">running total</div>
          </div>
        </div>
        ${unit.customer_ref ? `<div class="card__sub">${esc(unit.customer_ref)}</div>` : ""}
        <div class="card__actions">
          <button class="btn" data-act="extend" data-unit="${esc(unit.id)}" ${busy ? "disabled" : ""}>+15 min</button>
          <button class="btn" data-act="extend30" data-unit="${esc(unit.id)}" ${busy ? "disabled" : ""}>+30</button>
          <button class="btn btn--primary" data-act="end" data-unit="${esc(unit.id)}" ${busy ? "disabled" : ""}>End &amp; bill</button>
        </div>`;
    } else if (unit.state === "available") {
      body = `
        <div class="card__sub">Idle · ${esc(TYPE_LABEL[unit.type] || unit.type)}</div>
        <div class="card__actions">
          <button class="btn btn--primary" data-act="start" data-unit="${esc(unit.id)}" ${busy ? "disabled" : ""}>Start session</button>
          <button class="btn" data-act="maint-on" data-unit="${esc(unit.id)}" ${busy ? "disabled" : ""}>Maintenance</button>
        </div>`;
    } else if (unit.state === "maintenance") {
      body = `
        <div class="card__sub">Excluded from availability</div>
        <div class="card__actions">
          <button class="btn" data-act="maint-off" data-unit="${esc(unit.id)}" ${busy ? "disabled" : ""}>Back in service</button>
        </div>`;
    } else {
      body = `<div class="card__sub">${esc(unit.customer_ref || "Held for a booking")}</div>`;
    }

    return `
      <article class="card u-${esc(unit.state)}${urgent ? " card--urgent" : ""}">
        <div class="card__head">
          <div>
            <h3 class="card__title">${esc(unit.name)}</h3>
            <div class="card__sub">${esc(unit.zone || "—")}</div>
          </div>
          <div class="card__badges">
            ${manual ? `<span class="badge badge--manual" title="${esc(ENFORCEMENT.manual.hint)}">Manual</span>` : ""}
            <span class="badge">
              <svg width="14" height="14" aria-hidden="true"><use href="#${meta.icon}"/></svg>
              ${esc(meta.label)}
            </span>
          </div>
        </div>
        ${body}
      </article>`;
  }


  // ------------------------------------------------------------------- sales

  const METHOD_LABEL = { cash: "Cash", upi: "UPI", card: "Card", paid_online: "Paid online" };

  const timeOfDay = (iso) =>
    new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });

  function renderSales() {
    const sales = state.sales;

    if (!sales) return;

    el("sClosed").textContent = rupees(sales.closed_paise);
    el("sClosedSub").textContent = `${sumBy(sales.by_type, "closed_sessions")} sessions closed`;
    el("sLive").textContent = rupees(sales.live_paise);
    el("sLiveSub").textContent = `${sumBy(sales.by_type, "live_sessions")} still running`;
    el("sTotal").textContent = rupees(sales.total_paise);

    // By unit type. Closed and live stay in separate columns: one is money in the till,
    // the other is money still on the floor, and adding them up hides that difference.
    el("sByType").innerHTML = `
      <thead><tr>
        <th>Unit type</th><th class="num">Closed</th><th class="num">Running</th>
        <th class="num">Sessions</th><th class="num">Total</th>
      </tr></thead>
      <tbody>
        ${sales.by_type.map((row) => `
          <tr>
            <td>${esc(TYPE_LABEL[row.unit_type] || row.unit_type)}</td>
            <td class="num">${rupees(row.closed_paise)}</td>
            <td class="num">${row.live_paise ? rupees(row.live_paise) : "—"}</td>
            <td class="num">${row.closed_sessions} closed · ${row.live_sessions} live</td>
            <td class="num strong">${rupees(row.total_paise)}</td>
          </tr>`).join("")}
      </tbody>`;

    const methods = Object.entries(sales.by_payment_method || {});

    el("sByMethod").innerHTML = methods.length
      ? `<thead><tr><th>Method</th><th class="num">Taken</th></tr></thead>
         <tbody>${methods.map(([m, amount]) =>
            `<tr><td>${esc(METHOD_LABEL[m] || m)}</td><td class="num strong">${rupees(amount)}</td></tr>`
          ).join("")}</tbody>`
      : `<tbody><tr><td class="panel__note">Nothing settled yet this shift.</td></tr></tbody>`;

    // The individual sales. Clicking one reveals the stored breakdown it was billed
    // from — not a recomputation, the very lines written when the session closed.
    const rows = state.saleList;

    el("sEmpty").hidden = rows.length > 0;
    el("sList").innerHTML = rows.length ? `
      <thead><tr>
        <th>Time</th><th>Unit</th><th>Customer</th><th>Method</th><th class="num">Amount</th>
      </tr></thead>
      <tbody>
        ${rows.map((sale) => `
          <tr class="sale" data-sale="${esc(sale.id)}">
            <td>${esc(timeOfDay(sale.settled_at))}</td>
            <td>${esc(sale.unit_name || "—")}</td>
            <td>${esc(sale.customer_ref || "—")}</td>
            <td><span class="pill">${esc(METHOD_LABEL[sale.payment_method] || sale.payment_method)}</span></td>
            <td class="num strong">${esc(sale.amount || rupees(sale.amount_paise))}</td>
          </tr>
          ${state.openSale === sale.id ? `
          <tr class="tbl__lines"><td colspan="5">
            <ul>
              ${sale.lines.map((line) => `
                <li><span>${esc(line.description)}</span><b>${rupees(line.amount_paise)}</b></li>`).join("")}
              <li><span class="strong">Total</span><b>${rupees(sale.amount_paise)}</b></li>
            </ul>
          </td></tr>` : ""}
        `).join("")}
      </tbody>` : "";
  }

  // ----------------------------------------------------------------- pricing


  function renderPricing() {
    el("pricingGrid").innerHTML = UNIT_TYPES.map((type) => {
      const rows = state.pricing.filter((r) => r.unit_type === type);
      const current = rows.find((r) => r.is_current);
      const scheduled = rows.filter((r) => !r.is_current && new Date(r.effective_from) > new Date());
      const history = rows.filter((r) => !r.is_current && new Date(r.effective_from) <= new Date());

      return `
        <article class="card">
          <div class="card__head">
            <div>
              <h3 class="card__title">${esc(TYPE_LABEL[type])}</h3>
              <div class="card__sub">${current ? "Rate in force now" : "No rate set"}</div>
            </div>
            ${current ? `<span class="pill pill--live">Live</span>` : ""}
          </div>

          ${current ? `
            <div class="price__now">
              <span class="price__rate">${rupees(current.hourly_rate_paise)}</span>
              <span class="price__per">/ hour</span>
            </div>
            <div class="price__row"><span>Overtime</span><b>${current.overtime_rate_paise_per_minute ? rupees(current.overtime_rate_paise_per_minute) + " / min" : "at the hourly rate"}</b></div>
            ${type === "ps5" ? `<div class="price__row"><span>Extra controller</span><b>${current.controller_surcharge_paise_per_hour ? rupees(current.controller_surcharge_paise_per_hour) + " / hr" : "free"}</b></div>` : ""}
          ` : `
            <p class="card__sub" style="margin:10px 0">
              Starting a session on a ${esc(TYPE_LABEL[type])} will fail until a rate is set —
              which beats billing everyone zero and finding out at closing time.
            </p>`}

          ${scheduled.length ? scheduled.map((r) => `
            <div class="price__row">
              <span><span class="pill pill--soon">Scheduled</span></span>
              <b>${rupees(r.hourly_rate_paise)} from ${esc(new Date(r.effective_from).toLocaleString())}</b>
            </div>`).join("") : ""}

          <div class="card__actions">
            <button class="btn btn--primary" data-price="${esc(type)}">${current ? "Change rate" : "Set rate"}</button>
          </div>

          ${history.length ? `
            <details class="price__hist">
              <summary>${history.length} earlier rate${history.length > 1 ? "s" : ""}</summary>
              <ul>
                ${history.map((r) => `
                  <li><span>${esc(new Date(r.effective_from).toLocaleString())}</span><b>${rupees(r.hourly_rate_paise)} / hr</b></li>`).join("")}
              </ul>
            </details>` : ""}
        </article>`;
    }).join("");
  }

  function promptPrice(type) {
    const current = state.pricing.find((r) => r.unit_type === type && r.is_current);

    openModal(`${TYPE_LABEL[type]} rate`, `
      <label class="field"><span>Hourly rate (₹)</span>
        <input id="p-rate" type="number" min="0" step="10" value="${current ? current.hourly_rate_paise / 100 : 120}" /></label>
      <label class="field"><span>Overtime penalty (₹ per minute past grace)</span>
        <input id="p-over" type="number" min="0" step="1" value="${current ? current.overtime_rate_paise_per_minute / 100 : 0}" /></label>
      <p class="modal__note">Leave at 0 and overtime is billed at the hourly rate above —
        time played is time charged. It is never free; the free window is the grace period.</p>
      ${type === "ps5" ? `
      <label class="field"><span>Extra controller (₹ per hour, each)</span>
        <input id="p-ctrl" type="number" min="0" step="10" value="${current ? current.controller_surcharge_paise_per_hour / 100 : 0}" /></label>` : ""}
      <p class="modal__lead" style="margin-top:14px">
        Applies to sessions started from now on. Anything already running keeps the rate it
        captured at its start, and the current rate is kept as history rather than replaced.
      </p>
      <div class="modal__actions">
        <button class="btn" type="button" data-close>Cancel</button>
        <button class="btn btn--primary" id="m-go" type="button">Save rate</button>
      </div>`, () => {
      el("m-go").onclick = async () => {
        const payload = {
          unit_type: type,
          hourly_rate_paise: Math.round(Number(el("p-rate").value) * 100),
          overtime_rate_paise_per_minute: Math.round(Number(el("p-over").value) * 100),
          controller_surcharge_paise_per_hour: Math.round(Number(el("p-ctrl")?.value || 0) * 100),
        };

        if (!(payload.hourly_rate_paise >= 0)) { toast("Enter a valid rate", true); return; }

        closeModal();

        try {
          await api("/pricing", { method: "POST", body: JSON.stringify(payload) });
          await refresh();
          toast(`${TYPE_LABEL[type]} now ${rupees(payload.hourly_rate_paise)}/hr for new sessions`);
        } catch (error) {
          toast(error.message, true);
        }
      };
    });
  }

  // -------------------------------------------------------------------- views

  async function showView(view) {
    state.view = view;
    state.openSale = null;

    document.querySelectorAll(".tab").forEach((tab) => {
      const on = tab.dataset.view === view;
      tab.classList.toggle("tab--on", on);
      tab.setAttribute("aria-selected", String(on));
    });

    ["floor", "sales", "pricing"].forEach((name) => {
      el(`view-${name}`).hidden = name !== view;
    });

    // Search and Add-unit belong to the floor only; leaving them live on other tabs
    // would offer controls that do nothing to what is on screen.
    el("search").parentElement.style.display = view === "floor" ? "" : "none";
    el("addBtn").style.display = view === "floor" ? "" : "none";

    await refresh();
  }

  function render() {
    el("linkbar").dataset.down = state.linkDown ? "1" : "0";

    if (state.view === "sales") { renderSales(); renderFooter(); return; }
    if (state.view === "pricing") { renderPricing(); renderFooter(); return; }

    renderKpis();
    renderDistribution();

    // Order matters: each of these can reset the filter below it when the choice it
    // held no longer exists, and the chips are counted from whatever the two above
    // them settled on. Run the other way round and the counts are a frame stale.
    renderTypes();
    renderZones();
    renderChips();

    const rows = visibleUnits();

    el("grid").innerHTML = rows.map(renderCard).join("");
    el("emptyState").hidden = rows.length > 0;

    renderFooter();
  }

  function renderFooter() {
    el("footStamp").textContent = state.linkDown
      ? "Disconnected"
      : `Live · updated ${new Date().toLocaleTimeString()}`;
  }

  // -------------------------------------------------------------------- modals

  function openModal(title, html, onMount) {
    el("modalTitle").textContent = title;
    el("modalBody").innerHTML = html;
    el("modalHost").hidden = false;
    onMount?.();
  }

  const closeModal = () => { el("modalHost").hidden = true; };

  function promptStart(unit) {
    openModal(`Start a session on ${unit.name}`, `
      <label class="field"><span>Customer (optional)</span>
        <input id="m-customer" type="text" placeholder="Name or phone" autocomplete="off" /></label>
      <label class="field"><span>Duration</span>
        <select id="m-duration">
          <option value="30">30 minutes</option>
          <option value="60" selected>1 hour</option>
          <option value="120">2 hours</option>
          <option value="0">Open-ended (bill by time used)</option>
        </select></label>
      ${unit.type === "ps5" ? `
      <label class="field"><span>Extra controllers</span>
        <select id="m-controllers">
          <option value="0" selected>None</option><option value="1">1</option>
          <option value="2">2</option><option value="3">3</option>
        </select></label>` : ""}
      <div class="modal__actions">
        <button class="btn" type="button" data-close>Cancel</button>
        <button class="btn btn--primary" id="m-go" type="button">Start</button>
      </div>`, () => {
      el("m-go").onclick = () => {
        const payload = {
          customer_ref: el("m-customer").value.trim(),
          duration_minutes: Number(el("m-duration").value),
          extra_controllers: Number(el("m-controllers")?.value || 0),
        };
        closeModal();
        startSession(unit, payload);
      };
    });
  }

  /** Explains the gap between the clock and the bill, which is the question asked at the
   *  counter. Two different gaps are possible and they mean opposite things: minutes the
   *  customer played and owes for, or minutes the machine was locked and they do not. */
  function billNote(bill) {
    const over = bill.actual_minutes - bill.booked_minutes;

    if (bill.unbilled_minutes) {
      return `<p class="modal__note">Played ${bill.actual_minutes} min against
        ${bill.booked_minutes} booked, but it locked when the grace ran out —
        the ${bill.unbilled_minutes} min since are <b>not charged</b>.</p>`;
    }

    if (bill.overtime_minutes) {
      return `<p class="modal__note">Played ${bill.actual_minutes} min against
        ${bill.booked_minutes} booked — ${bill.overtime_minutes} min of that is
        overtime. Nothing locks this unit, so the overrun is charged.</p>`;
    }

    if (over > 0) {
      return `<p class="modal__note">${over} min past the booked time, inside the
        grace period. Not charged.</p>`;
    }

    return "";
  }

  function promptEnd(unit) {
    /* Itemised, not just a total. The manager is about to take money and has to be able
       to answer "why is it that much?" while the customer is standing there — most often
       because the session ran over, which is the line a single figure hides. Fetched from
       the server, so it is the same computation the sale will be written from. */
    openModal(`Close ${unit.name}`, `
      <div id="m-bill"><p class="modal__lead">Working out the bill…</p></div>
      <label class="field"><span>Paid by</span>
        <select id="m-method">
          <option value="cash" selected>Cash</option>
          <option value="upi">UPI</option>
          <option value="card">Card</option>
        </select></label>
      <div class="modal__actions">
        <button class="btn" type="button" data-close>Cancel</button>
        <button class="btn btn--primary" id="m-go" type="button">End &amp; bill</button>
      </div>`, () => {
      el("m-go").onclick = () => {
        const method = el("m-method").value;
        closeModal();
        endSession(unit, method);
      };

      api(`/sessions/${unit.current_session_id}/bill`)
        .then((bill) => {
          const host = el("m-bill");

          // The modal may already be closed, or reopened on another unit.
          if (!host) return;

          host.innerHTML = `
            <table class="bill">
              <tbody>
                ${bill.lines.map((line) => `
                  <tr${line.kind === "overtime" ? ' class="bill__over"' : ""}>
                    <td>${esc(line.description)}</td>
                    <td class="num">${esc(rupees(line.amount_paise))}</td>
                  </tr>`).join("")}
              </tbody>
              <tfoot>
                <tr><td>Total</td><td class="num">${esc(bill.total)}</td></tr>
              </tfoot>
            </table>
            ${billNote(bill)}`;
        })
        .catch(() => {
          const host = el("m-bill");

          // Falls back to the figure already on the card rather than blocking the close;
          // a manager mid-shift needs to be able to take the money regardless.
          if (host) {
            host.innerHTML =
              `<p class="modal__lead">Billing <b>${esc(unit.running_total || "—")}</b> for this session.</p>`;
          }
        });
    });
  }

  function promptAddUnit() {
    openModal("Add a unit", `
      <label class="field"><span>Name</span><input id="m-name" type="text" placeholder="PC 1" /></label>
      <label class="field"><span>Type</span>
        <select id="m-type">
          ${UNIT_TYPES.map((t) => `<option value="${esc(t)}">${esc(TYPE_LABEL[t])}</option>`).join("")}
        </select></label>
      <label class="field"><span>Zone</span><input id="m-zone" type="text" placeholder="Battle Zone" /></label>
      <label class="field"><span>When time runs out</span>
        <select id="m-enforce">
          <option value="">Whatever this type normally does</option>
          ${Object.entries(ENFORCEMENT).map(([key, meta]) =>
            `<option value="${esc(key)}">${esc(meta.label)} — ${esc(meta.hint)}</option>`).join("")}
        </select></label>
      <p class="modal__note" id="m-enforce-note"></p>
      <div class="modal__actions">
        <button class="btn" type="button" data-close>Cancel</button>
        <button class="btn btn--primary" id="m-go" type="button">Add</button>
      </div>`, () => {
      // Left on the default, the server picks from the type. The note spells out what
      // that will be, because "a PS5 cannot be software-locked" is not something the
      // person adding a unit at the counter should be expected to already know.
      const note = () => {
        const chosen = el("m-enforce").value;
        const implied = { pc: "software", sim: "software", ps5: "manual", pool: "manual", snooker: "manual" };
        const mode = chosen || implied[el("m-type").value] || "manual";

        el("m-enforce-note").textContent = ENFORCEMENT[mode].hint;
      };

      el("m-type").onchange = note;
      el("m-enforce").onchange = note;
      note();

      el("m-go").onclick = async () => {
        const payload = {
          name: el("m-name").value.trim(),
          type: el("m-type").value,
          zone: el("m-zone").value.trim(),
        };

        // Omitted rather than sent as null, so the server's type-derived default is
        // what fills it in.
        if (el("m-enforce").value) payload.enforcement = el("m-enforce").value;

        if (!payload.name) { toast("A unit needs a name", true); return; }

        closeModal();

        try {
          await api("/units", { method: "POST", body: JSON.stringify(payload) });
          await refresh();
          toast(`${payload.name} added`);
        } catch (error) {
          toast(error.message, true);
        }
      };
    });
  }


  // --------------------------------------------------------------------- alerts

  /* Alerts arrive pushed, not polled.
   *
   * The interesting ones are edges — "five minutes left", "grace expired" — and an edge
   * is exactly what polling loses: either it falls between two requests, or the client
   * re-derives it, which is the browser-side logic this dashboard exists without.
   *
   * State keeps its own one-second poll. This stream carries only the events.
   */

  const ALERT_STYLE = {
    five_minute_warning: { icon: "i-warning",  tone: "warn",  hold: 6000 },
    expired:             { icon: "i-overtime", tone: "over",  hold: 8000 },
    grace_timeout:       { icon: "i-locked",   tone: "lock",  hold: 12000 },
    no_show:             { icon: "i-scheduled",tone: "warn",  hold: 10000 },

    // Held longest of all. It is the only enforcement a pool table has, and it is on
    // screen precisely when nothing else is going to stop the unit running on.
    overdue:             { icon: "i-overtime", tone: "over",  hold: 15000 },
  };

  let alertStream = null;

  function connectAlerts() {
    if (alertStream) alertStream.close();

    alertStream = new EventSource(API + "/events");

    alertStream.addEventListener("alert", (event) => {
      let payload;

      try {
        payload = JSON.parse(event.data);
      } catch {
        return;
      }

      showAlert(payload);

      // A lock or an expiry changes the floor, so pull fresh state rather than waiting
      // out the remainder of the poll interval.
      if (payload.triggers_lock || payload.kind === "no_show") refresh();
    });

    // EventSource reconnects on its own; this only surfaces that it is trying, so a
    // manager is never quietly left without alerts.
    alertStream.onerror = () => {
      if (alertStream.readyState === EventSource.CLOSED) {
        setTimeout(connectAlerts, 3000);
      }
    };
  }

  const seenAlerts = new Set();

  function showAlert(payload) {
    // The stream replays what fired just before this tab connected, so a reload does
    // not re-toast alerts the manager already dealt with.
    //
    // The message is part of the key, not just the kind. Overdue is deliberately
    // repeated by the server every five minutes on a unit nothing can lock, and keying
    // on the kind alone would swallow every repeat after the first — silently turning
    // the one form of enforcement a pool table has into a single toast at minute five.
    // Each repeat carries a higher minute count, so it differs; a genuine replay of the
    // same alert does not, and is still suppressed.
    const key = `${payload.session_id}:${payload.kind}:${payload.message}`;

    if (seenAlerts.has(key)) return;

    seenAlerts.add(key);

    const style = ALERT_STYLE[payload.kind] || { icon: "i-warning", tone: "warn", hold: 6000 };

    pushAlert(payload.message, style);
  }

  function pushAlert(message, style) {
    const host = el("alerts");

    const node = document.createElement("div");
    node.className = `alert alert--${style.tone}`;
    node.setAttribute("role", "status");
    node.innerHTML =
      `<svg width="17" height="17" aria-hidden="true"><use href="#${style.icon}"/></svg>` +
      `<span>${esc(message)}</span>` +
      `<button class="alert__x" type="button" aria-label="Dismiss">&times;</button>`;

    node.querySelector(".alert__x").onclick = () => node.remove();

    host.append(node);

    // Keep the stack short. On a busy evening the newest few are what matter, and a
    // column of twenty toasts covers the floor grid underneath.
    while (host.children.length > 5) host.firstElementChild.remove();

    setTimeout(() => node.remove(), style.hold);
  }

  // --------------------------------------------------------------------- toast

  let toastTimer;

  function toast(message, isError) {
    const node = el("toast");
    node.textContent = message;
    node.hidden = false;
    node.classList.toggle("toast--error", Boolean(isError));

    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.hidden = true; }, 3200);
  }

  // --------------------------------------------------------------------- wiring

  function findUnit(id) {
    return state.units.find((u) => u.id === id);
  }

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-close]")) { closeModal(); return; }

    const tab = event.target.closest("[data-view]");
    if (tab) { showView(tab.dataset.view); return; }

    const price = event.target.closest("[data-price]");
    if (price) { promptPrice(price.dataset.price); return; }

    const saleRow = event.target.closest("[data-sale]");
    if (saleRow) {
      // Toggle the stored breakdown for this sale.
      state.openSale = state.openSale === saleRow.dataset.sale ? null : saleRow.dataset.sale;
      render();
      return;
    }

    const chip = event.target.closest("[data-state]");
    if (chip) { state.filterState = chip.dataset.state; render(); return; }

    const button = event.target.closest("[data-act]");
    if (!button) return;

    const unit = findUnit(button.dataset.unit);
    if (!unit) return;

    switch (button.dataset.act) {
      case "start":     promptStart(unit); break;
      case "end":       promptEnd(unit); break;
      case "extend":    extendSession(unit, 15); break;
      case "extend30":  extendSession(unit, 30); break;
      case "maint-on":  setMaintenance(unit, true); break;
      case "maint-off": setMaintenance(unit, false); break;
    }
  });

  el("addBtn").onclick = promptAddUnit;
  el("search").oninput = (e) => { state.query = e.target.value; render(); };
  el("zoneFilter").onchange = (e) => { state.filterZone = e.target.value; render(); };
  el("typeFilter").onchange = (e) => { state.filterType = e.target.value; savePrefs(); render(); };
  el("sortBy").onchange = (e) => { state.sort = e.target.value; savePrefs(); render(); };

  // Theme and filters are the only things kept locally. They are presentation, and
  // losing them costs nothing — unlike a cached rupee figure, which would be wrong.
  function savePrefs() {
    try {
      localStorage.setItem("playslot.prefs",
        JSON.stringify({
          theme: document.documentElement.dataset.theme,
          sort: state.sort,
          // Kept because someone running the tables upstairs sets it once a shift and
          // should not have to set it again after every refresh.
          filterType: state.filterType,
        }));
    } catch { /* private browsing; prefs simply do not persist */ }
  }

  function loadPrefs() {
    try {
      const saved = JSON.parse(localStorage.getItem("playslot.prefs") || "{}");
      if (saved.theme) document.documentElement.dataset.theme = saved.theme;
      if (saved.sort) { state.sort = saved.sort; el("sortBy").value = saved.sort; }
      // renderTypes() drops it back to "all" if the venue no longer has that kind.
      if (saved.filterType) state.filterType = saved.filterType;
    } catch { /* ignore */ }
  }

  el("themeBtn").onclick = () => {
    const root = document.documentElement;
    root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
    savePrefs();
  };

  // ---------------------------------------------------------------------- boot

  loadPrefs();

  api("/health")
    .then((health) => { el("footVenue").textContent = health.venue; })
    .catch(() => { el("footVenue").textContent = "—"; });

  refresh();
  connectAlerts();

  // The only interval in the file, and it fetches rather than counts. Every figure it
  // draws was computed by the engine; nothing here advances a clock of its own.
  setInterval(refresh, POLL_MS);
})();
