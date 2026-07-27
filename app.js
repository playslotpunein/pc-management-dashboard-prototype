/* ============================================================
   Unit Management Dashboard — prototype logic
   No dependencies, no build step. State persists in localStorage.
   ============================================================ */
(function () {
  "use strict";

  var STORE_KEY = "umd.state.v2";
  var THEME_KEY = "umd.theme";

  var STATUS = {
    available:   { label: "Available",   icon: "i-available" },
    "in-use":    { label: "In use",      icon: "i-in-use" },
    maintenance: { label: "Maintenance", icon: "i-maintenance" },
    offline:     { label: "Offline",     icon: "i-offline" }
  };
  var STATUS_ORDER = ["available", "in-use", "maintenance", "offline"];

  // ---------- Seed data ----------
  function seed() {
    var now = Date.now();
    var mins = function (m) { return new Date(now - m * 60000).toISOString(); };
    var units = [
      { id: "PC-01", name: "Nova",     zone: "Battle Zone",  status: "in-use",      cpu: "Ryzen 7 7800X3D", gpu: "RTX 4070", ram: "32GB", storage: "1TB NVMe", rate: 120, note: "", session: { customer: "Rohan M.",   startedAt: mins(84) } },
      { id: "PC-02", name: "Pulse",    zone: "Battle Zone",  status: "in-use",      cpu: "Ryzen 7 7800X3D", gpu: "RTX 4070", ram: "32GB", storage: "1TB NVMe", rate: 120, note: "", session: { customer: "Aditi S.",   startedAt: mins(37) } },
      { id: "PC-03", name: "Vertex",   zone: "Battle Zone",  status: "available",   cpu: "Ryzen 7 7800X3D", gpu: "RTX 4070", ram: "32GB", storage: "1TB NVMe", rate: 120, note: "", session: null },
      { id: "PC-04", name: "Blaze",    zone: "Battle Zone",  status: "available",   cpu: "Ryzen 7 7800X3D", gpu: "RTX 4070", ram: "32GB", storage: "1TB NVMe", rate: 120, note: "", session: null },
      { id: "PC-05", name: "Apex",     zone: "Pro Arena",    status: "in-use",      cpu: "Core i9-14900K",  gpu: "RTX 4090", ram: "64GB", storage: "2TB NVMe", rate: 220, note: "Tournament rig", session: { customer: "Team Vortex", startedAt: mins(152) } },
      { id: "PC-06", name: "Titan",    zone: "Pro Arena",    status: "in-use",      cpu: "Core i9-14900K",  gpu: "RTX 4090", ram: "64GB", storage: "2TB NVMe", rate: 220, note: "Tournament rig", session: { customer: "Team Vortex", startedAt: mins(152) } },
      { id: "PC-07", name: "Onyx",     zone: "Pro Arena",    status: "maintenance", cpu: "Core i9-14900K",  gpu: "RTX 4090", ram: "64GB", storage: "2TB NVMe", rate: 220, note: "GPU thermal paste redo", session: null },
      { id: "PC-08", name: "Comet",    zone: "Casual Bay",   status: "available",   cpu: "Ryzen 5 7600",    gpu: "RTX 4060", ram: "16GB", storage: "1TB NVMe", rate: 80,  note: "", session: null },
      { id: "PC-09", name: "Drift",    zone: "Casual Bay",   status: "in-use",      cpu: "Ryzen 5 7600",    gpu: "RTX 4060", ram: "16GB", storage: "1TB NVMe", rate: 80,  note: "", session: { customer: "Kabir P.",   startedAt: mins(19) } },
      { id: "PC-10", name: "Echo",     zone: "Casual Bay",   status: "available",   cpu: "Ryzen 5 7600",    gpu: "RTX 4060", ram: "16GB", storage: "1TB NVMe", rate: 80,  note: "", session: null },
      { id: "PC-11", name: "Halo",     zone: "Casual Bay",   status: "offline",     cpu: "Ryzen 5 7600",    gpu: "RTX 4060", ram: "16GB", storage: "1TB NVMe", rate: 80,  note: "PSU failure — awaiting part", session: null },
      { id: "PC-12", name: "Rift",     zone: "VR Room",      status: "in-use",      cpu: "Core i7-14700K",  gpu: "RTX 4080", ram: "32GB", storage: "2TB NVMe", rate: 180, note: "Meta Quest 3 tethered", session: { customer: "Sana K.",    startedAt: mins(48) } },
      { id: "PC-13", name: "Mirage",   zone: "VR Room",      status: "available",   cpu: "Core i7-14700K",  gpu: "RTX 4080", ram: "32GB", storage: "2TB NVMe", rate: 180, note: "", session: null },
      { id: "PC-14", name: "Stream",   zone: "Creator Deck", status: "available",   cpu: "Core i9-14900K",  gpu: "RTX 4080", ram: "64GB", storage: "4TB NVMe", rate: 200, note: "Elgato + dual monitor", session: null }
    ];
    var log = [
      logRow("PC-03", "Vertex", "Ishan R.", 95, 190),
      logRow("PC-08", "Comet",  "Neha T.",  60, 80),
      logRow("PC-01", "Nova",   "Arjun D.", 130, 260),
      logRow("PC-12", "Rift",   "Meera J.", 45, 135),
      logRow("PC-09", "Drift",  "Farhan A.", 75, 100)
    ];
    return { units: units, log: log };
  }
  function logRow(unitId, unitName, customer, minutes, amount) {
    return {
      unitId: unitId, unitName: unitName, customer: customer,
      start: new Date(Date.now() - minutes * 60000).toISOString(),
      end: new Date().toISOString(),
      minutes: minutes, amount: amount
    };
  }

  // ---------- State ----------
  var state = load();
  var filters = { status: "all", zone: "all", search: "", sort: "id" };

  function load() {
    try {
      var raw = localStorage.getItem(STORE_KEY);
      if (raw) {
        var p = JSON.parse(raw);
        if (p && p.units) return p;
      }
    } catch (e) { /* ignore */ }
    return seed();
  }
  function save() {
    try { localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (e) { /* ignore */ }
  }

  // ---------- Helpers ----------
  function $(sel, root) { return (root || document).querySelector(sel); }
  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function money(n) { return "₹" + Math.round(n).toLocaleString("en-IN"); }
  function pad(n) { return n < 10 ? "0" + n : "" + n; }
  function durationMs(ms) {
    var s = Math.max(0, Math.floor(ms / 1000));
    var h = Math.floor(s / 3600); s -= h * 3600;
    var m = Math.floor(s / 60); s -= m * 60;
    return pad(h) + ":" + pad(m) + ":" + pad(s);
  }
  function elapsedMs(unit) {
    if (!unit.session) return 0;
    return Date.now() - new Date(unit.session.startedAt).getTime();
  }
  function liveCost(unit) {
    if (!unit.session) return 0;
    return unit.rate * (elapsedMs(unit) / 3600000);
  }
  function isToday(iso) {
    var d = new Date(iso), n = new Date();
    return d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
  }
  function icon(id, cls) {
    return '<svg class="' + (cls || "") + '" width="16" height="16" aria-hidden="true"><use href="#' + id + '"/></svg>';
  }
  function toast(msg) {
    var t = $("#toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(function () { t.hidden = true; }, 2600);
  }

  // ---------- Derived ----------
  function counts() {
    var c = { available: 0, "in-use": 0, maintenance: 0, offline: 0 };
    state.units.forEach(function (u) { c[u.status] = (c[u.status] || 0) + 1; });
    return c;
  }
  function zones() {
    var z = {};
    state.units.forEach(function (u) { z[u.zone] = true; });
    return Object.keys(z).sort();
  }
  function revenueToday() {
    var total = 0, n = 0;
    state.log.forEach(function (r) { if (isToday(r.end)) { total += r.amount; n++; } });
    return { total: total, count: n };
  }

  // ---------- Rendering ----------
  function render() {
    renderKpis();
    renderDistribution();
    renderChips();
    renderZoneFilter();
    renderGrid();
    save();
  }

  function renderKpis() {
    var c = counts();
    var total = state.units.length;
    var usable = total - c.maintenance - c.offline;
    var occ = usable > 0 ? Math.round((c["in-use"] / usable) * 100) : 0;
    $("#kpiOccupancy").textContent = occ + "%";
    $("#kpiOccupancySub").textContent = c["in-use"] + " of " + usable + " usable units in session";
    $("#kpiOccupancyMeter").style.width = occ + "%";
    $("#kpiTotal").textContent = total;
    $("#kpiZones").textContent = zones().length + " zones";
    $("#kpiInUse").textContent = c["in-use"];
    $("#kpiAvailable").textContent = c.available;
    $("#kpiDown").textContent = c.maintenance + c.offline;
    var rev = revenueToday();
    $("#kpiRevenue").textContent = money(rev.total);
    $("#kpiSessions").textContent = rev.count + " sessions closed";
  }

  function renderDistribution() {
    var c = counts();
    var total = state.units.length || 1;
    var bar = $("#distBar");
    var legend = $("#legend");
    bar.innerHTML = "";
    legend.innerHTML = "";
    STATUS_ORDER.forEach(function (st) {
      var n = c[st] || 0;
      if (n > 0) {
        var seg = document.createElement("div");
        seg.className = "dist__seg dist__seg--" + st;
        seg.style.flexBasis = (n / total * 100) + "%";
        seg.style.flexGrow = n;
        seg.title = STATUS[st].label + ": " + n;
        bar.appendChild(seg);
      }
      var li = document.createElement("span");
      li.className = "legend__item";
      li.innerHTML = '<i class="dot dot--' + st + '"></i>' + STATUS[st].label + " <b>" + n + "</b>";
      legend.appendChild(li);
    });
  }

  function renderChips() {
    var c = counts();
    var host = $("#statusChips");
    host.innerHTML = "";
    var defs = [{ key: "all", label: "All", count: state.units.length }];
    STATUS_ORDER.forEach(function (st) { defs.push({ key: st, label: STATUS[st].label, count: c[st] || 0 }); });
    defs.forEach(function (d) {
      var b = document.createElement("button");
      b.className = "chip";
      b.type = "button";
      b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", filters.status === d.key ? "true" : "false");
      var dot = d.key === "all" ? "" : '<i class="dot dot--' + d.key + '"></i>';
      b.innerHTML = dot + esc(d.label) + ' <span class="chip__count">' + d.count + "</span>";
      b.addEventListener("click", function () { filters.status = d.key; render(); });
      host.appendChild(b);
    });
  }

  function renderZoneFilter() {
    var sel = $("#zoneFilter");
    var current = filters.zone;
    var zs = zones();
    sel.innerHTML = '<option value="all">All zones</option>' +
      zs.map(function (z) { return '<option value="' + esc(z) + '">' + esc(z) + "</option>"; }).join("");
    sel.value = zs.indexOf(current) >= 0 ? current : "all";
  }

  function visibleUnits() {
    var q = filters.search.trim().toLowerCase();
    var list = state.units.filter(function (u) {
      if (filters.status !== "all" && u.status !== filters.status) return false;
      if (filters.zone !== "all" && u.zone !== filters.zone) return false;
      if (q) {
        var hay = (u.id + " " + u.name + " " + u.zone + " " + (u.session ? u.session.customer : "")).toLowerCase();
        if (hay.indexOf(q) < 0) return false;
      }
      return true;
    });
    var by = filters.sort;
    list.sort(function (a, b) {
      if (by === "status") {
        var d = STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status);
        return d || a.id.localeCompare(b.id);
      }
      if (by === "zone") return a.zone.localeCompare(b.zone) || a.id.localeCompare(b.id);
      if (by === "session") return elapsedMs(b) - elapsedMs(a) || a.id.localeCompare(b.id);
      return a.id.localeCompare(b.id, undefined, { numeric: true });
    });
    return list;
  }

  function renderGrid() {
    var grid = $("#grid");
    var list = visibleUnits();
    grid.innerHTML = "";
    $("#emptyState").hidden = list.length !== 0;
    list.forEach(function (u) { grid.appendChild(card(u)); });
    startTicker();
  }

  function card(u) {
    var el = document.createElement("article");
    el.className = "card card--" + u.status;
    el.tabIndex = 0;
    el.setAttribute("role", "button");
    el.setAttribute("aria-label", u.id + " " + u.name + ", " + STATUS[u.status].label);

    var badge = '<span class="badge badge--' + u.status + '">' + icon(STATUS[u.status].icon) + STATUS[u.status].label + "</span>";

    var body;
    if (u.status === "in-use" && u.session) {
      body =
        '<div class="session">' +
          '<div class="session__row">' + icon("i-user") + '<span class="session__cust">' + esc(u.session.customer) + "</span></div>" +
          '<div class="session__row">' + icon("i-clock") +
            '<span class="session__timer" data-started="' + esc(u.session.startedAt) + '">' + durationMs(elapsedMs(u)) + "</span>" +
            '<span class="session__cost" data-started="' + esc(u.session.startedAt) + '" data-rate="' + u.rate + '">' + money(liveCost(u)) + "</span>" +
          "</div>" +
        "</div>";
    } else if (u.status === "maintenance" || u.status === "offline") {
      body = '<div class="session session--empty">' + esc(u.note || (u.status === "offline" ? "Unit offline" : "Under maintenance")) + "</div>";
    } else {
      body = '<div class="session session--empty">Idle · ₹' + u.rate + "/hr</div>";
    }

    var actions = actionButtons(u);

    el.innerHTML =
      '<div class="card__top">' +
        "<div>" +
          '<div class="card__id">' + esc(u.id) + "</div>" +
          '<div class="card__name">' + esc(u.name) + "</div>" +
          '<div class="card__zone">' + icon("i-pin") + esc(u.zone) + "</div>" +
        "</div>" + badge +
      "</div>" +
      body +
      '<div class="specs">' +
        '<span class="spec">' + icon("i-chip") + " " + esc(u.gpu) + "</span>" +
        '<span class="spec">' + esc(u.cpu) + "</span>" +
        '<span class="spec">' + esc(u.ram) + "</span>" +
      "</div>" +
      '<div class="card__actions">' + actions + "</div>";

    // open detail on card click / keyboard
    el.addEventListener("click", function () { openDetail(u.id); });
    el.addEventListener("keydown", function (e) {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openDetail(u.id); }
    });
    // wire action buttons (stop propagation so they don't open detail)
    Array.prototype.forEach.call(el.querySelectorAll("[data-act]"), function (btn) {
      btn.addEventListener("click", function (e) {
        e.stopPropagation();
        handleAction(btn.getAttribute("data-act"), u.id);
      });
    });
    return el;
  }

  function actionButtons(u) {
    if (u.status === "available") {
      return '<button class="btn btn--primary btn--sm" data-act="start">' + icon("i-in-use") + "Start session</button>" +
             '<button class="btn btn--sm" data-act="maintenance">Maintenance</button>';
    }
    if (u.status === "in-use") {
      return '<button class="btn btn--danger btn--sm" data-act="end">End session</button>' +
             '<button class="btn btn--sm" data-act="detail">Details</button>';
    }
    if (u.status === "maintenance") {
      return '<button class="btn btn--primary btn--sm" data-act="free">Mark available</button>' +
             '<button class="btn btn--sm" data-act="offline">Offline</button>';
    }
    // offline
    return '<button class="btn btn--primary btn--sm" data-act="free">Bring online</button>' +
           '<button class="btn btn--sm" data-act="maintenance">Maintenance</button>';
  }

  // ---------- Actions ----------
  function unitById(id) {
    for (var i = 0; i < state.units.length; i++) if (state.units[i].id === id) return state.units[i];
    return null;
  }

  function handleAction(act, id) {
    var u = unitById(id);
    if (!u) return;
    if (act === "start") return openStartSession(u);
    if (act === "end") return endSession(u);
    if (act === "detail") return openDetail(id);
    if (act === "maintenance") { setStatus(u, "maintenance"); toast(u.id + " set to maintenance"); }
    if (act === "offline") { setStatus(u, "offline"); toast(u.id + " marked offline"); }
    if (act === "free") { setStatus(u, "available"); toast(u.id + " is now available"); }
  }

  function setStatus(u, status) {
    if (u.session && status !== "in-use") u.session = null;
    u.status = status;
    render();
  }

  function endSession(u) {
    if (!u.session) return;
    var start = new Date(u.session.startedAt).getTime();
    var end = Date.now();
    var minutes = Math.max(1, Math.round((end - start) / 60000));
    var amount = Math.round(u.rate * (end - start) / 3600000);
    state.log.unshift({
      unitId: u.id, unitName: u.name, customer: u.session.customer,
      start: u.session.startedAt, end: new Date(end).toISOString(),
      minutes: minutes, amount: amount
    });
    u.session = null;
    u.status = "available";
    render();
    toast("Session closed · " + money(amount) + " · " + minutes + " min");
  }

  // ---------- Modals ----------
  var modalHost = $("#modalHost");
  var modalBody = $("#modalBody");
  var modalTitle = $("#modalTitle");

  function openModal(title, html) {
    modalTitle.textContent = title;
    modalBody.innerHTML = html;
    modalHost.hidden = false;
    document.body.style.overflow = "hidden";
    var first = modalBody.querySelector("input, select, textarea, button");
    if (first) setTimeout(function () { first.focus(); }, 30);
  }
  function closeModal() {
    modalHost.hidden = true;
    modalBody.innerHTML = "";
    document.body.style.overflow = "";
  }
  modalHost.addEventListener("click", function (e) {
    if (e.target.hasAttribute("data-close")) closeModal();
  });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape" && !modalHost.hidden) closeModal();
  });

  function openStartSession(u) {
    openModal("Start session · " + u.id,
      '<div class="field">' +
        "<label for=\"f-cust\">Customer / party name</label>" +
        '<input id="f-cust" type="text" placeholder="e.g. Rohan M." />' +
      "</div>" +
      '<div class="field">' +
        "<label for=\"f-rate\">Rate (₹ / hour)</label>" +
        '<input id="f-rate" type="number" min="0" step="10" value="' + u.rate + '" />' +
      "</div>" +
      '<p class="hint">' + esc(u.name) + " · " + esc(u.zone) + " · " + esc(u.gpu) + "</p>" +
      '<div class="modal__actions">' +
        '<button class="btn btn--ghost" data-close type="button">Cancel</button>' +
        '<button class="btn btn--primary" id="f-go" type="button">' + icon("i-in-use") + "Start</button>" +
      "</div>");
    $("#f-go").addEventListener("click", function () {
      var name = $("#f-cust").value.trim() || "Walk-in";
      var rate = parseInt($("#f-rate").value, 10);
      if (!isNaN(rate) && rate >= 0) u.rate = rate;
      u.session = { customer: name, startedAt: new Date().toISOString() };
      u.status = "in-use";
      closeModal();
      render();
      toast("Session started on " + u.id);
    });
    var cust = $("#f-cust");
    cust.addEventListener("keydown", function (e) { if (e.key === "Enter") $("#f-go").click(); });
  }

  function openDetail(id) {
    var u = unitById(id);
    if (!u) return;
    var hist = state.log.filter(function (r) { return r.unitId === u.id; }).slice(0, 6);
    var histHtml = hist.length ?
      '<div class="history">' + hist.map(function (r) {
        return '<div class="history__item">' + icon("i-user") +
          "<div><b>" + esc(r.customer) + "</b><div class=\"hint\">" + r.minutes + " min · " + timeAgo(r.end) + "</div></div>" +
          '<span class="amt">' + money(r.amount) + "</span></div>";
      }).join("") + "</div>" :
      '<p class="history__empty">No completed sessions yet.</p>';

    var live = "";
    if (u.session) {
      live =
        '<div class="detail__cell"><span>Customer</span><b>' + esc(u.session.customer) + "</b></div>" +
        '<div class="detail__cell"><span>Elapsed</span><b class="session__timer" data-started="' + esc(u.session.startedAt) + '">' + durationMs(elapsedMs(u)) + "</b></div>" +
        '<div class="detail__cell"><span>Running total</span><b class="session__cost" data-started="' + esc(u.session.startedAt) + '" data-rate="' + u.rate + '">' + money(liveCost(u)) + "</b></div>" +
        '<div class="detail__cell"><span>Started</span><b>' + fmtTime(u.session.startedAt) + "</b></div>";
    }

    openModal(u.id + " · " + u.name,
      '<div class="detail__head">' +
        '<span class="badge badge--' + u.status + '">' + icon(STATUS[u.status].icon) + STATUS[u.status].label + "</span>" +
        '<div style="display:flex;gap:8px">' +
          '<button class="btn btn--sm" id="d-edit" type="button">Edit</button>' +
          '<button class="btn btn--sm btn--danger" id="d-del" type="button">' + icon("i-trash") + "</button>" +
        "</div>" +
      "</div>" +
      (live ? '<div class="detail__grid">' + live + "</div>" : "") +
      '<div class="detail__grid">' +
        '<div class="detail__cell"><span>Zone</span><b>' + esc(u.zone) + "</b></div>" +
        '<div class="detail__cell"><span>Rate</span><b>₹' + u.rate + "/hr</b></div>" +
        '<div class="detail__cell"><span>CPU</span><b>' + esc(u.cpu) + "</b></div>" +
        '<div class="detail__cell"><span>GPU</span><b>' + esc(u.gpu) + "</b></div>" +
        '<div class="detail__cell"><span>Memory</span><b>' + esc(u.ram) + "</b></div>" +
        '<div class="detail__cell"><span>Storage</span><b>' + esc(u.storage) + "</b></div>" +
      "</div>" +
      (u.note ? '<div class="detail__cell" style="margin-bottom:16px"><span>Note</span><b style="font-weight:500">' + esc(u.note) + "</b></div>" : "") +
      '<div class="detail__actions" style="display:flex;gap:8px;flex-wrap:wrap">' + quickActions(u) + "</div>" +
      '<div class="detail__section-title">Recent sessions</div>' + histHtml);

    // wire quick actions
    Array.prototype.forEach.call(modalBody.querySelectorAll("[data-act]"), function (btn) {
      btn.addEventListener("click", function () {
        var act = btn.getAttribute("data-act");
        if (act === "start") { closeModal(); openStartSession(u); return; }
        handleAction(act, u.id);
        if (act === "end" || act === "free" || act === "maintenance" || act === "offline") closeModal();
      });
    });
    $("#d-edit").addEventListener("click", function () { openEdit(u); });
    $("#d-del").addEventListener("click", function () {
      if (confirm("Delete " + u.id + " (" + u.name + ")? This cannot be undone.")) {
        state.units = state.units.filter(function (x) { return x.id !== u.id; });
        closeModal(); render(); toast(u.id + " deleted");
      }
    });
  }

  function quickActions(u) {
    if (u.status === "available") return '<button class="btn btn--primary btn--sm" data-act="start">Start session</button><button class="btn btn--sm" data-act="maintenance">Maintenance</button>';
    if (u.status === "in-use")    return '<button class="btn btn--danger btn--sm" data-act="end">End session</button>';
    if (u.status === "maintenance") return '<button class="btn btn--primary btn--sm" data-act="free">Mark available</button><button class="btn btn--sm" data-act="offline">Offline</button>';
    return '<button class="btn btn--primary btn--sm" data-act="free">Bring online</button><button class="btn btn--sm" data-act="maintenance">Maintenance</button>';
  }

  function openEdit(u) { unitForm(u); }
  function openAdd() { unitForm(null); }

  function unitForm(u) {
    var isEdit = !!u;
    var d = u || { id: nextId(), name: "", zone: zones()[0] || "Battle Zone", cpu: "", gpu: "", ram: "16GB", storage: "1TB NVMe", rate: 100, note: "" };
    openModal(isEdit ? "Edit " + d.id : "Add unit",
      '<div class="field__row">' +
        '<div class="field"><label for="u-id">Unit ID</label><input id="u-id" value="' + esc(d.id) + '" ' + (isEdit ? "disabled" : "") + ' /></div>' +
        '<div class="field"><label for="u-name">Name</label><input id="u-name" value="' + esc(d.name) + '" placeholder="e.g. Nova" /></div>' +
      "</div>" +
      '<div class="field__row">' +
        '<div class="field"><label for="u-zone">Zone</label><input id="u-zone" value="' + esc(d.zone) + '" placeholder="e.g. Battle Zone" /></div>' +
        '<div class="field"><label for="u-rate">Rate (₹/hr)</label><input id="u-rate" type="number" min="0" step="10" value="' + d.rate + '" /></div>' +
      "</div>" +
      '<div class="field__row">' +
        '<div class="field"><label for="u-cpu">CPU</label><input id="u-cpu" value="' + esc(d.cpu) + '" placeholder="e.g. Ryzen 5 7600" /></div>' +
        '<div class="field"><label for="u-gpu">GPU</label><input id="u-gpu" value="' + esc(d.gpu) + '" placeholder="e.g. RTX 4060" /></div>' +
      "</div>" +
      '<div class="field__row">' +
        '<div class="field"><label for="u-ram">Memory</label><input id="u-ram" value="' + esc(d.ram) + '" /></div>' +
        '<div class="field"><label for="u-storage">Storage</label><input id="u-storage" value="' + esc(d.storage) + '" /></div>' +
      "</div>" +
      '<div class="field"><label for="u-note">Note</label><textarea id="u-note" placeholder="Optional">' + esc(d.note) + "</textarea></div>" +
      '<div class="modal__actions">' +
        '<button class="btn btn--ghost" data-close type="button">Cancel</button>' +
        '<button class="btn btn--primary" id="u-save" type="button">' + (isEdit ? "Save changes" : "Add unit") + "</button>" +
      "</div>");

    $("#u-save").addEventListener("click", function () {
      var id = isEdit ? d.id : $("#u-id").value.trim().toUpperCase();
      if (!id) { toast("Unit ID is required"); return; }
      if (!isEdit && unitById(id)) { toast("Unit ID already exists"); return; }
      var payload = {
        name: $("#u-name").value.trim() || id,
        zone: $("#u-zone").value.trim() || "Unzoned",
        cpu: $("#u-cpu").value.trim() || "—",
        gpu: $("#u-gpu").value.trim() || "—",
        ram: $("#u-ram").value.trim() || "—",
        storage: $("#u-storage").value.trim() || "—",
        rate: Math.max(0, parseInt($("#u-rate").value, 10) || 0),
        note: $("#u-note").value.trim()
      };
      if (isEdit) {
        for (var k in payload) u[k] = payload[k];
        toast(id + " updated");
      } else {
        state.units.push(Object.assign({ id: id, status: "available", session: null }, payload));
        toast(id + " added");
      }
      closeModal();
      render();
    });
  }

  function nextId() {
    var max = 0;
    state.units.forEach(function (u) {
      var m = /(\d+)/.exec(u.id);
      if (m) max = Math.max(max, parseInt(m[1], 10));
    });
    return "PC-" + pad(max + 1);
  }

  // ---------- Time formatting ----------
  function fmtTime(iso) {
    var d = new Date(iso);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }
  function timeAgo(iso) {
    var s = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return "just now";
    var m = Math.floor(s / 60); if (m < 60) return m + "m ago";
    var h = Math.floor(m / 60); if (h < 24) return h + "h ago";
    return Math.floor(h / 24) + "d ago";
  }

  // ---------- Live ticker ----------
  function startTicker() {
    if (startTicker._on) return;
    startTicker._on = true;
    setInterval(function () {
      var els = document.querySelectorAll("[data-started]");
      Array.prototype.forEach.call(els, function (el) {
        var start = new Date(el.getAttribute("data-started")).getTime();
        var ms = Date.now() - start;
        if (el.classList.contains("session__timer")) {
          el.textContent = durationMs(ms);
        } else if (el.classList.contains("session__cost")) {
          var rate = parseFloat(el.getAttribute("data-rate")) || 0;
          el.textContent = money(rate * (ms / 3600000));
        }
      });
    }, 1000);
  }

  // ---------- Theme ----------
  function applyTheme(t) {
    if (t === "dark" || t === "light") document.documentElement.setAttribute("data-theme", t);
    else document.documentElement.removeAttribute("data-theme");
  }
  function currentTheme() {
    var t = document.documentElement.getAttribute("data-theme");
    if (t) return t;
    return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  (function initTheme() {
    var saved = localStorage.getItem(THEME_KEY);
    if (saved) applyTheme(saved);
  })();

  // ---------- Wire top-level controls ----------
  $("#addBtn").addEventListener("click", openAdd);
  $("#themeBtn").addEventListener("click", function () {
    var next = currentTheme() === "dark" ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
  });
  $("#search").addEventListener("input", function (e) { filters.search = e.target.value; renderGrid(); });
  $("#zoneFilter").addEventListener("change", function (e) { filters.zone = e.target.value; renderGrid(); });
  $("#sortBy").addEventListener("change", function (e) { filters.sort = e.target.value; renderGrid(); });
  $("#seedBtn").addEventListener("click", function () {
    if (confirm("Reset all units and session history to the demo data?")) {
      state = seed();
      filters = { status: "all", zone: "all", search: "", sort: "id" };
      $("#search").value = "";
      $("#sortBy").value = "id";
      render();
      toast("Demo data reset");
    }
  });

  $("#footStamp").textContent = "Prototype · " + new Date().toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });

  // ---------- Go ----------
  render();
})();
