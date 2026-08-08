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

  /** States where a customer is mid-session and the unit cannot be sold. */
  const OCCUPIED = new Set(["active", "warning", "overtime", "locked"]);

  /** The two a manager must not miss from across the room. */
  const URGENT = new Set(["overtime", "locked"]);

  const state = {
    units: [],
    sales: null,
    linkDown: false,
    filterState: "all",
    filterZone: "all",
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
      const [units, sales] = await Promise.all([
        api("/units"),
        api("/sales/today"),
      ]);

      state.units = units;
      state.sales = sales;

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

  function visibleUnits() {
    const query = state.query.trim().toLowerCase();

    let rows = state.units.filter((unit) => {
      if (state.filterState !== "all" && unit.state !== state.filterState) return false;
      if (state.filterZone !== "all" && (unit.zone || "") !== state.filterZone) return false;

      if (!query) return true;

      return [unit.name, unit.zone, unit.customer_ref, unit.type]
        .filter(Boolean)
        .some((field) => String(field).toLowerCase().includes(query));
    });

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

  function renderDistribution() {
    const counts = {};
    state.units.forEach((u) => { counts[u.state] = (counts[u.state] || 0) + 1; });

    const total = state.units.length || 1;

    el("distBar").innerHTML = Object.keys(STATES)
      .filter((key) => counts[key])
      .map((key) =>
        `<span class="dist__seg u-${key}" style="width:${(counts[key] / total) * 100}%;background:var(--st)" title="${esc(STATES[key].label)}: ${counts[key]}"></span>`)
      .join("");

    el("legend").innerHTML = Object.keys(STATES)
      .filter((key) => counts[key])
      .map((key) =>
        `<span class="legend__item u-${key}"><i class="dot" style="background:var(--st)"></i>${esc(STATES[key].label)} ${counts[key]}</span>`)
      .join("");
  }

  function renderChips() {
    const counts = { all: state.units.length };
    state.units.forEach((u) => { counts[u.state] = (counts[u.state] || 0) + 1; });

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
    const select = el("zoneFilter");
    const zones = [...new Set(state.units.map((u) => u.zone).filter(Boolean))].sort();
    const current = state.filterZone;

    select.innerHTML =
      `<option value="all">All zones</option>` +
      zones.map((z) => `<option value="${esc(z)}">${esc(z)}</option>`).join("");

    select.value = zones.includes(current) ? current : "all";
    state.filterZone = select.value;
  }

  function renderCard(unit) {
    const meta = STATES[unit.state] || { label: unit.state, icon: "i-available" };
    const busy = state.busy.has(unit.id);
    const urgent = URGENT.has(unit.state);
    const occupied = OCCUPIED.has(unit.state);

    let body = "";

    if (occupied) {
      // Straight from the server. The browser never works out how long is left.
      const remaining = unit.remaining_seconds;
      const over = remaining !== null && remaining <= 0;

      // "open / remaining" reads as nonsense on an open-ended walk-in, and the manager
      // needs to know at a glance that this one has no deadline to run out.
      const timeLabel = remaining === null
        ? "no deadline · billed by time used"
        : over
          ? (unit.state === "locked" ? "grace used up" : "over by")
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
        <div class="card__sub">Idle · ${esc(unit.type.toUpperCase())}</div>
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
          <span class="badge">
            <svg width="14" height="14" aria-hidden="true"><use href="#${meta.icon}"/></svg>
            ${esc(meta.label)}
          </span>
        </div>
        ${body}
      </article>`;
  }

  function render() {
    el("linkbar").dataset.down = state.linkDown ? "1" : "0";

    renderKpis();
    renderDistribution();
    renderChips();
    renderZones();

    const rows = visibleUnits();

    el("grid").innerHTML = rows.map(renderCard).join("");
    el("emptyState").hidden = rows.length > 0;

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

  function promptEnd(unit) {
    openModal(`Close ${unit.name}`, `
      <p class="modal__lead">Billing <b>${esc(unit.running_total || "—")}</b> for this session.</p>
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
    });
  }

  function promptAddUnit() {
    openModal("Add a unit", `
      <label class="field"><span>Name</span><input id="m-name" type="text" placeholder="Nova" /></label>
      <label class="field"><span>Type</span>
        <select id="m-type"><option value="pc">PC</option><option value="ps5">PS5</option><option value="sim">Sim rig</option></select></label>
      <label class="field"><span>Zone</span><input id="m-zone" type="text" placeholder="Battle Zone" /></label>
      <div class="modal__actions">
        <button class="btn" type="button" data-close>Cancel</button>
        <button class="btn btn--primary" id="m-go" type="button">Add</button>
      </div>`, () => {
      el("m-go").onclick = async () => {
        const payload = {
          name: el("m-name").value.trim(),
          type: el("m-type").value,
          zone: el("m-zone").value.trim(),
        };

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
  el("sortBy").onchange = (e) => { state.sort = e.target.value; savePrefs(); render(); };

  // Theme and filters are the only things kept locally. They are presentation, and
  // losing them costs nothing — unlike a cached rupee figure, which would be wrong.
  function savePrefs() {
    try {
      localStorage.setItem("playslot.prefs",
        JSON.stringify({ theme: document.documentElement.dataset.theme, sort: state.sort }));
    } catch { /* private browsing; prefs simply do not persist */ }
  }

  function loadPrefs() {
    try {
      const saved = JSON.parse(localStorage.getItem("playslot.prefs") || "{}");
      if (saved.theme) document.documentElement.dataset.theme = saved.theme;
      if (saved.sort) { state.sort = saved.sort; el("sortBy").value = saved.sort; }
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

  // The only interval in the file, and it fetches rather than counts. Every figure it
  // draws was computed by the engine; nothing here advances a clock of its own.
  setInterval(refresh, POLL_MS);
})();
