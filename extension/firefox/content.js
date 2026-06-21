(function () {
  const HOOK_SOURCE = "itamx-elal-award-hook";
  const PANEL_ID = "itamx-elal-award-panel";
  const SAVER_BUCKETS = {
    "COACH:E": "coach",
    "PREMIUM_COACH:A": "premium",
    "BUSINESS:X": "business"
  };
  const CLASS_ORDER = { coach: 0, premium: 1, business: 2 };
  const CABIN_ORDER = { COACH: 0, PREMIUM_COACH: 1, BUSINESS: 2 };

  const state = {
    rows: [],
    raw: [],
    errors: [],
    request: null,
    status: "Waiting for EL AL award search",
    collapsed: false,
    filters: {
      saverOnly: true,
      coach: true,
      premium: true,
      business: true,
      embed: true,
      minSeats: 1,
      maxPoints: "",
      query: ""
    }
  };

  function runtimeUrl(path) {
    const api = globalThis.browser || globalThis.chrome;
    return api.runtime.getURL(path);
  }

  function injectHook() {
    const script = document.createElement("script");
    script.src = runtimeUrl("page-hook.js");
    script.async = false;
    script.onload = () => script.remove();
    (document.documentElement || document.head).appendChild(script);
  }

  function secondsToMinutes(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) {
      return null;
    }
    return Math.round(numeric / 60);
  }

  function cabinName(fare) {
    const value = String(fare.bookingClassName || fare.cabinTypeName || "").toLowerCase();
    if (value.includes("business")) {
      return "BUSINESS";
    }
    if (value.includes("premium")) {
      return "PREMIUM_COACH";
    }
    if (value.includes("economy")) {
      return "COACH";
    }
    return value ? value.toUpperCase() : null;
  }

  function normalizeSegment(segment) {
    const carrier = segment.carrier || (segment.airline && segment.airline.name) || null;
    const number = segment.flightNumber == null ? null : String(segment.flightNumber);
    return {
      carrier,
      flight_number: number,
      flight: carrier && number ? `${carrier}${number}` : null,
      origin: segment.departureAirport && segment.departureAirport.code,
      destination: segment.arrivalAirport && segment.arrivalAirport.code,
      departure: segment.departureDate,
      arrival: segment.arrivalDate,
      duration_minutes: secondsToMinutes(segment.duration),
      aircraft: segment.aircraftType || null
    };
  }

  function saverClass(row) {
    return SAVER_BUCKETS[`${String(row.cabin || "").toUpperCase()}:${String(row.rbd || "").toUpperCase()}`] || null;
  }

  function normalizeDirection(payload, direction) {
    const trip = payload && payload.data && payload.data.trip;
    const branch = trip && (direction === "inbound" ? trip.returnBound : trip.outbound);
    if (!branch) {
      return [];
    }

    const rows = [];
    for (const boundType of ["directBounds", "indirectBounds", "railAndFlyBounds"]) {
      const bounds = branch[boundType] && Array.isArray(branch[boundType].bounds)
        ? branch[boundType].bounds
        : [];
      for (const bound of bounds) {
        const segments = Array.isArray(bound.segments)
          ? bound.segments.map(normalizeSegment)
          : [];
        const flights = segments.map((segment) => segment.flight).filter(Boolean);
        const fares = Array.isArray(bound.fares) ? bound.fares : [];

        for (const fare of fares) {
          const netPrice = fare.netPrice || {};
          const points = netPrice.points || {};
          const taxes = points.taxes || {};
          const cash = netPrice.cash || {};
          const row = {
            direction,
            bound_type: boundType.replace("Bounds", ""),
            bound_id: bound.id,
            fare_id: fare.idOffer,
            flights,
            origin: segments[0] && segments[0].origin,
            destination: segments.length ? segments[segments.length - 1].destination : null,
            departure: segments[0] && segments[0].departure,
            arrival: segments.length ? segments[segments.length - 1].arrival : null,
            duration_minutes: secondsToMinutes(bound.duration),
            segments,
            cabin: cabinName(fare),
            rbd: fare.rbd,
            fare_family: fare.familyName || fare.name,
            points: points.amount,
            min_points: points.minAmountToConsume,
            tax_amount: taxes.amount,
            tax_currency: taxes.currencyCode,
            cash_amount: cash.amount,
            cash_currency: cash.currencyCode,
            seats_left: fare.nbSeatLeft,
            recommended: Boolean(fare.recommended),
            best_value: Boolean(fare.bestValue)
          };
          row.award_class = saverClass(row);
          rows.push(row);
        }
      }
    }
    return rows;
  }

  function dateFromRow(row) {
    if (row.departure) {
      return row.departure.slice(0, 10);
    }
    const match = String(row.bound_id || "").match(/^(\d{4})(\d{2})(\d{2})_/);
    return match ? `${match[1]}-${match[2]}-${match[3]}` : "";
  }

  function formatNumber(value) {
    const numeric = Number(value);
    return Number.isFinite(numeric) ? numeric.toLocaleString("en-US") : "-";
  }

  function formatCompactPoints(value) {
    const numeric = Number(value);
    if (!Number.isFinite(numeric)) {
      return "-";
    }
    if (numeric >= 1000) {
      return `${(numeric / 1000).toFixed(numeric % 1000 === 0 ? 0 : 1)}k`;
    }
    return String(numeric);
  }

  function formatMoney(amount, currency) {
    const numeric = Number(amount);
    if (!Number.isFinite(numeric)) {
      return "-";
    }
    return `${currency || ""}${numeric.toFixed(2)}`;
  }

  function formatTime(value) {
    if (!value) {
      return "-";
    }
    return String(value).slice(11, 16) || String(value);
  }

  function aircraft(row) {
    return [...new Set((row.segments || []).map((segment) => segment.aircraft).filter(Boolean))].join(",");
  }

  function cabinLabel(row) {
    if (row.award_class) {
      return row.award_class;
    }
    if (row.cabin === "PREMIUM_COACH") {
      return "premium";
    }
    if (row.cabin === "BUSINESS") {
      return "business";
    }
    if (row.cabin === "COACH") {
      return "coach";
    }
    return row.cabin || "fare";
  }

  function fareSort(a, b) {
    const cabinCompare = (CABIN_ORDER[a.cabin] ?? 9) - (CABIN_ORDER[b.cabin] ?? 9);
    if (cabinCompare) return cabinCompare;
    const saverCompare = (a.award_class ? 0 : 1) - (b.award_class ? 0 : 1);
    if (saverCompare) return saverCompare;
    const pointsCompare = Number(a.points || Infinity) - Number(b.points || Infinity);
    if (pointsCompare) return pointsCompare;
    return String(a.rbd || "").localeCompare(String(b.rbd || ""));
  }

  function rowText(row) {
    return [
      row.award_class,
      row.cabin,
      row.rbd,
      row.fare_family,
      row.flights && row.flights.join(" "),
      row.origin,
      row.destination,
      aircraft(row)
    ]
      .join(" ")
      .toLowerCase();
  }

  function filteredRows() {
    const query = state.filters.query.trim().toLowerCase();
    const maxPoints = Number(state.filters.maxPoints);
    const hasMaxPoints = Number.isFinite(maxPoints) && maxPoints > 0;
    const minSeats = Number(state.filters.minSeats) || 0;

    return state.rows
      .filter((row) => {
        const awardClass = row.award_class;
        if (state.filters.saverOnly && !awardClass) {
          return false;
        }
        if (awardClass && !state.filters[awardClass]) {
          return false;
        }
        if (!awardClass && !state.filters.saverOnly) {
          const cabin = String(row.cabin || "").toUpperCase();
          if (cabin === "COACH" && !state.filters.coach) return false;
          if (cabin === "PREMIUM_COACH" && !state.filters.premium) return false;
          if (cabin === "BUSINESS" && !state.filters.business) return false;
        }
        if (minSeats > 0 && Number(row.seats_left || 0) < minSeats) {
          return false;
        }
        if (hasMaxPoints && Number(row.points || Infinity) > maxPoints) {
          return false;
        }
        if (query && !rowText(row).includes(query)) {
          return false;
        }
        return true;
      })
      .sort((a, b) => {
        const dateCompare = dateFromRow(a).localeCompare(dateFromRow(b));
        if (dateCompare) return dateCompare;
        const classCompare = (CLASS_ORDER[a.award_class] ?? 9) - (CLASS_ORDER[b.award_class] ?? 9);
        if (classCompare) return classCompare;
        return String(a.departure || "").localeCompare(String(b.departure || ""));
      });
  }

  function ensurePanel() {
    let panel = document.getElementById(PANEL_ID);
    if (panel) {
      return panel;
    }

    panel = document.createElement("aside");
    panel.id = PANEL_ID;
    panel.innerHTML = `
      <div class="itamx-head">
        <div>
          <strong>itamx awards</strong>
          <span class="itamx-status"></span>
        </div>
        <div class="itamx-actions">
          <button type="button" data-action="export">CSV</button>
          <button type="button" data-action="toggle">Hide</button>
        </div>
      </div>
      <div class="itamx-body">
        <div class="itamx-filters">
          <label><input type="checkbox" data-filter="saverOnly" checked> Points award</label>
          <label><input type="checkbox" data-filter="coach" checked> E</label>
          <label><input type="checkbox" data-filter="premium" checked> A</label>
          <label><input type="checkbox" data-filter="business" checked> X</label>
          <label><input type="checkbox" data-filter="embed" checked> Embed</label>
          <label>Seats <input type="number" min="0" step="1" data-filter="minSeats" value="1"></label>
          <label>Max pts <input type="number" min="0" step="1000" data-filter="maxPoints"></label>
          <input type="search" data-filter="query" placeholder="flight, RBD, airport, aircraft">
        </div>
        <div class="itamx-summary"></div>
        <div class="itamx-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>Class</th>
                <th>RBD</th>
                <th>Flight</th>
                <th>Route</th>
                <th>Time</th>
                <th>Points</th>
                <th>Tax</th>
                <th>Cash</th>
                <th>Seats</th>
                <th>AC</th>
                <th>Family</th>
              </tr>
            </thead>
            <tbody></tbody>
          </table>
        </div>
        <pre class="itamx-detail"></pre>
      </div>
    `;

    panel.addEventListener("change", onFilterChange);
    panel.addEventListener("input", onFilterChange);
    panel.addEventListener("click", onPanelClick);
    document.documentElement.appendChild(panel);
    return panel;
  }

  function onFilterChange(event) {
    const input = event.target.closest("[data-filter]");
    if (!input) {
      return;
    }
    const key = input.dataset.filter;
    state.filters[key] = input.type === "checkbox" ? input.checked : input.value;
    render();
  }

  function onPanelClick(event) {
    const action = event.target.closest("[data-action]");
    if (action && action.dataset.action === "toggle") {
      state.collapsed = !state.collapsed;
      render();
      return;
    }
    if (action && action.dataset.action === "export") {
      exportCsv(filteredRows());
      return;
    }

    const rowButton = event.target.closest("[data-row]");
    if (rowButton) {
      const index = Number(rowButton.dataset.row);
      const row = filteredRows()[index];
      const detail = ensurePanel().querySelector(".itamx-detail");
      detail.textContent = JSON.stringify(row || {}, null, 2);
    }
  }

  function csvCell(value) {
    const text = value == null ? "" : String(value);
    return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
  }

  function exportCsv(rows) {
    const headers = [
      "date",
      "class",
      "rbd",
      "flight",
      "origin",
      "destination",
      "departure",
      "arrival",
      "points",
      "tax",
      "cash",
      "seats_left",
      "aircraft",
      "fare_family"
    ];
    const lines = [headers.join(",")];
    for (const row of rows) {
      lines.push(
        [
          dateFromRow(row),
          row.award_class || row.cabin,
          row.rbd,
          row.flights && row.flights.join(" "),
          row.origin,
          row.destination,
          row.departure,
          row.arrival,
          row.points,
          formatMoney(row.tax_amount, row.tax_currency),
          formatMoney(row.cash_amount, row.cash_currency),
          row.seats_left,
          aircraft(row),
          row.fare_family
        ]
          .map(csvCell)
          .join(",")
      );
    }
    const blob = new Blob([lines.join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = "elal-awards.csv";
    anchor.click();
    URL.revokeObjectURL(url);
  }

  function isExtensionNode(element) {
    return Boolean(element.closest && element.closest(`#${PANEL_ID}, .itamx-inline-card, .itamx-fare-chip`));
  }

  function isVisibleElement(element) {
    if (!(element instanceof Element) || isExtensionNode(element)) {
      return false;
    }
    const style = getComputedStyle(element);
    if (style.display === "none" || style.visibility === "hidden" || style.opacity === "0") {
      return false;
    }
    const rect = element.getBoundingClientRect();
    return rect.width >= 40 && rect.height >= 20;
  }

  function uniqueFlights() {
    return [
      ...new Set(
        state.rows.flatMap((row) => (row.flights || []).filter((flight) => /^LY\d+$/i.test(flight)))
      )
    ];
  }

  function rowsForFlight(flight) {
    return state.rows
      .filter((row) => (row.flights || []).includes(flight))
      .sort(fareSort);
  }

  function pointsText(row) {
    return formatNumber(row.points);
  }

  function bestRow(rows, predicate) {
    const matches = rows.filter(predicate);
    if (!matches.length) {
      return null;
    }
    return matches.sort(fareSort)[0];
  }

  function saverMetric(rows, cabin, rbd) {
    const row = bestRow(
      rows,
      (candidate) => candidate.cabin === cabin && String(candidate.rbd || "").toUpperCase() === rbd
    );
    if (!row) {
      return `${rbd}-`;
    }
    return `${rbd}${row.seats_left ?? "-"} ${formatCompactPoints(row.points)}`;
  }

  function bestCabinMetric(rows, cabin, label) {
    const row = bestRow(rows, (candidate) => candidate.cabin === cabin);
    if (!row) {
      return `${label}-`;
    }
    return `${label} ${row.rbd || "?"}${row.seats_left ?? "-"} ${formatCompactPoints(row.points)}`;
  }

  function findFlightCard(flight, rows) {
    const elements = [...document.querySelectorAll("section, article, li, div")];
    const candidates = [];
    for (const element of elements) {
      if (!isVisibleElement(element)) {
        continue;
      }
      const text = element.textContent || "";
      if (!text.includes(flight)) {
        continue;
      }
      const rect = element.getBoundingClientRect();
      if (rect.width < 320 || rect.height < 80 || rect.height > 900 || text.length > 6000) {
        continue;
      }
      const hasPoints = rows.some((row) => text.includes(pointsText(row)));
      candidates.push({
        element,
        hasPoints,
        area: rect.width * rect.height,
        textLength: text.length
      });
    }
    candidates.sort((a, b) => {
      if (a.hasPoints !== b.hasPoints) {
        return a.hasPoints ? -1 : 1;
      }
      return a.area - b.area || a.textLength - b.textLength;
    });
    return candidates[0] && candidates[0].element;
  }

  function clearEmbeddedDetails() {
    document.querySelectorAll(".itamx-inline-card, .itamx-fare-chip").forEach((node) => node.remove());
    document.querySelectorAll(".itamx-enhanced-host").forEach((node) => {
      node.classList.remove("itamx-enhanced-host");
    });
  }

  function buildInlineCard(rows) {
    const box = document.createElement("details");
    box.className = "itamx-inline-card";
    box.dir = "ltr";

    const head = document.createElement("summary");
    head.className = "itamx-inline-summary";
    const saverCount = rows.filter((row) => row.award_class).length;

    const title = document.createElement("span");
    title.className = "itamx-inline-brand";
    title.textContent = "itamx";
    head.appendChild(title);

    const saver = document.createElement("span");
    saver.className = "itamx-inline-savers";
    saver.textContent = [
      saverMetric(rows, "COACH", "E"),
      saverMetric(rows, "PREMIUM_COACH", "A"),
      saverMetric(rows, "BUSINESS", "X")
    ].join("  ");
    head.appendChild(saver);

    const best = document.createElement("span");
    best.className = "itamx-inline-best";
    best.textContent = [
      bestCabinMetric(rows, "COACH", "Y"),
      bestCabinMetric(rows, "PREMIUM_COACH", "W"),
      bestCabinMetric(rows, "BUSINESS", "J")
    ].join("  ");
    head.appendChild(best);

    const count = document.createElement("span");
    count.className = "itamx-inline-count";
    count.textContent = `${rows.length} fares · ${saverCount} points award`;
    head.appendChild(count);

    box.appendChild(head);

    const list = document.createElement("div");
    list.className = "itamx-inline-list";
    for (const row of rows) {
      const item = document.createElement("span");
      item.className = `itamx-inline-pill ${row.award_class ? `itamx-${row.award_class}` : "itamx-nonsaver"}`;
      item.textContent = [
        `${row.rbd || "?"} ${cabinLabel(row)}`,
        `${formatCompactPoints(row.points)} pts`,
        `${row.seats_left ?? "-"} seats`,
        row.fare_family || null
      ]
        .filter(Boolean)
        .join(" · ");
      item.title = [
        `${formatNumber(row.points)} points`,
        `${formatMoney(row.tax_amount, row.tax_currency)} tax`,
        `${formatMoney(row.cash_amount, row.cash_currency)} cash`,
        `${row.seats_left ?? "-"} seats`,
        row.fare_family || ""
      ]
        .filter(Boolean)
        .join("\n");
      list.appendChild(item);
    }
    box.appendChild(list);
    return box;
  }

  function findFareTarget(card, row) {
    const point = pointsText(row);
    const candidates = [];
    for (const element of card.querySelectorAll("div, span, button, td")) {
      if (!isVisibleElement(element)) {
        continue;
      }
      const text = element.textContent || "";
      if (!text.includes(point)) {
        continue;
      }
      const rect = element.getBoundingClientRect();
      if (rect.width < 80 || rect.height < 30 || text.length > 800) {
        continue;
      }
      candidates.push({
        element,
        area: rect.width * rect.height,
        textLength: text.length
      });
    }
    candidates.sort((a, b) => a.area - b.area || a.textLength - b.textLength);
    return candidates[0] && candidates[0].element;
  }

  function annotateFareCells(card, rows) {
    for (const row of rows) {
      const target = findFareTarget(card, row);
      if (!target) {
        continue;
      }
      const chip = document.createElement("span");
      chip.className = `itamx-fare-chip ${row.award_class ? `itamx-${row.award_class}` : "itamx-nonsaver"}`;
      chip.dir = "ltr";
      chip.textContent = `${row.rbd || "?"} · ${row.seats_left ?? "-"} seats${row.award_class ? " · points award" : ""}`;
      chip.title = [
        `RBD ${row.rbd || "?"}`,
        row.award_class ? `${row.award_class} points award` : `${cabinLabel(row)} other award`,
        `${formatNumber(row.points)} points`,
        `${formatMoney(row.tax_amount, row.tax_currency)} tax`,
        `${formatMoney(row.cash_amount, row.cash_currency)} cash`,
        `${row.seats_left ?? "-"} seats`,
        row.fare_family || ""
      ]
        .filter(Boolean)
        .join("\n");
      target.appendChild(chip);
    }
  }

  let embedTimer = null;
  function scheduleEmbeddedDetails() {
    if (embedTimer) {
      clearTimeout(embedTimer);
    }
    embedTimer = setTimeout(() => {
      embedTimer = null;
      embedDetails();
    }, 150);
  }

  function embedDetails() {
    clearEmbeddedDetails();
    if (!state.filters.embed || !state.rows.length) {
      return;
    }

    const cardRows = new Map();
    for (const flight of uniqueFlights()) {
      const rows = rowsForFlight(flight);
      const card = findFlightCard(flight, rows);
      if (!card) {
        continue;
      }
      const existing = cardRows.get(card) || [];
      cardRows.set(card, existing.concat(rows));
    }

    for (const [card, rows] of cardRows) {
      const uniqueRows = [...new Map(rows.map((row) => [`${row.direction}:${row.bound_id}:${row.fare_id}`, row])).values()].sort(fareSort);
      card.classList.add("itamx-enhanced-host");
      annotateFareCells(card, uniqueRows);
      card.appendChild(buildInlineCard(uniqueRows));
    }
  }

  function render() {
    const panel = ensurePanel();
    panel.classList.toggle("itamx-collapsed", state.collapsed);
    panel.querySelector("[data-action='toggle']").textContent = state.collapsed ? "Show" : "Hide";

    const rows = filteredRows();
    const saverCounts = rows.reduce(
      (acc, row) => {
        if (row.award_class) {
          acc[row.award_class] = (acc[row.award_class] || 0) + 1;
        }
        return acc;
      },
      { coach: 0, premium: 0, business: 0 }
    );

    panel.querySelector(".itamx-status").textContent = state.status;
    panel.querySelector(".itamx-summary").textContent =
      `${rows.length}/${state.rows.length} rows | E ${saverCounts.coach || 0} | A ${saverCounts.premium || 0} | X ${saverCounts.business || 0}` +
      (state.errors.length ? ` | ${state.errors.length} errors` : "");

    const tbody = panel.querySelector("tbody");
    tbody.textContent = "";
    rows.slice(0, 250).forEach((row, index) => {
      const tr = document.createElement("tr");
      tr.className = row.award_class ? `itamx-${row.award_class}` : "";
      tr.innerHTML = `
        <td><button type="button" data-row="${index}">${dateFromRow(row)}</button></td>
        <td>${row.award_class || row.cabin || "-"}</td>
        <td>${row.rbd || "-"}</td>
        <td>${(row.flights || []).join(" ") || "-"}</td>
        <td>${row.origin || "-"}-${row.destination || "-"}</td>
        <td>${formatTime(row.departure)}-${formatTime(row.arrival)}</td>
        <td>${formatNumber(row.points)}</td>
        <td>${formatMoney(row.tax_amount, row.tax_currency)}</td>
        <td>${formatMoney(row.cash_amount, row.cash_currency)}</td>
        <td>${row.seats_left ?? "-"}</td>
        <td>${aircraft(row) || "-"}</td>
        <td>${row.fare_family || "-"}</td>
      `;
      tbody.appendChild(tr);
    });
    scheduleEmbeddedDetails();
  }

  function handleAwardMessage(data) {
    state.raw.push(data);
    if (data.kind === "ready") {
      state.status = "Hook ready";
      render();
      return;
    }
    if (data.kind === "fast") {
      state.rows = [];
      state.errors = [];
      state.request = data.request || null;
      state.status = `Search ${data.status || ""}`.trim();
    }
    if (data.response && Array.isArray(data.response.errors) && data.response.errors.length) {
      state.errors.push({ kind: data.kind, status: data.status, errors: data.response.errors });
    }
    if (data.kind === "outbound" || data.kind === "inbound") {
      const rows = normalizeDirection(data.response, data.kind);
      state.rows = state.rows.filter((row) => row.direction !== data.kind).concat(rows);
      state.status = `${data.kind} ${data.status || ""}`.trim();
      setTimeout(scheduleEmbeddedDetails, 600);
      setTimeout(scheduleEmbeddedDetails, 1600);
    }
    render();
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) {
      return;
    }
    const data = event.data;
    if (!data || data.source !== HOOK_SOURCE) {
      return;
    }
    handleAwardMessage(data);
  });

  if (document.documentElement) {
    injectHook();
    ensurePanel();
    render();
  } else {
    document.addEventListener("DOMContentLoaded", () => {
      injectHook();
      ensurePanel();
      render();
    });
  }
})();
