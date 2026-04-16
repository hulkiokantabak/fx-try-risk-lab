const noteKey = "fx-try-risk-lab-note";
const SVG_NS = "http://www.w3.org/2000/svg";
const chartPalette = ["#1d5c4b", "#b76a2b", "#6b7f78"];

async function loadJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Failed to load ${path}`);
  }
  return response.json();
}

function clearNode(node) {
  while (node.firstChild) {
    node.removeChild(node.firstChild);
  }
}

function appendTextElement(parent, tagName, text, className = "") {
  const element = document.createElement(tagName);
  if (className) {
    element.className = className;
  }
  element.textContent = text;
  parent.appendChild(element);
  return element;
}

function formatNumber(value, digits = 1) {
  return Number.isFinite(value) ? value.toFixed(digits) : "n/a";
}

function formatChange(value, digits = 2) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "n/a";
  }
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

function capitalizeWord(value) {
  if (typeof value !== "string" || !value.length) {
    return "n/a";
  }
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function normalizeArray(value) {
  return Array.isArray(value) ? value : [];
}

function safeExternalHref(value) {
  if (typeof value !== "string" || !value.trim()) {
    return null;
  }

  try {
    const url = new URL(value, window.location.origin);
    if (url.protocol === "http:" || url.protocol === "https:") {
      return url.href;
    }
  } catch (error) {
    console.warn("Ignoring invalid link", error);
  }

  return null;
}

function createSvgElement(tagName) {
  return document.createElementNS(SVG_NS, tagName);
}

function formatChartValue(value, suffix = "") {
  return `${formatNumber(value, 2)}${suffix}`;
}

function flattenChartPoints(chartData) {
  return normalizeArray(chartData?.series).flatMap((series) => normalizeArray(series.points));
}

function buildLinePoints(points, bounds) {
  if (!points.length) {
    return "";
  }

  const { minValue, maxValue, width, height, padding } = bounds;
  const range = maxValue - minValue || 1;
  const xStep = points.length > 1 ? (width - padding * 2) / (points.length - 1) : 0;

  return points
    .map((point, index) => {
      const x = padding + xStep * index;
      const y = height - padding - ((point.value - minValue) / range) * (height - padding * 2);
      return `${x.toFixed(2)},${y.toFixed(2)}`;
    })
    .join(" ");
}

function renderLineChart(chartId, legendId, metaId, chartData, suffix = "") {
  const svg = document.getElementById(chartId);
  const legend = document.getElementById(legendId);
  const meta = document.getElementById(metaId);
  clearNode(svg);
  clearNode(legend);

  const allPoints = flattenChartPoints(chartData);
  if (!allPoints.length) {
    meta.textContent = "Chart unavailable in this snapshot.";
    const fallback = document.createElement("span");
    fallback.className = "stamp";
    fallback.textContent = "No chart data";
    legend.appendChild(fallback);
    return;
  }

  const values = allPoints.map((point) => point.value);
  const minValue = Math.min(...values);
  const maxValue = Math.max(...values);
  const bounds = { minValue, maxValue, width: 360, height: 180, padding: 18 };

  [0.2, 0.5, 0.8].forEach((fraction) => {
    const guide = createSvgElement("line");
    const y = bounds.padding + (bounds.height - bounds.padding * 2) * fraction;
    guide.setAttribute("x1", String(bounds.padding));
    guide.setAttribute("x2", String(bounds.width - bounds.padding));
    guide.setAttribute("y1", y.toFixed(2));
    guide.setAttribute("y2", y.toFixed(2));
    guide.setAttribute("class", "chart-guide");
    svg.appendChild(guide);
  });

  normalizeArray(chartData?.series).forEach((series, index) => {
    const polyline = createSvgElement("polyline");
    polyline.setAttribute("fill", "none");
    polyline.setAttribute("stroke", chartPalette[index % chartPalette.length]);
    polyline.setAttribute("stroke-width", "3");
    polyline.setAttribute("stroke-linecap", "round");
    polyline.setAttribute("stroke-linejoin", "round");
    polyline.setAttribute("points", buildLinePoints(normalizeArray(series.points), bounds));
    svg.appendChild(polyline);

    const legendItem = document.createElement("div");
    legendItem.className = "chart-legend-item";
    const swatch = document.createElement("span");
    swatch.className = "legend-swatch";
    swatch.style.backgroundColor = chartPalette[index % chartPalette.length];
    legendItem.appendChild(swatch);
    appendTextElement(
      legendItem,
      "span",
      `${series.label ?? "Series"} ${formatChartValue(normalizeArray(series.points).at(-1)?.value, suffix)}`,
    );
    legend.appendChild(legendItem);
  });

  const firstPoint = allPoints[0];
  const lastPoint = allPoints[allPoints.length - 1];
  meta.textContent =
    `${chartData?.subtitle ?? "Chart"} Range ${formatChartValue(minValue, suffix)} to ${formatChartValue(maxValue, suffix)}. ` +
    `${firstPoint.date ?? "start"} to ${lastPoint.date ?? "end"}.`;
}

function renderCurve(snapshot) {
  const curveGrid = document.getElementById("curve-grid");
  clearNode(curveGrid);

  Object.entries(snapshot.curve).forEach(([horizon, score]) => {
    const card = document.createElement("article");
    card.className = "curve-card";
    if (horizon === snapshot.primary_horizon) {
      card.classList.add("primary");
    }

    appendTextElement(card, "span", horizon, "eyebrow");
    appendTextElement(card, "strong", formatNumber(score));
    appendTextElement(
      card,
      "p",
      `Chance TRY weakens more than ${snapshot.thresholds[horizon]}%.`,
    );
    curveGrid.appendChild(card);
  });
}

function renderReasonList(snapshot) {
  const container = document.getElementById("reasons");
  clearNode(container);

  const reasons = normalizeArray(snapshot.reasons);
  if (!reasons.length) {
    appendTextElement(container, "p", "No supporting reasons were published for this snapshot.");
    return;
  }

  reasons.forEach((reason) => {
    const row = document.createElement("article");
    row.className = "reason";
    const titleRow = document.createElement("div");
    titleRow.className = "reason-title";

    appendTextElement(titleRow, "span", reason.title ?? "Untitled reason");
    appendTextElement(titleRow, "span", formatNumber(reason.score), "badge");
    row.appendChild(titleRow);
    appendTextElement(row, "p", reason.detail ?? "No detail provided.", "reason-detail");
    container.appendChild(row);
  });
}

function renderWatchlist(snapshot) {
  const container = document.getElementById("watchlist");
  clearNode(container);

  const items = normalizeArray(snapshot.watchlist);
  if (!items.length) {
    appendTextElement(container, "li", "No additional watch items were published for this snapshot.");
    return;
  }

  items.forEach((item) => {
    appendTextElement(container, "li", item);
  });
}

function renderMetricGrid(targetId, items) {
  const container = document.getElementById(targetId);
  clearNode(container);

  items.forEach((item) => {
    const article = document.createElement("article");
    article.className = "metric";
    appendTextElement(article, "span", item.label, "eyebrow");
    appendTextElement(article, "strong", item.value);
    appendTextElement(article, "p", item.detail);
    container.appendChild(article);
  });
}

function renderBriefingGrid(targetId, items, valueClass = "") {
  const container = document.getElementById(targetId);
  clearNode(container);

  normalizeArray(items).forEach((item) => {
    const card = document.createElement("article");
    card.className = "brief-card";
    appendTextElement(card, "span", item.label ?? "", "eyebrow");
    appendTextElement(card, "h3", item.title ?? "Untitled card");
    if (item.now) {
      appendTextElement(card, "p", item.now, valueClass || "brief-now");
    }
    appendTextElement(card, "p", item.detail ?? "No detail provided.", "brief-detail");
    container.appendChild(card);
  });
}

function renderHistory(history) {
  const body = document.getElementById("history-body");
  clearNode(body);

  const items = normalizeArray(history);
  if (!items.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.textContent = "No published history is available yet.";
    row.appendChild(cell);
    body.appendChild(row);
    return;
  }

  items
    .slice()
    .reverse()
    .forEach((entry) => {
      const row = document.createElement("tr");
      [
        entry.as_of?.slice(0, 10) ?? "n/a",
        entry.primary_horizon ?? "n/a",
        formatNumber(entry.primary_score),
        entry.market_regime ?? "n/a",
        entry.macro_regime ?? "n/a",
      ].forEach((value) => {
        appendTextElement(row, "td", value);
      });
      body.appendChild(row);
    });
}

function renderHeadlines(snapshot) {
  const container = document.getElementById("headline-list");
  clearNode(container);

  const headlines = normalizeArray(snapshot.news?.recent_headlines);
  if (!headlines.length) {
    appendTextElement(container, "li", "No recent headlines were captured in this snapshot.");
    return;
  }

  headlines.forEach((headline) => {
    const li = document.createElement("li");
    const link = document.createElement("a");
    const safeHref = safeExternalHref(headline.link);

    link.textContent = headline.title ?? "Untitled headline";
    if (safeHref) {
      link.href = safeHref;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
    } else {
      link.href = "#";
      link.className = "disabled-link";
      link.setAttribute("aria-disabled", "true");
    }

    li.appendChild(link);
    li.appendChild(document.createTextNode(" "));
    appendTextElement(li, "span", `(${headline.published_at ?? "date unavailable"})`, "stamp");
    container.appendChild(li);
  });
}

function renderWarnings(snapshot) {
  const section = document.getElementById("caveats");
  const list = document.getElementById("warning-list");
  clearNode(list);

  document.getElementById("caveat-message").textContent =
    snapshot.briefing?.caveat_message ??
    "The model still publishes a usable estimate, but these source issues can make the read more neutral than usual.";

  const warnings = normalizeArray(snapshot.warnings);
  if (!warnings.length) {
    section.hidden = true;
    return;
  }

  warnings.forEach((warning) => {
    appendTextElement(list, "li", warning);
  });
  section.hidden = false;
}

function storageGet(key) {
  try {
    return window.localStorage.getItem(key);
  } catch (error) {
    console.warn("Local storage is unavailable.", error);
    return null;
  }
}

function storageSet(key, value) {
  try {
    window.localStorage.setItem(key, value);
    return true;
  } catch (error) {
    console.warn("Local storage is unavailable.", error);
    return false;
  }
}

function restoreNote() {
  const field = document.getElementById("local-note");
  const existing = storageGet(noteKey);
  if (existing) {
    field.value = existing;
    document.getElementById("note-status").textContent = "Saved locally on this browser.";
  }
}

function wireNoteActions(snapshot) {
  const noteField = document.getElementById("local-note");
  const saveButton = document.querySelector("[data-save-note]");
  const exportButton = document.querySelector("[data-export-note]");
  const status = document.getElementById("note-status");

  saveButton.addEventListener("click", () => {
    const saved = storageSet(noteKey, noteField.value);
    status.textContent = saved
      ? "Saved locally on this browser."
      : "This browser blocked local saving, so the note was not stored.";
  });

  exportButton.addEventListener("click", () => {
    const payload = {
      exported_at: new Date().toISOString(),
      snapshot_generated_at: snapshot.generated_at,
      primary_horizon: snapshot.primary_horizon,
      primary_score: snapshot.primary_score,
      note: noteField.value,
    };

    const blob = new Blob([JSON.stringify(payload, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "fx-try-risk-note.json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  });
}

function renderSnapshot(snapshot, history) {
  const briefing = snapshot.briefing ?? {};
  const threshold = snapshot.thresholds[snapshot.primary_horizon];

  document.getElementById("house-call-title").textContent = briefing.stance ?? "Snapshot unavailable";
  document.getElementById("house-call-text").textContent =
    briefing.house_call ?? snapshot.headline ?? "No house call was published.";
  document.getElementById("briefing-horizon").textContent = briefing.primary_horizon ?? "n/a";
  document.getElementById("briefing-confidence").textContent = capitalizeWord(
    briefing.confidence ?? "n/a",
  );
  document.getElementById("briefing-caveat").textContent = capitalizeWord(
    briefing.caveat_severity ?? "n/a",
  );

  document.getElementById("primary-score").textContent = formatNumber(
    briefing.probability ?? snapshot.primary_score,
  );
  document.getElementById("primary-meta").textContent =
    `Chance TRY weakens more than ${threshold}% over ${snapshot.primary_horizon}. Confidence ${briefing.confidence ?? "n/a"}.`;
  document.getElementById("generated-at").textContent =
    `Last built ${snapshot.generated_at.replace("T", " ").replace("Z", " UTC")}`;

  renderBriefingGrid("why-read-grid", snapshot.why_read);
  renderBriefingGrid("trigger-grid", snapshot.trigger_cards);
  renderCurve(snapshot);
  renderMetricGrid("market-metrics", [
    {
      label: "USD/TRY",
      value: formatNumber(snapshot.market.usd_try.latest, 4),
      detail: `5d ${formatChange(snapshot.market.usd_try.change_5d)} | 20d ${formatChange(snapshot.market.usd_try.change_20d)}`,
    },
    {
      label: "TRY vs peers",
      value: formatChange(snapshot.market.try_gap_20d),
      detail: `Peer basket 20d average is ${formatChange(snapshot.market.peer_avg_20d)}.`,
    },
    {
      label: "VIX / VXEEM",
      value: `${snapshot.market.volatility.VIX ?? "n/a"} / ${snapshot.market.volatility.VXEEM ?? "n/a"}`,
      detail: `VVIX ${snapshot.market.volatility.VVIX ?? "n/a"} | OVX ${snapshot.market.volatility.OVX ?? "n/a"}`,
    },
    {
      label: "Pressure scores",
      value: `${formatNumber(snapshot.market.scores.market_pressure)} / ${formatNumber(snapshot.market.scores.volatility_pressure)}`,
      detail: "Price-action score / volatility score",
    },
  ]);
  renderMetricGrid("macro-metrics", [
    {
      label: "Fed / US 2Y",
      value: `${snapshot.macro.global.fed_funds ?? "n/a"} / ${snapshot.macro.global.us_2y ?? "n/a"}`,
      detail: `Broad dollar 20d ${formatChange(snapshot.macro.global.broad_dollar_change_20d)}.`,
    },
    {
      label: "Policy rate",
      value: formatNumber(snapshot.macro.turkey.policy_rate, 2),
      detail: "CBRT one-week repo rate",
    },
    {
      label: "Official reserves",
      value: formatNumber(snapshot.macro.turkey.official_reserve_assets, 1),
      detail: `Latest window ${formatChange(snapshot.macro.turkey.official_reserve_assets_change_4w)}`,
    },
    {
      label: "Headline load",
      value: `${snapshot.news.headline_count_14d ?? "n/a"} / ${snapshot.news.chatter_count_14d ?? "n/a"}`,
      detail: "Google News / social chatter in 14 days",
    },
  ]);

  document.getElementById("market-regime").textContent = snapshot.market.regime_label;
  document.getElementById("macro-regime").textContent = snapshot.macro.regime_label;

  renderLineChart("market-chart", "market-chart-legend", "market-chart-meta", snapshot.charts?.market_trend, "%");
  renderLineChart("score-chart", "score-chart-legend", "score-chart-meta", snapshot.charts?.score_history);
  renderReasonList(snapshot);
  renderWatchlist(snapshot);
  renderHistory(history);
  renderHeadlines(snapshot);
  renderWarnings(snapshot);

  restoreNote();
  wireNoteActions(snapshot);
}

async function main() {
  try {
    const [snapshot, history] = await Promise.all([
      loadJson("./data/latest.json"),
      loadJson("./data/history.json"),
    ]);
    renderSnapshot(snapshot, history);
  } catch (error) {
    document.getElementById("house-call-title").textContent = "Snapshot unavailable";
    document.getElementById("house-call-text").textContent =
      "The browser snapshot could not be loaded. If you are previewing locally, use start-browser.ps1 or a simple HTTP server instead of opening index.html directly.";
    document.getElementById("primary-meta").textContent = "Snapshot unavailable";
    console.error(error);
  }
}

document.addEventListener("DOMContentLoaded", main);
