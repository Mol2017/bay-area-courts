// Frontend for the south bay area drop-in basketball calendar.
//
// Loads ../data/merged.json and renders a Mon–Sun weekly grid with:
//   • A two-week data window (current + next week) navigable via prev/today/
//     next buttons in the header.
//   • A scrollable 24-hour calendar grid that defaults to showing 8 AM.
//   • Per-source filtering (legend checkboxes), sorted by drop-in price.
//   • A Google-Calendar-style click popup with venue, time, address,
//     per-court breakdown, cost, and a source link.
//   • A red Refresh button that POSTs to /api/refresh (handled by
//     scripts/serve.py) to wipe data, re-scrape, re-merge, and re-render.
//     Falls back to a plain re-fetch on static hosts.
//
// All "today" math is done in Pacific time so the page is consistent for
// viewers in any timezone.

const HOUR_HEIGHT = 64;

// The grid renders the full 24-hour day so any session is reachable by
// scrolling. After the first render the viewport snaps to 8 AM at the top.
const START_HOUR = 0;
const END_HOUR = 24;
const DEFAULT_VIEW_START_HOUR = 8;

const PAD = 4;
const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTH_LABELS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

// Palette borrowed from the reference design. Unknown sources fall back to
// a hashed pastel HSL.
const SOURCE_COLORS = {
  red_morton_community_center: "#fce8b2", // recruiting yellow
  peninsula_community_center:  "#d2e3fc", // work blue
  arrillaga_family_gymnasium:  "#c7f0d8", // personal green
  newark_community_center:     "#f9dedc", // travel pink
  san_jose:                    "#eadcf8", // cross purple
};
const DEFAULT_COLOR = "#e8eaed";

// Per-source short name, city, and drop-in price ($/person). Used for the
// legend sort order and the legend label. Unknown sources fall back to the
// source slug, no city, and Infinity (sorted to the bottom of the list).
const SOURCE_INFO = {
  arrillaga_family_gymnasium:  { short: "AFG",  city: "Menlo Park",   price: 0  },
  red_morton_community_center: { short: "RMCC", city: "Redwood City", price: 5  },
  newark_community_center:     { short: "SC",   city: "Newark",       price: 14 },
  peninsula_community_center:  { short: "PCC",  city: "Redwood City", price: 55 },
};

// Sources whose checkbox starts unchecked on first page load. Anything not
// in this set is enabled by default.
const DEFAULT_OFF = new Set(["peninsula_community_center"]);

const enabledSources = new Set();
let currentData = null;
// ISO date (YYYY-MM-DD) of the Monday of the week currently displayed.
// Updated by prev/next/today buttons; defaults to current week's Monday on load.
let viewedWeekStart = null;

// ─── helpers ──────────────────────────────────────────────────────────────

function colorFor(source) {
  if (SOURCE_COLORS[source]) return SOURCE_COLORS[source];
  if (!source) return DEFAULT_COLOR;
  // Deterministic hash → pastel HSL for unknown sources.
  let h = 0;
  for (const c of source) h = (h * 31 + c.charCodeAt(0)) & 0xffff;
  return `hsl(${h % 360}, 70%, 85%)`;
}

function prettySource(source) {
  return source.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function sourceInfo(source) {
  return (
    SOURCE_INFO[source] || { short: prettySource(source), city: "", price: Infinity }
  );
}

function priceLabel(price) {
  return price === Infinity ? "?" : `$${price}/person`;
}

function parseISODateOnly(iso) {
  // "2026-04-06" → {y, m, d}
  const [y, m, d] = iso.split("-").map(Number);
  return { y, m, d };
}

function dayIndex(sessionStartISO, weekStartISO) {
  // sessionStart looks like "2026-04-06T10:00-07:00"; the date portion is
  // already in PT because the scraper writes it with a PT offset.
  const a = parseISODateOnly(sessionStartISO.slice(0, 10));
  const b = parseISODateOnly(weekStartISO);
  const aUTC = Date.UTC(a.y, a.m - 1, a.d);
  const bUTC = Date.UTC(b.y, b.m - 1, b.d);
  return Math.round((aUTC - bUTC) / 86400000);
}

function timePart(sessionISO) {
  // "2026-04-06T10:00-07:00" → "10:00"
  return sessionISO.slice(11, 16);
}

function timeToMinutes(hhmm) {
  const [hh, mm] = hhmm.split(":").map(Number);
  return hh * 60 + mm;
}

function minutesToTop(minutes) {
  return ((minutes - START_HOUR * 60) / 60) * HOUR_HEIGHT;
}

function fmtHourLabel(h24) {
  const suffix = h24 >= 12 ? "PM" : "AM";
  const h12 = h24 % 12 === 0 ? 12 : h24 % 12;
  return `${h12} ${suffix}`;
}

function fmtTimeMinutes(m) {
  const h = Math.floor(m / 60);
  const mm = m % 60;
  const suffix = h >= 12 ? "PM" : "AM";
  const h12 = h % 12 === 0 ? 12 : h % 12;
  return `${h12}:${String(mm).padStart(2, "0")} ${suffix}`;
}

function fmtTimeRange(startMin, endMin) {
  return `${fmtTimeMinutes(startMin)} – ${fmtTimeMinutes(endMin)}`;
}

function weekDates(weekStartISO) {
  const { y, m, d } = parseISODateOnly(weekStartISO);
  const startUTC = Date.UTC(y, m - 1, d);
  const out = [];
  for (let i = 0; i < 7; i++) {
    const dt = new Date(startUTC + i * 86400000);
    out.push({
      day: dt.getUTCDate(),
      month: dt.getUTCMonth(),
      year: dt.getUTCFullYear(),
      weekday: WEEKDAY_LABELS[i],
      iso:
        dt.getUTCFullYear() +
        "-" +
        String(dt.getUTCMonth() + 1).padStart(2, "0") +
        "-" +
        String(dt.getUTCDate()).padStart(2, "0"),
    });
  }
  return out;
}

function todayInPTISO() {
  // Use Intl to get today's date string in PT, regardless of viewer TZ.
  const fmt = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Los_Angeles",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  });
  return fmt.format(new Date()); // "YYYY-MM-DD"
}

function currentWeekMondayISO() {
  // Monday of the week containing today (PT).
  const today = todayInPTISO();
  const { y, m, d } = parseISODateOnly(today);
  // JS Date in UTC for math, then derive weekday.
  const utc = new Date(Date.UTC(y, m - 1, d));
  // getUTCDay: Sun=0, Mon=1, ... Sat=6 — convert to Monday-based offset.
  const monOffset = (utc.getUTCDay() + 6) % 7;
  const monday = new Date(utc.getTime() - monOffset * 86400000);
  return (
    monday.getUTCFullYear() +
    "-" +
    String(monday.getUTCMonth() + 1).padStart(2, "0") +
    "-" +
    String(monday.getUTCDate()).padStart(2, "0")
  );
}

function shiftWeekISO(weekStartISO, weekDelta) {
  const { y, m, d } = parseISODateOnly(weekStartISO);
  const t = Date.UTC(y, m - 1, d) + weekDelta * 7 * 86400000;
  const dt = new Date(t);
  return (
    dt.getUTCFullYear() +
    "-" +
    String(dt.getUTCMonth() + 1).padStart(2, "0") +
    "-" +
    String(dt.getUTCDate()).padStart(2, "0")
  );
}

// ─── overlap packing (sweep) ──────────────────────────────────────────────

function assignColumns(events) {
  events.sort(
    (a, b) => a.startMinutes - b.startMinutes || a.endMinutes - b.endMinutes
  );

  let active = [];
  let cluster = [];

  function finalize(clusterEvents) {
    if (!clusterEvents.length) return;
    const columns = [];
    for (const ev of clusterEvents) {
      let placed = false;
      for (let i = 0; i < columns.length; i++) {
        const last = columns[i][columns[i].length - 1];
        if (last.endMinutes <= ev.startMinutes) {
          columns[i].push(ev);
          ev.column = i;
          placed = true;
          break;
        }
      }
      if (!placed) {
        ev.column = columns.length;
        columns.push([ev]);
      }
    }
    const total = columns.length;
    clusterEvents.forEach((ev) => (ev.totalColumns = total));
  }

  for (const ev of events) {
    active = active.filter((a) => a.endMinutes > ev.startMinutes);
    if (active.length === 0) {
      finalize(cluster);
      cluster = [ev];
      active.push(ev);
    } else {
      cluster.push(ev);
      active.push(ev);
    }
  }
  finalize(cluster);
  return events;
}

// ─── render ───────────────────────────────────────────────────────────────

function buildTimeColumn() {
  const timeCol = document.getElementById("timeCol");
  timeCol.innerHTML = "";
  for (let h = START_HOUR; h < END_HOUR; h++) {
    const slot = document.createElement("div");
    slot.className = "time-slot";
    slot.textContent = fmtHourLabel(h);
    timeCol.appendChild(slot);
  }
}

function renderHeaderRow() {
  const dates = weekDates(viewedWeekStart);
  const today = todayInPTISO();
  const headerRow = document.getElementById("dayHeaderRow");
  headerRow.innerHTML = '<div class="blank"></div>';
  dates.forEach((d, idx) => {
    const div = document.createElement("div");
    div.className = "day-header" + (d.iso === today ? " active" : "");
    div.innerHTML = `<div class="weekday">${d.weekday}</div><div class="date">${d.day}</div>`;
    headerRow.appendChild(div);
    if (d.iso === today) {
      const col = document.querySelector(`.day-col[data-day="${idx}"]`);
      if (col) col.classList.add("active");
    }
  });

  const first = dates[0];
  const last = dates[6];
  let title;
  if (first.month === last.month) {
    title = `${MONTH_LABELS[first.month]} ${first.day} – ${last.day}, ${first.year}`;
  } else {
    title = `${MONTH_LABELS[first.month]} ${first.day} – ${MONTH_LABELS[last.month]} ${last.day}, ${first.year}`;
  }
  // Suffix with "(this week)" or "(next week)" relative to today's Monday.
  const todaysMon = currentWeekMondayISO();
  if (viewedWeekStart === todaysMon) title += " · this week";
  else if (viewedWeekStart === shiftWeekISO(todaysMon, 1)) title += " · next week";
  else if (viewedWeekStart === shiftWeekISO(todaysMon, -1)) title += " · last week";
  document.getElementById("weekTitle").textContent = title;

  // (Stats heading is just "Summary" — set in HTML, not updated here.)

  // Disable nav buttons that would step beyond the data window.
  const prev = document.getElementById("prevWeekBtn");
  const next = document.getElementById("nextWeekBtn");
  if (currentData) {
    const prevTarget = shiftWeekISO(viewedWeekStart, -1);
    const nextTarget = shiftWeekISO(viewedWeekStart, 1);
    // Allow if target Monday is still inside [window_start, window_end - 7].
    const ws = currentData.window_start || currentData.week_start;
    const we = currentData.window_end || currentData.week_end;
    if (prev) prev.disabled = ws ? prevTarget < ws : false;
    if (next) next.disabled = we ? nextTarget >= we : false;
  }
}

function clearDayColumns() {
  document.querySelectorAll(".day-col").forEach((col) => {
    col.innerHTML = "";
    col.classList.remove("active");
  });
}

function transformSessions(data) {
  // → grouped by day index 0..6 (Mon..Sun), filtered to the currently
  // viewed week (viewedWeekStart). Events outside the rendered hour range
  // are clipped to the bounds so they always sit inside the grid.
  const grouped = { 0: [], 1: [], 2: [], 3: [], 4: [], 5: [], 6: [] };
  for (const s of data.sessions) {
    if (!enabledSources.has(s.source)) continue;
    const day = dayIndex(s.start, viewedWeekStart);
    if (day < 0 || day > 6) continue;
    const startMin = Math.max(START_HOUR * 60, timeToMinutes(timePart(s.start)));
    const endMin = Math.min(END_HOUR * 60, timeToMinutes(timePart(s.end)));
    if (endMin <= startMin) continue;
    grouped[day].push({
      session: s,
      startMinutes: startMin,
      endMinutes: endMin,
    });
  }
  return grouped;
}

function scrollToDefaultView() {
  // Place 8 AM at the top of the .calendar-grid-wrap viewport on first
  // load, so the user lands on the same window as before but can still
  // scroll up for early-morning events or down for late-night ones.
  const wrap = document.querySelector(".calendar-grid-wrap");
  if (!wrap) return;
  wrap.scrollTop = (DEFAULT_VIEW_START_HOUR - START_HOUR) * HOUR_HEIGHT;
}

function renderEvents(data) {
  clearDayColumns();
  // Re-mark active day after clearing.
  const today = todayInPTISO();
  const dates = weekDates(viewedWeekStart);
  dates.forEach((d, idx) => {
    if (d.iso === today) {
      document
        .querySelector(`.day-col[data-day="${idx}"]`)
        ?.classList.add("active");
    }
  });

  const grouped = transformSessions(data);
  for (const [day, dayEvents] of Object.entries(grouped)) {
    const col = document.querySelector(`.day-col[data-day="${day}"]`);
    assignColumns(dayEvents);
    for (const ev of dayEvents) {
      const div = document.createElement("div");
      div.className = "event";
      const top = minutesToTop(ev.startMinutes);
      const height = Math.max(
        24,
        minutesToTop(ev.endMinutes) - minutesToTop(ev.startMinutes)
      );
      const widthPct = 100 / (ev.totalColumns || 1);
      const leftPct = ev.column * widthPct;

      div.style.top = `${top}px`;
      div.style.height = `${height}px`;
      div.style.left = `calc(${leftPct}% + ${PAD}px)`;
      div.style.width = `calc(${widthPct}% - ${PAD * 2}px)`;
      div.style.background = colorFor(ev.session.source);

      const title = ev.session.venue || "Open Gym";
      const meta = sourceInfo(ev.session.source).short;
      div.innerHTML = `
        <div class="event-title">${escapeHTML(title)}</div>
        <div class="event-time">${fmtTimeRange(ev.startMinutes, ev.endMinutes)}</div>
        <div class="event-meta">${escapeHTML(meta)}</div>
      `;
      div.addEventListener("click", (e) => showEventPopup(ev.session, e));
      col.appendChild(div);
    }
  }
}

function escapeHTML(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}

// ─── sidebar ──────────────────────────────────────────────────────────────

function renderLegend(data) {
  // Discover sources from the data, then sort cheapest → priciest.
  const sources = [...new Set(data.sessions.map((s) => s.source))];
  sources.sort((a, b) => sourceInfo(a).price - sourceInfo(b).price);

  // Initial enabled state: everything that isn't in DEFAULT_OFF.
  for (const src of sources) {
    if (!DEFAULT_OFF.has(src)) enabledSources.add(src);
  }

  const legend = document.getElementById("legend");
  legend.innerHTML = "";
  for (const src of sources) {
    const info = sourceInfo(src);
    const enabled = enabledSources.has(src);
    const row = document.createElement("label");
    row.className = "legend-item" + (enabled ? "" : " disabled");
    row.innerHTML = `
      <input type="checkbox" ${enabled ? "checked" : ""}>
      <span class="dot" style="background:${colorFor(src)}"></span>
      <span class="legend-label">
        <strong>${escapeHTML(info.short)}</strong>
        ${info.city ? `<span class="legend-city">${escapeHTML(info.city)}</span>` : ""}
        <span class="legend-price">${escapeHTML(priceLabel(info.price))}</span>
      </span>
    `;
    const cb = row.querySelector("input");
    cb.addEventListener("change", () => {
      if (cb.checked) {
        enabledSources.add(src);
        row.classList.remove("disabled");
      } else {
        enabledSources.delete(src);
        row.classList.add("disabled");
      }
      rerenderViewedWeek();
    });
    legend.appendChild(row);
  }
}

function renderStats(data) {
  // Only count sessions for the currently viewed week.
  const visible = data.sessions.filter(
    (s) =>
      enabledSources.has(s.source) &&
      (() => {
        const d = dayIndex(s.start, viewedWeekStart);
        return d >= 0 && d <= 6;
      })()
  );
  const venues = new Set(visible.map((s) => s.venue));
  const stats = document.getElementById("stats");
  stats.innerHTML = `
    <div><strong>${visible.length}</strong> sessions</div>
    <div><strong>${venues.size}</strong> venue${venues.size === 1 ? "" : "s"}</div>
    <div><strong>${enabledSources.size}</strong> source${enabledSources.size === 1 ? "" : "s"} on</div>
  `;
}

// ─── event popup (Google Calendar style) ──────────────────────────────────

function hideEventPopup() {
  document.getElementById("eventPopup").hidden = true;
  document.getElementById("popupBackdrop").hidden = true;
}

function showEventPopup(s, clickEvent) {
  const popup = document.getElementById("eventPopup");
  const backdrop = document.getElementById("popupBackdrop");

  // Color bar uses the source color so the popup feels tied to the event.
  document.getElementById("popupColorBar").style.background = colorFor(s.source);

  // Title = venue (gym name).
  document.getElementById("popupVenue").textContent = s.venue || "Open Gym";

  // Subtitle = activity (e.g. "Court Rental", "Drop-In Basketball").
  const activityEl = document.getElementById("popupActivity");
  if (s.activity) {
    activityEl.textContent = `${s.activity} · ${prettySource(s.source)}`;
    activityEl.style.display = "";
  } else {
    activityEl.style.display = "none";
  }

  // When: weekday + date + time range, in the viewed week.
  const startMin = timeToMinutes(timePart(s.start));
  const endMin = timeToMinutes(timePart(s.end));
  const day = dayIndex(s.start, viewedWeekStart);
  const date = weekDates(viewedWeekStart)[day];
  document.getElementById("popupWhen").textContent =
    `${date.weekday}, ${MONTH_LABELS[date.month]} ${date.day} · ${fmtTimeRange(startMin, endMin)}`;

  // Address (hide row if missing).
  const addrRow = document.getElementById("popupAddressRow");
  const addrEl = document.getElementById("popupAddress");
  if (s.address) {
    addrEl.textContent = s.address;
    addrRow.style.display = "";
  } else {
    addrRow.style.display = "none";
  }

  // Per-court breakdown (Court 1 + Court 2 windows). Only shown if the
  // session carries a structured `courts` field.
  const courtsRow = document.getElementById("popupCourtsRow");
  const courtsEl = document.getElementById("popupCourts");
  courtsEl.innerHTML = "";
  if (Array.isArray(s.courts) && s.courts.length) {
    for (const c of s.courts) {
      const line = document.createElement("div");
      line.className = "popup-court-line";
      line.innerHTML = `<strong>${escapeHTML(c.name)}:</strong> ${escapeHTML((c.windows || []).join(", "))}`;
      courtsEl.appendChild(line);
    }
    courtsRow.style.display = "";
  } else {
    courtsRow.style.display = "none";
  }

  // Cost.
  const costRow = document.getElementById("popupCostRow");
  const costEl = document.getElementById("popupCost");
  if (s.cost) {
    costEl.textContent = s.cost;
    costRow.style.display = "";
  } else {
    costRow.style.display = "none";
  }

  // Notes — only show if there's no structured courts breakdown (otherwise
  // the notes are redundant text duplication of what we already display).
  const notesRow = document.getElementById("popupNotesRow");
  const notesEl = document.getElementById("popupNotes");
  if (s.notes && !(Array.isArray(s.courts) && s.courts.length)) {
    notesEl.textContent = s.notes;
    notesRow.style.display = "";
  } else {
    notesRow.style.display = "none";
  }

  // Source link.
  const link = document.getElementById("popupLink");
  if (s.source_event_url) {
    link.href = s.source_event_url;
    link.style.display = "";
  } else {
    link.style.display = "none";
  }

  // Show + position. We make it visible first so we can measure its size.
  backdrop.hidden = false;
  popup.hidden = false;
  popup.style.left = "0px";
  popup.style.top = "0px";
  const rect = popup.getBoundingClientRect();
  const margin = 12;

  // Anchor the popup to the clicked element if available, else to the click
  // coordinates. Snap into the viewport if needed.
  let x, y;
  if (clickEvent && clickEvent.currentTarget && clickEvent.currentTarget.getBoundingClientRect) {
    const eb = clickEvent.currentTarget.getBoundingClientRect();
    // Prefer placing the popup to the right of the event.
    x = eb.right + 8;
    y = eb.top;
    if (x + rect.width > window.innerWidth - margin) {
      // Not enough room on right — try left.
      x = eb.left - rect.width - 8;
    }
    if (x < margin) {
      // Still doesn't fit — center horizontally.
      x = Math.max(margin, (window.innerWidth - rect.width) / 2);
    }
  } else if (clickEvent) {
    x = clickEvent.clientX + 10;
    y = clickEvent.clientY + 10;
  } else {
    x = (window.innerWidth - rect.width) / 2;
    y = (window.innerHeight - rect.height) / 2;
  }
  // Final viewport snapping.
  if (y + rect.height > window.innerHeight - margin) {
    y = window.innerHeight - rect.height - margin;
  }
  if (y < margin) y = margin;
  if (x + rect.width > window.innerWidth - margin) {
    x = window.innerWidth - rect.width - margin;
  }
  if (x < margin) x = margin;

  popup.style.left = `${x}px`;
  popup.style.top = `${y}px`;
}

// ─── load + refresh ───────────────────────────────────────────────────────

function setStatus(text) {
  document.getElementById("updatedAt").textContent = text;
}

function rerenderViewedWeek() {
  if (!currentData) return;
  renderHeaderRow();
  renderStats(currentData);
  renderEvents(currentData);
}

function setViewedWeek(iso) {
  viewedWeekStart = iso;
  rerenderViewedWeek();
}

async function loadMergedJSON() {
  const res = await fetch("../data/merged.json", { cache: "no-store" });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  currentData = data;
  enabledSources.clear();
  // Default the viewed week to the current week's Monday on first load.
  // Subsequent refreshes preserve whatever week the user was viewing,
  // unless that week is outside the new data window.
  const todaysMon = currentWeekMondayISO();
  const ws = data.window_start || data.week_start;
  const we = data.window_end || data.week_end;
  if (
    !viewedWeekStart ||
    (ws && viewedWeekStart < ws) ||
    (we && shiftWeekISO(viewedWeekStart, 1) > we)
  ) {
    viewedWeekStart = todaysMon;
  }
  renderLegend(data);
  rerenderViewedWeek();
  setStatus(`Updated ${new Date(data.generated_at).toLocaleString()}`);
}

// Initial load on page open: just read merged.json from disk.
async function initialLoad() {
  const btn = document.getElementById("refreshBtn");
  btn.classList.add("loading");
  try {
    await loadMergedJSON();
    scrollToDefaultView();
  } catch (e) {
    setStatus(`Load failed: ${e.message}`);
  } finally {
    btn.classList.remove("loading");
  }
}

// ─── refresh: local /api/refresh + GitHub Actions fallback ───────────────
//
// The Refresh button has two implementations and picks one at runtime:
//
//   1. Local dev (scripts/serve.py running)
//      → POST /api/refresh, which wipes data, re-scrapes, re-merges, and
//        returns a JSON status. Fast path, no token needed.
//
//   2. GitHub Pages (no backend)
//      → POST to GitHub's REST API to dispatch the
//        .github/workflows/refresh.yml workflow. We poll the workflow run
//        until it completes, then reload merged.json (the workflow commits
//        a fresh data/merged.json which Pages serves on its next deploy).
//        Requires a fine-grained Personal Access Token with Actions write
//        access on this repo only. The token is stored in localStorage
//        the first time the user clicks Refresh.

const GH_TOKEN_KEY = "gh_pat";
// How long the workflow_dispatch path waits before giving up. The cron
// usually finishes in ~3 minutes; Pages then deploys in another minute.
const WORKFLOW_TIMEOUT_MS = 10 * 60 * 1000;
const POLL_INTERVAL_MS = 5000;

function detectGitHubRepo() {
  // Explicit override in config.js wins.
  const cfg = window.SITE_CONFIG || {};
  if (cfg.repoOwner && cfg.repoName) {
    return { owner: cfg.repoOwner, repo: cfg.repoName };
  }
  // Auto-detect from <user>.github.io/<repo>/web/...
  const host = location.hostname;
  if (host.endsWith(".github.io")) {
    const owner = host.replace(/\.github\.io$/, "");
    const seg = location.pathname.split("/").filter(Boolean);
    if (seg.length >= 1) return { owner, repo: seg[0] };
  }
  return null;
}

function ghHeaders(token) {
  return {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "X-GitHub-Api-Version": "2022-11-28",
  };
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

function promptForToken() {
  return prompt(
    "Refresh needs a GitHub Personal Access Token to trigger the workflow.\n\n" +
      "Create a fine-grained PAT at github.com/settings/tokens?type=beta\n" +
      "  • Resource owner: your account\n" +
      "  • Repository access: only this repo\n" +
      "  • Repository permissions: Actions = Read and write\n\n" +
      "The token is stored in this browser's localStorage and never sent " +
      "anywhere except api.github.com.\n\nPaste the token:"
  );
}

async function getStoredToken() {
  let token = localStorage.getItem(GH_TOKEN_KEY);
  if (!token) {
    token = promptForToken();
    if (token) localStorage.setItem(GH_TOKEN_KEY, token);
  }
  return token;
}

function clearStoredToken() {
  localStorage.removeItem(GH_TOKEN_KEY);
}

async function refresh() {
  const btn = document.getElementById("refreshBtn");
  if (btn.classList.contains("loading")) return;
  btn.classList.add("loading");
  setStatus("Refreshing…");
  try {
    // 1. Try the local serve.py /api/refresh first.
    const localResult = await tryLocalRefresh();
    if (localResult.handled) return;

    // 2. Fall back to GitHub Actions workflow_dispatch.
    await refreshViaGitHubActions();
  } catch (e) {
    setStatus(`Refresh failed: ${e.message}`);
  } finally {
    btn.classList.remove("loading");
  }
}

async function tryLocalRefresh() {
  // { handled: true } means we did the work (success OR a known soft state
  // like 409 busy). { handled: false } means there's no local backend and
  // the caller should fall through to the GitHub Actions path.
  let res;
  try {
    res = await fetch("/api/refresh", { method: "POST" });
  } catch (_networkErr) {
    return { handled: false };
  }

  // Static hosts have no /api/refresh endpoint at all and respond with one
  // of these statuses (depending on the host):
  //   • 404 — most servers (no such file)
  //   • 405 — GitHub Pages (POST not allowed on a read-only static host)
  //   • 501 — some CDNs (POST method "not implemented")
  // In all of those cases we fall through to the GitHub Actions API path.
  if (res.status === 404 || res.status === 405 || res.status === 501) {
    return { handled: false };
  }
  if (res.status === 409) {
    setStatus("A refresh is already running — try again shortly");
    return { handled: true };
  }
  const body = await res.json().catch(() => ({}));
  if (!res.ok || body.status !== "ok") {
    throw new Error(body.message || `HTTP ${res.status}`);
  }
  await loadMergedJSON();
  setStatus(
    `Refreshed in ${body.duration_s}s · ${body.sessions} sessions from ${(body.sources || []).length} sources`
  );
  return { handled: true };
}

async function refreshViaGitHubActions() {
  const repo = detectGitHubRepo();
  if (!repo) {
    throw new Error(
      "Can't detect GitHub repo — set repoOwner/repoName in web/config.js"
    );
  }
  const cfg = window.SITE_CONFIG || {};
  const workflow = cfg.workflowFile || "refresh.yml";
  const ref = cfg.workflowRef || "main";

  let token = await getStoredToken();
  if (!token) {
    setStatus("Refresh canceled (no token).");
    return;
  }

  // 1. Snapshot the most recent existing workflow run id so we can detect
  //    the *new* run we're about to trigger.
  setStatus("Looking up workflow…");
  let beforeRunId = null;
  try {
    beforeRunId = await fetchLatestRunId(repo, workflow, token);
  } catch (e) {
    if (String(e.message).includes("401") || String(e.message).includes("403")) {
      clearStoredToken();
      throw new Error("GitHub rejected the token (401/403). Click Refresh again to enter a new one.");
    }
    if (String(e.message).includes("404")) {
      throw new Error(`Workflow ${workflow} not found in ${repo.owner}/${repo.repo}`);
    }
    throw e;
  }

  // 2. Trigger workflow_dispatch.
  setStatus("Triggering GitHub Actions workflow…");
  const dispatchUrl = `https://api.github.com/repos/${repo.owner}/${repo.repo}/actions/workflows/${workflow}/dispatches`;
  const dispatchRes = await fetch(dispatchUrl, {
    method: "POST",
    headers: { ...ghHeaders(token), "Content-Type": "application/json" },
    body: JSON.stringify({ ref }),
  });
  if (dispatchRes.status === 401 || dispatchRes.status === 403) {
    clearStoredToken();
    throw new Error("GitHub rejected the token. Click Refresh again to enter a new one.");
  }
  if (!dispatchRes.ok && dispatchRes.status !== 204) {
    const text = await dispatchRes.text().catch(() => "");
    throw new Error(`workflow_dispatch HTTP ${dispatchRes.status} ${text}`);
  }

  // 3. Poll until a *new* run shows up (run_id != beforeRunId).
  setStatus("Waiting for workflow to start…");
  const newRunId = await waitForNewRun(repo, workflow, token, beforeRunId);

  // 4. Poll the run until it leaves "queued" / "in_progress".
  const runUrl = `https://api.github.com/repos/${repo.owner}/${repo.repo}/actions/runs/${newRunId}`;
  const start = Date.now();
  while (Date.now() - start < WORKFLOW_TIMEOUT_MS) {
    await sleep(POLL_INTERVAL_MS);
    const runRes = await fetch(runUrl, { headers: ghHeaders(token) });
    if (!runRes.ok) throw new Error(`run poll HTTP ${runRes.status}`);
    const run = await runRes.json();
    const elapsed = Math.round((Date.now() - start) / 1000);
    setStatus(
      `Workflow ${run.status}${run.conclusion ? " · " + run.conclusion : ""} · ${elapsed}s · run #${run.run_number}`
    );
    if (run.status === "completed") {
      if (run.conclusion !== "success") {
        throw new Error(
          `workflow ${run.conclusion} (see ${run.html_url})`
        );
      }
      // 5. Pages deploy lags the commit by ~30s. Wait, then reload.
      setStatus("Workflow done — waiting for Pages to publish…");
      await sleep(30000);
      await loadMergedJSON();
      setStatus(
        `Refreshed via GitHub Actions · run #${run.run_number}`
      );
      return;
    }
  }
  throw new Error("workflow timed out — check the Actions tab");
}

async function fetchLatestRunId(repo, workflow, token) {
  const url = `https://api.github.com/repos/${repo.owner}/${repo.repo}/actions/workflows/${workflow}/runs?per_page=1`;
  const res = await fetch(url, { headers: ghHeaders(token) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const body = await res.json();
  return body.workflow_runs && body.workflow_runs.length
    ? body.workflow_runs[0].id
    : null;
}

async function waitForNewRun(repo, workflow, token, beforeRunId) {
  // workflow_dispatch can take 1–10 s before the run is queued.
  const url = `https://api.github.com/repos/${repo.owner}/${repo.repo}/actions/workflows/${workflow}/runs?per_page=5`;
  for (let i = 0; i < 30; i++) {
    await sleep(2000);
    const res = await fetch(url, { headers: ghHeaders(token) });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const body = await res.json();
    const runs = body.workflow_runs || [];
    const fresh = runs.find((r) => r.id !== beforeRunId);
    if (fresh) return fresh.id;
  }
  throw new Error("no new workflow run appeared after dispatch");
}

function init() {
  buildTimeColumn();
  document.getElementById("refreshBtn").addEventListener("click", refresh);
  document.getElementById("popupClose").addEventListener("click", hideEventPopup);
  document.getElementById("popupBackdrop").addEventListener("click", hideEventPopup);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") hideEventPopup();
  });
  document.getElementById("prevWeekBtn").addEventListener("click", () => {
    if (!viewedWeekStart) return;
    setViewedWeek(shiftWeekISO(viewedWeekStart, -1));
  });
  document.getElementById("nextWeekBtn").addEventListener("click", () => {
    if (!viewedWeekStart) return;
    setViewedWeek(shiftWeekISO(viewedWeekStart, 1));
  });
  document.getElementById("todayBtn").addEventListener("click", () => {
    setViewedWeek(currentWeekMondayISO());
  });
  initialLoad();
}

init();
