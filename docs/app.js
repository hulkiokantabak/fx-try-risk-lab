"use strict";

const NOTE_KEY = "fx-try-risk-lab-note";
const SVG_NS = "http://www.w3.org/2000/svg";
const HORIZON_ORDER = ["1w", "1m", "3m", "6m", "1y"];
const CHART_COLORS = ["#37b6a3", "#e5a84b", "#91a9b8", "#d56b6b"];
const CHART_DASHES = [null, "9 5", "2 4", "12 4 2 4"];
let activeSnapshot = null;
let activeHorizon = null;

async function loadJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) throw new Error(`Failed to load ${path}`);
  return response.json();
}

function byId(id) {
  return document.getElementById(id);
}

function clearNode(node) {
  if (!node) return;
  while (node.firstChild) node.removeChild(node.firstChild);
}

function appendText(parent, tagName, text, className = "") {
  const element = document.createElement(tagName);
  if (className) element.className = className;
  element.textContent = text;
  parent.appendChild(element);
  return element;
}

function appendDefinition(parent, label, value) {
  const wrapper = document.createElement("div");
  appendText(wrapper, "dt", label);
  appendText(wrapper, "dd", value);
  parent.appendChild(wrapper);
}

function normalizeArray(value) {
  return Array.isArray(value) ? value : [];
}

function finite(value) {
  const number = typeof value === "string" && value.trim() ? Number(value) : value;
  return Number.isFinite(number) ? number : null;
}

function formatNumber(value, digits = 1) {
  const number = finite(value);
  return number === null ? "n/a" : number.toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function formatChange(value, digits = 2) {
  const number = finite(value);
  if (number === null) return "n/a";
  return `${number > 0 ? "+" : ""}${formatNumber(number, digits)}%`;
}

function formatSigned(value, suffix = "", digits = 2) {
  const number = finite(value);
  if (number === null) return "n/a";
  return `${number > 0 ? "+" : ""}${formatNumber(number, digits)}${suffix}`;
}

function formatDate(value, includeTime = false) {
  if (!value) return "not published";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  const options = includeTime
    ? { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "UTC", timeZoneName: "short" }
    : { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" };
  return new Intl.DateTimeFormat("en-GB", options).format(date);
}

function capitalize(value) {
  if (typeof value !== "string" || !value.length) return "n/a";
  return value.charAt(0).toUpperCase() + value.slice(1);
}

function safeExternalHref(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const url = new URL(value, window.location.origin);
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null;
  } catch (error) {
    console.warn("Ignoring invalid link", error);
    return null;
  }
}

function svgElement(tagName, attributes = {}) {
  const element = document.createElementNS(SVG_NS, tagName);
  Object.entries(attributes).forEach(([key, value]) => {
    element.setAttribute(key, String(value));
  });
  return element;
}

function addSvgDescription(svg, titleId, titleText, descriptionId, descriptionText) {
  const title = svgElement("title", { id: titleId });
  title.textContent = titleText;
  svg.appendChild(title);
  const description = svgElement("desc", { id: descriptionId });
  description.textContent = descriptionText;
  svg.appendChild(description);
  return description;
}

function setText(id, value) {
  const element = byId(id);
  if (element) element.textContent = value;
}

function isCalibrated(snapshot) {
  return snapshot?.model?.is_calibrated === true || snapshot?.model?.output_type === "calibrated_probability";
}

function assessmentLabel(snapshot, lower = false) {
  const label = isCalibrated(snapshot) ? "Forecast probability" : "Experimental probability estimate";
  return lower ? label.toLowerCase() : label;
}

function horizons(snapshot) {
  const keys = Object.keys(snapshot?.curve ?? {});
  return HORIZON_ORDER.filter((item) => keys.includes(item)).concat(keys.filter((item) => !HORIZON_ORDER.includes(item)));
}

function horizonUncertainty(snapshot, horizon) {
  const value = snapshot?.uncertainty?.[horizon] ?? snapshot?.forecast?.horizons?.[horizon]?.uncertainty;
  if (!value) return null;
  const lower = finite(value.lower_probability ?? value.lower ?? value.low ?? value.p10);
  const upper = finite(value.upper_probability ?? value.upper ?? value.high ?? value.p90);
  return lower === null || upper === null ? null : { lower, upper };
}

function horizonAuthority(snapshot, horizon) {
  const status = String(snapshot?.forecast?.horizons?.[horizon]?.calibration_status ?? "experimental").toLowerCase();
  return status === "calibrated"
    ? { label: "Limited authority", className: "authority-limited", detail: "This horizon passes the published historical gate; live evidence remains limited." }
    : { label: "Experimental", className: "authority-experimental", detail: "This horizon does not pass the published historical gate." };
}

function horizonChallenger(snapshot, horizon) {
  const challenger = snapshot?.forecast?.horizons?.[horizon]?.challenger;
  const probability = finite(challenger?.probability);
  if (probability === null) return null;
  return {
    probability,
    delta: finite(
      challenger?.delta_model_minus_challenger_percentage_points
        ?? challenger?.delta_model_minus_benchmark_percentage_points,
    ),
    name: challenger?.name ?? "Current target-purged climatology",
  };
}

function pathRiskView(snapshot) {
  const raw = snapshot?.path_risk ?? snapshot?.forecast?.path_risk;
  const fallbackHorizons = snapshot?.forecast?.touch_horizons;
  const horizonsPayload = raw?.horizons ?? fallbackHorizons;
  if (!horizonsPayload || typeof horizonsPayload !== "object") return null;
  const curve = {};
  HORIZON_ORDER.forEach((horizon) => {
    const item = horizonsPayload[horizon];
    const probability = finite(typeof item === "object" ? item?.probability : item);
    if (probability !== null) curve[horizon] = probability;
  });
  if (!Object.keys(curve).length) return null;
  return {
    curve,
    horizons: horizonsPayload,
    eventDefinition: raw?.event_definition ?? snapshot?.forecast?.touch_event_definition ?? {},
  };
}

function pathRiskUncertainty(pathRisk, horizon) {
  const value = pathRisk?.horizons?.[horizon]?.uncertainty;
  if (!value) return null;
  const lower = finite(value.lower_probability ?? value.lower);
  const upper = finite(value.upper_probability ?? value.upper);
  return lower === null || upper === null ? null : { lower, upper };
}

function renderAuthorityBadges(snapshot) {
  const container = byId("authority-badges");
  clearNode(container);
  horizons(snapshot).forEach((horizon) => {
    const authority = horizonAuthority(snapshot, horizon);
    const badge = document.createElement("li");
    badge.className = `authority-badge ${authority.className}`;
    badge.title = authority.detail;
    appendText(badge, "strong", horizon);
    badge.append(document.createTextNode(` ${authority.label}`));
    container.appendChild(badge);
  });
}

function baselineFor(snapshot, horizon) {
  const baseline = snapshot?.baseline ?? {};
  const specification = snapshot?.forecast?.horizons?.[horizon] ?? {};
  const spot = finite(baseline.value ?? baseline.spot ?? snapshot?.market?.usd_try?.latest);
  const threshold = finite(specification.threshold_percent ?? baseline.threshold_pct ?? snapshot?.thresholds?.[horizon]);
  const target = finite(
    specification.event?.threshold_value ?? baseline.target_spot,
  ) ?? (spot !== null && threshold !== null ? spot * (1 + threshold / 100) : null);
  return {
    spot,
    threshold,
    target,
    sessions: finite(specification.sessions),
    operator: specification.event?.operator ?? ">=",
    formula: specification.event?.formula ?? null,
    observedAt: baseline.observation_date ?? baseline.observed_at ?? snapshot?.market?.usd_try?.date ?? snapshot?.data_cutoff ?? snapshot?.generated_at,
    eventDefinition: baseline.event_definition ?? null,
  };
}

function targetRule(baseline, horizon) {
  return baseline.sessions === null
    ? `${horizon} horizon; session count not published`
    : `t + ${formatNumber(baseline.sessions, 0)}: the common ECB FX trading observation exactly ${formatNumber(baseline.sessions, 0)} sessions after baseline`;
}

function updateHorizon(horizon) {
  if (!activeSnapshot || !(horizon in (activeSnapshot.curve ?? {}))) return;
  activeHorizon = horizon;
  const score = finite(activeSnapshot.curve[horizon]);
  const baseline = baselineFor(activeSnapshot, horizon);
  const interval = horizonUncertainty(activeSnapshot, horizon);

  setText("score-label", assessmentLabel(activeSnapshot));
  setText("primary-score", score === null ? "n/a" : `${formatNumber(score, 1)}%`);
  setText(
    "primary-meta",
    baseline.threshold === null
      ? `${horizon} horizon · event threshold unavailable`
      : `${horizon} horizon · USD/TRY rises by ≥ ${formatNumber(baseline.threshold, 1)}% from baseline`,
  );
  setText("baseline-spot", baseline.spot === null ? "n/a" : `${formatNumber(baseline.spot, 4)} TRY per USD`);
  setText("baseline-date", formatDate(baseline.observedAt));
  setText("target-rule", targetRule(baseline, horizon));
  setText("target-spot", baseline.target === null ? "n/a" : `≥ ${formatNumber(baseline.target, 4)} USD/TRY`);
  setText(
    "event-definition",
    baseline.spot !== null && baseline.threshold !== null && baseline.target !== null && baseline.sessions !== null
        ? `Event: derived ECB USD/TRY at t + ${formatNumber(baseline.sessions, 0)} is ≥ ${formatNumber(baseline.target, 4)}. Equivalently, USDTRY[t+${formatNumber(baseline.sessions, 0)}] / USDTRY[t] − 1 ≥ ${formatNumber(baseline.threshold, 1)}%, using the common ECB EUR/TRY and EUR/USD trading observation exactly ${formatNumber(baseline.sessions, 0)} sessions after baseline.`
        : baseline.eventDefinition ?? `Event: derived ECB USD/TRY meets or exceeds the published ${horizon} threshold at the exact target observation.`,
  );

  const intervalNode = byId("uncertainty-label");
  if (interval) {
    intervalNode.textContent = `Published uncertainty interval: ${formatNumber(interval.lower, 1)}–${formatNumber(interval.upper, 1)}%`;
    intervalNode.hidden = false;
  } else {
    intervalNode.hidden = true;
  }

  document.querySelectorAll("[data-horizon]").forEach((button) => {
    const selected = button.dataset.horizon === horizon;
    button.classList.toggle("active", selected);
    button.setAttribute("aria-pressed", String(selected));
  });
  setText(
    "horizon-status",
    `${horizon} selected. ${assessmentLabel(activeSnapshot)} ${score === null ? "not available" : `${formatNumber(score, 1)} percent`}. ${targetRule(baseline, horizon)}.`,
  );
  renderTermChart(activeSnapshot);
  renderDrivers(activeSnapshot);
}

function renderHorizonSelector(snapshot) {
  const container = byId("horizon-selector");
  clearNode(container);
  horizons(snapshot).forEach((horizon) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "horizon-button";
    button.dataset.horizon = horizon;
    button.textContent = horizon;
    button.addEventListener("click", () => updateHorizon(horizon));
    container.appendChild(button);
  });
}

function renderTermChart(snapshot) {
  const svg = byId("term-chart");
  clearNode(svg);
  const description = addSvgDescription(
    svg,
    "term-chart-title",
    "Exact-terminal probability estimate and optional any-time-breach estimate",
    "term-chart-desc",
    "Loading chart data.",
  );
  const values = horizons(snapshot).map((horizon) => ({ horizon, value: finite(snapshot.curve[horizon]) })).filter((point) => point.value !== null);
  if (!values.length) {
    description.textContent = "No term-structure data were published.";
    return;
  }
  const width = 640;
  const height = 250;
  const margin = { left: 58, right: 22, top: 22, bottom: 46 };
  const pathRisk = pathRiskView(snapshot);
  const pathValues = pathRisk
    ? horizons(snapshot).map((horizon) => ({ horizon, value: finite(pathRisk.curve[horizon]) })).filter((point) => point.value !== null)
    : [];
  const allChartValues = values.concat(pathValues);
  const dataMin = Math.min(...allChartValues.map((point) => point.value));
  const dataMax = Math.max(...allChartValues.map((point) => point.value));
  const floor = Math.max(0, Math.floor((dataMin - 8) / 10) * 10);
  const ceiling = Math.min(100, Math.ceil((dataMax + 8) / 10) * 10) || 100;
  const range = ceiling - floor || 1;
  const x = (index) => margin.left + (index / Math.max(1, values.length - 1)) * (width - margin.left - margin.right);
  const y = (value) => height - margin.bottom - ((value - floor) / range) * (height - margin.top - margin.bottom);

  for (let tick = 0; tick <= 4; tick += 1) {
    const value = floor + (range * tick) / 4;
    const tickY = y(value);
    svg.appendChild(svgElement("line", { x1: margin.left, x2: width - margin.right, y1: tickY, y2: tickY, class: "chart-grid" }));
    const label = svgElement("text", { x: margin.left - 10, y: tickY + 4, class: "axis-label", "text-anchor": "end" });
    label.textContent = formatNumber(value, 0);
    svg.appendChild(label);
  }
  svg.appendChild(svgElement("line", { x1: margin.left, x2: width - margin.right, y1: height - margin.bottom, y2: height - margin.bottom, class: "chart-axis" }));
  const points = values.map((point, index) => `${x(index)},${y(point.value)}`).join(" ");
  svg.appendChild(svgElement("polyline", { points, fill: "none", class: "term-line" }));
  values.forEach((point, index) => {
    const label = svgElement("text", { x: x(index), y: height - 18, class: "axis-label", "text-anchor": "middle" });
    label.textContent = point.horizon;
    svg.appendChild(label);
    const circle = svgElement("circle", {
      cx: x(index), cy: y(point.value), r: point.horizon === activeHorizon ? 7 : 5,
      class: point.horizon === activeHorizon ? "term-point active" : "term-point",
    });
    const title = svgElement("title");
    title.textContent = `${point.horizon}: ${formatNumber(point.value, 1)}%`;
    circle.appendChild(title);
    svg.appendChild(circle);
    const valueLabel = svgElement("text", { x: x(index), y: y(point.value) - 12, class: "chart-value", "text-anchor": "middle" });
    valueLabel.textContent = formatNumber(point.value, 1);
    svg.appendChild(valueLabel);
  });
  if (pathValues.length) {
    const byHorizon = new Map(values.map((point, index) => [point.horizon, index]));
    const pathPoints = pathValues.map((point) => `${x(byHorizon.get(point.horizon) ?? 0)},${y(point.value)}`).join(" ");
    svg.appendChild(svgElement("polyline", { points: pathPoints, fill: "none", class: "path-risk-line" }));
    pathValues.forEach((point) => {
      const index = byHorizon.get(point.horizon) ?? 0;
      const circle = svgElement("circle", { cx: x(index), cy: y(point.value), r: 4, class: "path-risk-point" });
      const title = svgElement("title");
      title.textContent = `${point.horizon} any-time breach at an observed daily ECB reference-rate observation: ${formatNumber(point.value, 1)}%`;
      circle.appendChild(title);
      svg.appendChild(circle);
    });
  }
  const unit = svgElement("text", { x: 14, y: margin.top, class: "axis-title" });
  unit.textContent = isCalibrated(snapshot) ? "Probability (%)" : "Experimental estimate (%)";
  svg.appendChild(unit);
  description.textContent = `${assessmentLabel(snapshot)} for the exact-terminal event by horizon: ${values.map((point) => `${point.horizon} ${formatNumber(point.value, 1)}`).join(", ")}.${pathValues.length ? ` Separately trained, coherence-constrained any-time breach at observed daily ECB reference rates: ${pathValues.map((point) => `${point.horizon} ${formatNumber(point.value, 1)}`).join(", ")}. These events are not interchangeable, and intraday prices are outside the path contract.` : " No any-time-breach curve is published."}`;

  const legend = byId("term-legend");
  clearNode(legend);
  const terminalLegend = appendText(legend, "li", "Exact terminal observation", "term-legend-item terminal");
  terminalLegend.title = "Primary event: threshold met at exactly t+h.";
  if (pathValues.length) {
    const pathLegend = appendText(legend, "li", "Any daily ECB observation in t+1…t+h", "term-legend-item path");
    pathLegend.title = pathRisk.eventDefinition.relationship_to_terminal ?? "A separate path event; not interchangeable with terminal probability.";
  }

  const challengerNote = byId("challenger-note");
  const selectedChallenger = horizonChallenger(snapshot, activeHorizon);
  if (challengerNote && selectedChallenger) {
    challengerNote.textContent = `${activeHorizon} terminal challenger: ${formatNumber(selectedChallenger.probability, 1)}% (${selectedChallenger.name}). This target-purged historical event rate is a benchmark, not an expert view or an uncertainty bound.`;
    challengerNote.hidden = false;
  } else if (challengerNote) {
    challengerNote.hidden = true;
  }

  const body = byId("term-table-body");
  clearNode(body);
  setText("term-value-heading", `Exact-terminal ${assessmentLabel(snapshot).toLowerCase()}`);
  values.forEach((point) => {
    const row = document.createElement("tr");
    const baseline = baselineFor(snapshot, point.horizon);
    const interval = horizonUncertainty(snapshot, point.horizon);
    const pathProbability = finite(pathRisk?.curve?.[point.horizon]);
    const pathInterval = pathRiskUncertainty(pathRisk, point.horizon);
    const authority = horizonAuthority(snapshot, point.horizon);
    const challenger = horizonChallenger(snapshot, point.horizon);
    const horizonCell = appendText(row, "th", point.horizon);
    horizonCell.scope = "row";
    [
      baseline.sessions === null ? "not published" : `t + ${formatNumber(baseline.sessions, 0)} ECB observations`,
      `${formatNumber(point.value, 1)}%`,
      baseline.threshold === null ? "n/a" : `≥ ${formatNumber(baseline.threshold, 1)}% depreciation`,
      interval ? `${formatNumber(interval.lower, 1)}–${formatNumber(interval.upper, 1)}%` : "not published",
      pathProbability === null
        ? "not published"
        : `${formatNumber(pathProbability, 1)}%${pathInterval ? ` (${formatNumber(pathInterval.lower, 1)}–${formatNumber(pathInterval.upper, 1)}%)` : ""}`,
      challenger === null ? "not published" : `${formatNumber(challenger.probability, 1)}%`,
    ].forEach((value) => {
      appendText(row, "td", value);
    });
    const authorityCell = appendText(row, "td", authority.label);
    authorityCell.className = `authority-table ${authority.className}`;
    body.appendChild(row);
  });
}

function buildFallbackDrivers(snapshot) {
  const fromReasons = normalizeArray(snapshot.reasons).map((reason) => ({
    title: reason.title,
    detail: reason.detail,
    strength: reason.score,
    direction: /support|cushion|reserve|policy/i.test(`${reason.title} ${reason.detail}`) ? "down" : "up",
    evidence_tier: "model lens",
  }));
  if (fromReasons.length) return fromReasons;
  return normalizeArray(snapshot.why_read).map((reason) => ({
    title: reason.title,
    detail: reason.detail,
    direction: /support|cushion/i.test(reason.label ?? "") ? "down" : "up",
    evidence_tier: reason.label,
  }));
}

function publishedDrivers(snapshot) {
  if (normalizeArray(snapshot?.drivers).length) return snapshot.drivers;
  const byHorizon = snapshot?.signed_drivers ?? snapshot?.forecast?.signed_drivers;
  if (byHorizon && typeof byHorizon === "object") {
    return normalizeArray(byHorizon[activeHorizon] ?? byHorizon[snapshot.primary_horizon]);
  }
  return [];
}

function driverDirection(driver) {
  const value = String(driver.direction ?? driver.effect ?? "").toLowerCase();
  const estimatedEffect = finite(driver.estimated_effect_percentage_points);
  if (/neutral|flat|immaterial/.test(value)) return "neutral";
  if (estimatedEffect !== null) return estimatedEffect < 0 ? "down" : "up";
  return /down|lower|cushion|support|reduce|negative|relief/.test(value) ? "down" : "up";
}

function driverStrengthText(driver) {
  const strength = finite(driver.strength ?? driver.score ?? driver.estimated_effect_percentage_points);
  if (driver.estimated_effect_percentage_points !== undefined && strength !== null) {
    return `${strength > 0 ? "+" : ""}${formatNumber(strength, 1)} pp`;
  }
  return strength === null
    ? capitalize(driver.strength ?? "unranked")
    : `${formatNumber(strength, 0)}/100`;
}

function renderDrivers(snapshot) {
  const raisers = byId("risk-raisers");
  const cushions = byId("risk-cushions");
  const neutral = byId("neutral-drivers");
  const neutralPanel = byId("neutral-driver-panel");
  clearNode(raisers);
  clearNode(cushions);
  clearNode(neutral);
  const exactDrivers = publishedDrivers(snapshot);
  const drivers = exactDrivers.length ? exactDrivers : buildFallbackDrivers(snapshot);
  const groups = { up: [], down: [], neutral: [] };
  drivers.forEach((driver) => {
    groups[driverDirection(driver)].push(driver);
  });

  Object.entries({ up: groups.up, down: groups.down }).forEach(([direction, items]) => {
    const target = direction === "up" ? raisers : cushions;
    if (!items.length) {
      appendText(target, "p", "No driver published in this direction.", "empty-state");
      return;
    }
    items.slice(0, 6).forEach((driver) => {
      const article = document.createElement("article");
      article.className = "driver-row";
      const header = document.createElement("div");
      appendText(header, "h4", driver.title ?? driver.name ?? driver.label ?? "Unnamed driver");
      appendText(header, "span", driverStrengthText(driver), "strength");
      article.appendChild(header);
      appendText(
        article,
        "p",
        driver.detail ?? driver.evidence ??
          (driver.value !== undefined ? `Current ${formatNumber(driver.value, 3)} ${driver.unit ?? ""}; historical median ${formatNumber(driver.historical_median, 3)}. ${driver.interpretation ?? ""}`.trim() : "No evidence note was published."),
      );
      const metadata = [driver.evidence_tier ?? driver.tier ?? driver.source, driver.sample_count ? `n=${driver.sample_count}` : null, driver.source_date ? `observed ${formatDate(driver.source_date)}` : null].filter(Boolean).join(" · ");
      if (metadata) appendText(article, "small", metadata);
      target.appendChild(article);
    });
  });
  if (groups.neutral.length) {
    groups.neutral.slice(0, 6).forEach((driver) => {
      const article = document.createElement("article");
      article.className = "driver-row";
      const header = document.createElement("div");
      appendText(header, "h4", driver.title ?? driver.name ?? driver.label ?? "Unnamed driver");
      appendText(header, "span", driverStrengthText(driver), "strength");
      article.appendChild(header);
      appendText(article, "p", driver.interpretation ?? driver.detail ?? "No interpretation published.");
      neutral?.appendChild(article);
    });
    if (neutralPanel) neutralPanel.hidden = false;
  } else if (neutralPanel) {
    neutralPanel.hidden = true;
  }
}

function chartPoints(chartData) {
  return normalizeArray(chartData?.series).flatMap((series) => normalizeArray(series.points));
}

function renderLineChart(chartId, legendId, metaId, tableTargetId, chartData, suffix = "") {
  const svg = byId(chartId);
  const legend = byId(legendId);
  const tableTarget = byId(tableTargetId);
  clearNode(svg);
  clearNode(legend);
  clearNode(tableTarget);
  const description = addSvgDescription(
    svg,
    `${chartId}-title`,
    chartData?.title ?? "Published line chart",
    `${chartId}-desc`,
    "Loading chart data.",
  );
  const allPoints = chartPoints(chartData).filter((point) => finite(point.value) !== null);
  if (!allPoints.length) {
    setText(metaId, "Chart unavailable in this snapshot.");
    description.textContent = "No chart data were published.";
    appendText(legend, "span", "No chart data", "empty-state");
    return;
  }

  const width = 640;
  const height = 280;
  const margin = { left: 62, right: 22, top: 22, bottom: 46 };
  const values = allPoints.map((point) => finite(point.value));
  let minValue = Math.min(...values);
  let maxValue = Math.max(...values);
  const rawRange = maxValue - minValue || Math.max(1, Math.abs(maxValue) * 0.1);
  minValue -= rawRange * 0.12;
  maxValue += rawRange * 0.12;
  if (minValue > 0 && minValue < rawRange * 0.4) minValue = 0;
  if (maxValue < 0 && Math.abs(maxValue) < rawRange * 0.4) maxValue = 0;
  const range = maxValue - minValue || 1;
  const maxLength = Math.max(...normalizeArray(chartData.series).map((series) => normalizeArray(series.points).length));
  const x = (index) => margin.left + (index / Math.max(1, maxLength - 1)) * (width - margin.left - margin.right);
  const y = (value) => height - margin.bottom - ((value - minValue) / range) * (height - margin.top - margin.bottom);

  for (let tick = 0; tick <= 4; tick += 1) {
    const value = minValue + (range * tick) / 4;
    const tickY = y(value);
    const zero = Math.abs(value) < range / 100;
    svg.appendChild(svgElement("line", { x1: margin.left, x2: width - margin.right, y1: tickY, y2: tickY, class: zero ? "chart-zero" : "chart-grid" }));
    const label = svgElement("text", { x: margin.left - 10, y: tickY + 4, class: "axis-label", "text-anchor": "end" });
    label.textContent = `${formatNumber(value, range < 10 ? 1 : 0)}${suffix}`;
    svg.appendChild(label);
  }
  if (minValue < 0 && maxValue > 0) svg.appendChild(svgElement("line", { x1: margin.left, x2: width - margin.right, y1: y(0), y2: y(0), class: "chart-zero" }));
  svg.appendChild(svgElement("line", { x1: margin.left, x2: width - margin.right, y1: height - margin.bottom, y2: height - margin.bottom, class: "chart-axis" }));

  normalizeArray(chartData.series).forEach((series, seriesIndex) => {
    const points = normalizeArray(series.points).filter((point) => finite(point.value) !== null);
    const color = CHART_COLORS[seriesIndex % CHART_COLORS.length];
    const dashPattern = CHART_DASHES[seriesIndex % CHART_DASHES.length];
    const coordinates = points.map((point, index) => `${x(index)},${y(finite(point.value))}`).join(" ");
    const lineAttributes = { points: coordinates, fill: "none", stroke: color, class: "chart-line" };
    if (dashPattern) lineAttributes["stroke-dasharray"] = dashPattern;
    svg.appendChild(svgElement("polyline", lineAttributes));
    points.forEach((point, index) => {
      const marker = svgElement("circle", { cx: x(index), cy: y(finite(point.value)), r: 3, fill: color, class: "chart-marker" });
      const title = svgElement("title");
      title.textContent = `${series.label ?? "Series"}, ${formatDate(point.date)}: ${formatNumber(point.value, 2)}${suffix}`;
      marker.appendChild(title);
      svg.appendChild(marker);
    });
    const item = document.createElement("div");
    item.className = "legend-item";
    const swatch = document.createElement("span");
    swatch.className = `legend-swatch palette-${seriesIndex % CHART_COLORS.length} pattern-${seriesIndex % CHART_DASHES.length}`;
    swatch.setAttribute("aria-hidden", "true");
    item.appendChild(swatch);
    const latestPoint = points.length ? points[points.length - 1] : null;
    appendText(item, "span", `${series.label ?? "Series"} · ${formatNumber(latestPoint?.value, 2)}${suffix}`);
    legend.appendChild(item);
  });

  const longestSeries = normalizeArray(chartData.series).find((series) => normalizeArray(series.points).length === maxLength)?.points ?? [];
  [0, Math.floor((maxLength - 1) / 2), maxLength - 1].filter((index, position, array) => index >= 0 && array.indexOf(index) === position).forEach((index) => {
    const point = longestSeries[index];
    const label = svgElement("text", { x: x(index), y: height - 17, class: "axis-label", "text-anchor": index === 0 ? "start" : index === maxLength - 1 ? "end" : "middle" });
    label.textContent = point?.date ? formatDate(point.date).replace(/ \d{4}$/, "") : "";
    svg.appendChild(label);
  });

  const first = allPoints[0];
  const last = allPoints[allPoints.length - 1];
  const unit = chartId === "score-chart"
    ? assessmentLabel(activeSnapshot)
    : chartData?.unit ?? (suffix === "%" ? "Percent" : assessmentLabel(activeSnapshot));
  setText(metaId, `${chartData?.subtitle ?? "Published series."} Unit: ${unit}. ${formatDate(first.date)}–${formatDate(last.date)}.`);
  description.textContent = `${chartData?.title ?? "Line chart"}. Range ${formatNumber(Math.min(...values), 2)} to ${formatNumber(Math.max(...values), 2)} ${unit}.`;
  renderChartTable(tableTarget, chartData, suffix);
}

function renderChartTable(target, chartData, suffix) {
  const tableWrap = document.createElement("div");
  tableWrap.className = "table-wrap";
  const table = document.createElement("table");
  table.className = "data-table";
  const caption = appendText(table, "caption", chartData?.title ?? "Chart data");
  caption.className = "sr-only";
  const head = document.createElement("thead");
  const headRow = document.createElement("tr");
  ["Date", ...normalizeArray(chartData?.series).map((series) => series.label ?? "Series")].forEach((label) => {
    const th = appendText(headRow, "th", label);
    th.scope = "col";
  });
  head.appendChild(headRow);
  table.appendChild(head);
  const body = document.createElement("tbody");
  const dates = [...new Set(normalizeArray(chartData?.series).flatMap((series) => normalizeArray(series.points).map((point) => point.date)))];
  dates.forEach((date) => {
    const row = document.createElement("tr");
    const dateCell = appendText(row, "th", formatDate(date));
    dateCell.scope = "row";
    normalizeArray(chartData?.series).forEach((series) => {
      const point = normalizeArray(series.points).find((item) => item.date === date);
      appendText(row, "td", point ? `${formatNumber(point.value, 2)}${suffix}` : "—");
    });
    body.appendChild(row);
  });
  table.appendChild(body);
  tableWrap.appendChild(table);
  target.appendChild(tableWrap);
}

function renderMetricGrid(targetId, items) {
  const container = byId(targetId);
  clearNode(container);
  items.forEach((item) => {
    const article = document.createElement("article");
    article.className = "metric";
    appendText(article, "span", item.label);
    appendText(article, "strong", item.value);
    appendText(article, "p", item.detail);
    if (item.observationText) appendText(article, "small", item.observationText);
    else if (item.date) appendText(article, "small", `Observed ${formatDate(item.date)}`);
    container.appendChild(article);
  });
}

function dataHealthSource(snapshot, key) {
  return normalizeArray(snapshot?.data_health?.sources).find((source) => (source.key ?? source.id) === key) ?? null;
}

function sourceObservationText(snapshot, labelledKeys) {
  const details = labelledKeys.map(([label, key]) => {
    const source = dataHealthSource(snapshot, key);
    if (!source) return `${label}: UNKNOWN; source record not published`;
    const status = sourceStatus(source);
    const observed = source.latest_observation ?? source.observed_at ?? source.source_date ?? source.as_of;
    if (!observed) return `${label}: ${capitalize(status)}; observation date not published`;
    return status === "fresh"
      ? `${label}: fresh; observed ${formatDate(observed)}`
      : `${label}: ${capitalize(status)}; last observation ${formatDate(observed)}`;
  });
  return `Sources · ${details.join(" · ")}`;
}

function renderTriggers(snapshot) {
  const container = byId("trigger-grid");
  clearNode(container);
  const triggers = normalizeArray(snapshot.trigger_cards);
  if (!triggers.length) {
    appendText(container, "p", "No decision triggers were published.", "empty-state");
    return;
  }
  triggers.slice(0, 5).forEach((trigger) => {
    const article = document.createElement("article");
    article.className = "trigger-row";
    appendText(article, "h3", trigger.title ?? "Unnamed trigger");
    if (trigger.now) appendText(article, "p", trigger.now, "trigger-now");
    appendText(article, "p", trigger.detail ?? "No trigger detail published.");
    container.appendChild(article);
  });
}

function expertMembers(expertView) {
  return normalizeArray(expertView?.final_experts ?? expertView?.members ?? expertView?.experts ?? expertView?.views);
}

function confidenceLabel(value) {
  const score = finite(value?.score ?? value);
  if (score === null) return "Not scored";
  if (score < 40) return "Low confidence";
  if (score < 65) return "Moderate confidence";
  return "High confidence";
}

function confidenceScore(value) {
  return finite(value?.score ?? value);
}

function appendExpertCurve(parent, curve) {
  const list = document.createElement("dl");
  list.className = "expert-curve";
  HORIZON_ORDER.forEach((horizon) => {
    const value = finite(curve?.[horizon]);
    if (value === null) return;
    const item = document.createElement("div");
    appendText(item, "dt", horizon);
    appendText(item, "dd", `${formatNumber(value, 1)}%`);
    list.appendChild(item);
  });
  parent.appendChild(list);
}

function renderExpertView(snapshot) {
  const view = snapshot.expert_view;
  const members = expertMembers(view);
  const container = byId("expert-members");
  const houseBody = byId("expert-house-body");
  const dissent = byId("expert-dissent");
  const stress = byId("expert-stress");
  clearNode(container);
  clearNode(houseBody);
  clearNode(dissent);
  clearNode(stress);
  if (!view || !members.length) {
    setText("expert-status", "Not published");
    setText("expert-summary", "No structured expert assessment accompanies this snapshot. The model output and any future expert overlay are kept separate by design.");
    setText("expert-meta", "Expert judgment is never substituted for the empirical model.");
    appendText(container, "p", "Awaiting a frozen-evidence expert round.", "empty-state");
    byId("expert-house-wrap").hidden = true;
    dissent.hidden = true;
    stress.hidden = true;
    return;
  }
  const house = view.house ?? {};
  const disagreement = view.disagreement ?? {};
  const houseConfidence = confidenceScore(house.confidence);
  const aggregation = String(house.aggregation ?? house.aggregation_method ?? house.method ?? "aggregation method not published").replace(/-/g, " ");
  setText("expert-status", "Expert judgment—not model");
  setText("expert-summary", house.summary ?? view.summary ?? view.house_view ?? "Structured specialist views on the same evidence pack.");
  setText(
    "expert-meta",
    `House confidence ${formatNumber(houseConfidence, 0)}/100 · ${confidenceLabel(house.confidence)} · ${aggregation}. Frozen evidence ${view.evidence?.forecast_id ?? "not identified"}.`,
  );
  byId("expert-house-wrap").hidden = false;
  HORIZON_ORDER.forEach((horizon) => {
    const value = finite(house.curve?.[horizon]);
    const range = disagreement.ranges?.[horizon] ?? {};
    if (value === null) return;
    const row = document.createElement("tr");
    const horizonCell = appendText(row, "th", horizon);
    horizonCell.scope = "row";
    appendText(row, "td", `${formatNumber(value, 1)}%`);
    const lower = finite(range.min);
    const upper = finite(range.max);
    appendText(row, "td", lower === null || upper === null ? "not published" : `${formatNumber(lower, 1)}–${formatNumber(upper, 1)}%`);
    houseBody.appendChild(row);
  });
  members.forEach((member) => {
    const card = document.createElement("article");
    card.className = "expert-card";
    const role = member.role ?? member.name ?? "Unnamed expert";
    appendText(card, "span", member.mandate ?? member.specialty ?? "Final round", "eyebrow");
    appendText(card, "h3", role);
    appendText(card, "strong", member.stance ?? member.view ?? "No stance published");
    appendText(card, "p", `Confidence ${formatNumber(member.confidence, 0)}/100 · ${confidenceLabel(member.confidence)}`, "expert-confidence");
    appendExpertCurve(card, member.curve);
    const rationale = member.rationale ?? member.summary ?? member.reasoning;
    if (rationale) appendText(card, "p", rationale);
    container.appendChild(card);
  });
  const dissentText = disagreement.minority_view ?? view.dissent ?? view.minority_view;
  if (dissentText) {
    appendText(dissent, "strong", "Preserved dissent");
    appendText(dissent, "p", String(dissentText));
    dissent.hidden = false;
  } else dissent.hidden = true;
  const stressText = disagreement.stress_view ?? view.stress_view;
  if (stressText) {
    appendText(stress, "strong", "Stress view");
    appendText(stress, "p", String(stressText));
    stress.hidden = false;
  } else stress.hidden = true;
}

function calibrationHorizons(calibration) {
  if (calibration?.horizons && typeof calibration.horizons === "object") return calibration.horizons;
  if (calibration?.metrics && typeof calibration.metrics === "object") return calibration.metrics;
  return {};
}

function renderCalibration(snapshot) {
  const calibration = snapshot.calibration ?? snapshot.backtest ?? snapshot.forecast?.backtest ?? {};
  const calibrated = isCalibrated(snapshot);
  const state = byId("calibration-state");
  const summary = byId("calibration-summary");
  const body = byId("calibration-body");
  clearNode(state);
  clearNode(summary);
  clearNode(body);
  const icon = appendText(state, "span", calibrated ? "✓" : "!", calibrated ? "state-icon good" : "state-icon caution");
  icon.setAttribute("aria-hidden", "true");
  const copy = document.createElement("div");
  appendText(copy, "h3", calibrated ? "Validation gate passed" : "Experimental estimate—validation gate failed");
  appendText(copy, "p", calibrated
    ? "Every horizon passes the published backtest gate and has at least 20 resolved local calibration analogs; the live ledger is separate."
    : "This remains an experimental probability estimate. At least one horizon fails its backtest gate or lacks 20 resolved local calibration analogs; live outcomes are separate.");
  state.appendChild(copy);

  const horizonData = calibrationHorizons(calibration);
  const primaryMetrics = horizonData[snapshot.primary_horizon] ?? {};
  const live = snapshot.track_record?.live_ledger ?? {};
  const issued = finite(live.issued_forecasts);
  const resolved = finite(live.resolved_horizon_outcomes ?? live.resolved_forecast_count);
  const backtestN = finite(primaryMetrics.forecast_count ?? primaryMetrics.sample_size ?? primaryMetrics.n);
  const minimumCalibrationN = finite(calibration.sample_size);
  const brier = finite(calibration.brier_score ?? calibration.brier ?? primaryMetrics.brier_score);
  appendDefinition(summary, "Live forecasts issued", issued === null ? "not published" : formatNumber(issued, 0));
  appendDefinition(summary, "Live horizon outcomes resolved", resolved === null ? "not published" : formatNumber(resolved, 0));
  appendDefinition(summary, `${snapshot.primary_horizon ?? "Primary"} backtest N`, backtestN === null ? "not published" : formatNumber(backtestN, 0));
  appendDefinition(summary, `${snapshot.primary_horizon ?? "Primary"} backtest Brier`, brier === null ? "not published" : formatNumber(brier, 3));
  appendDefinition(summary, "Minimum current calibration N", minimumCalibrationN === null ? "not published" : formatNumber(minimumCalibrationN, 0));
  appendDefinition(summary, "Model-level validation gate", calibrated ? "Pass" : "Fail");
  appendDefinition(summary, "Model version", snapshot.model?.version ?? "legacy / unversioned");

  const available = Object.keys(horizonData);
  const rowHorizons = horizons(snapshot).length ? horizons(snapshot) : HORIZON_ORDER;
  rowHorizons.forEach((horizon) => {
    const item = horizonData[horizon] ?? {};
    const row = document.createElement("tr");
    const count = finite(item.sample_size ?? item.n ?? item.observations ?? item.forecast_count);
    const skill = finite(item.brier_skill_vs_climatology);
    const ece = finite(item.calibration_error);
    const currentCalibrationN = finite(snapshot.forecast?.horizons?.[horizon]?.sample?.calibration_examples);
    const publishedGate = snapshot.forecast?.horizons?.[horizon]?.calibration_status;
    const passes = publishedGate
      ? publishedGate === "calibrated"
      : count !== null && count >= 50 && skill !== null && skill > 0 && ece !== null && ece <= 0.10;
    const horizonCell = appendText(row, "th", horizon);
    horizonCell.scope = "row";
    [
      formatNumber(count, 0),
      formatNumber(item.brier_score ?? item.brier, 3),
      formatNumber(item.log_loss ?? item.logloss, 3),
      skill === null ? "n/a" : formatSigned(skill, "", 3),
      ece === null ? "n/a" : formatNumber(ece, 3),
      currentCalibrationN === null ? "n/a" : formatNumber(currentCalibrationN, 0),
    ].forEach((value) => {
      appendText(row, "td", value);
    });
    const isReported = available.includes(horizon);
    const gate = appendText(row, "td", isReported ? (passes ? "Pass" : "Fail") : "Not reported");
    gate.className = isReported ? `gate-status ${passes ? "gate-pass" : "gate-fail"}` : "gate-status";
    body.appendChild(row);
  });
}

function inferHealth(snapshot) {
  if (snapshot.data_health) return snapshot.data_health;
  const global = snapshot.macro?.global ?? {};
  const marketDate = snapshot.market?.usd_try?.date ?? snapshot.generated_at;
  const warnings = normalizeArray(snapshot.warnings);
  const fredMissing = [global.fed_funds, global.us_2y, global.us_10y, global.broad_dollar_change_20d].some((value) => finite(value) === null);
  const sources = [
    { name: "ECB FX reference rates", status: snapshot.market?.usd_try?.latest ? "fresh" : "unavailable", observed_at: marketDate, detail: "USD/TRY and peer FX series" },
    { name: "Cboe volatility indices", status: snapshot.market?.volatility?.VIX ? "fresh" : "unavailable", observed_at: snapshot.data_cutoff ?? snapshot.generated_at, detail: "VIX, VXEEM and related risk gauges" },
    { name: "FRED global macro", status: fredMissing ? "unavailable" : "fresh", observed_at: snapshot.data_cutoff ?? snapshot.generated_at, detail: "US rates and broad dollar series" },
    { name: "CBRT policy and reserves", status: snapshot.macro?.turkey?.policy_rate ? "fresh" : "unavailable", observed_at: snapshot.data_cutoff ?? snapshot.generated_at, detail: "Policy rate and official reserve assets" },
    { name: "Public news feeds", status: "fresh", observed_at: snapshot.generated_at, detail: "Contextual headline flow" },
  ];
  return {
    status: warnings.length ? "degraded" : "healthy",
    fresh_count: sources.filter((source) => source.status === "fresh").length,
    stale_count: 0,
    unavailable_count: sources.filter((source) => source.status === "unavailable").length,
    sources,
    critical_failures: warnings,
  };
}

function sourceStatus(source) {
  const status = String(source.status ?? (source.is_fresh === true ? "fresh" : "unknown")).toLowerCase();
  if (/fresh|healthy|ok|live/.test(status)) return "fresh";
  if (/stale|cached|delayed/.test(status)) return "stale";
  if (/unavailable|failed|missing|error/.test(status)) return "unavailable";
  return "unknown";
}

function renderHealth(snapshot) {
  const health = inferHealth(snapshot);
  const status = String(health.overall_status ?? health.status ?? "unknown").toLowerCase();
  const alert = byId("data-alert");
  alert.className = `data-alert ${/healthy|good|fresh/.test(status) ? "healthy" : /critical|failed|blocked/.test(status) ? "critical" : "degraded"}`;
  const failures = normalizeArray(health.critical_failures);
  const unavailable = finite(health.unavailable_count) ?? normalizeArray(health.sources).filter((source) => sourceStatus(source) === "unavailable").length;
  const stale = finite(health.stale_count) ?? normalizeArray(health.sources).filter((source) => sourceStatus(source) === "stale").length;
  const healthy = /healthy|good|fresh/.test(status) && unavailable === 0 && stale === 0;
  setText("data-alert-title", healthy ? "All critical sources healthy" : `${capitalize(status)} evidence pack`);
  setText("data-alert-detail", healthy ? "No source failure is affecting this assessment." : `${unavailable} unavailable and ${stale} stale source${unavailable + stale === 1 ? "" : "s"}. Treat the assessment with added caution.`);

  const counts = byId("health-counts");
  clearNode(counts);
  [["Fresh", health.fresh_count], ["Stale", stale], ["Unavailable", unavailable]].forEach(([label, value]) => {
    const item = document.createElement("li");
    appendText(item, "strong", formatNumber(value, 0));
    item.appendChild(document.createTextNode(` ${label}`));
    counts.appendChild(item);
  });

  const grid = byId("source-grid");
  clearNode(grid);
  normalizeArray(health.sources).forEach((source) => {
    const statusValue = sourceStatus(source);
    const article = document.createElement("article");
    article.className = `source-card ${statusValue}`;
    const header = document.createElement("div");
    appendText(header, "h3", source.name ?? source.label ?? "Unnamed source");
    appendText(header, "span", capitalize(statusValue), `source-status ${statusValue}`);
    article.appendChild(header);
    appendText(
      article,
      "p",
      source.detail ?? source.message ??
        (source.used_cache ? "Using a semantically valid last-good cache." : source.last_error ? "The latest fetch attempt failed; see technical warnings." : "Validated public input."),
    );
    const observed = source.latest_observation ?? source.observed_at ?? source.source_date ?? source.as_of;
    appendText(
      article,
      "small",
      observed
        ? `${statusValue === "fresh" ? "Observed" : `${capitalize(statusValue)}; last observation`} ${formatDate(observed)}${source.age_days !== undefined ? ` · ${source.age_days} days old` : ""}${source.item_count !== undefined ? ` · ${source.item_count} items` : ""}`
        : `${capitalize(statusValue)}; observation date not published`,
    );
    grid.appendChild(article);
  });

  const warnings = [...new Set([...normalizeArray(snapshot.warnings), ...failures].map(String))];
  const list = byId("warning-list");
  clearNode(list);
  setText("warning-count", `(${warnings.length})`);
  warnings.forEach((warning) => {
    appendText(list, "li", warning);
  });
  byId("warning-disclosure").hidden = warnings.length === 0;
}

function renderHistory(history) {
  const body = byId("history-body");
  clearNode(body);
  const items = normalizeArray(history).slice().reverse().slice(0, 12);
  if (!items.length) {
    const row = document.createElement("tr");
    const cell = appendText(row, "td", "No publication history available.");
    cell.colSpan = 4;
    body.appendChild(row);
    return;
  }
  items.forEach((entry) => {
    const row = document.createElement("tr");
    const dateCell = appendText(row, "th", formatDate(entry.as_of));
    dateCell.scope = "row";
    [
      entry.primary_horizon ?? "n/a",
      `${formatNumber(entry.primary_score, 1)}%`,
      entry.stance ?? entry.market_regime ?? "n/a",
    ].forEach((value) => {
      appendText(row, "td", value);
    });
    body.appendChild(row);
  });
}

function sameModelHistory(history, snapshot) {
  const modelVersion = snapshot?.model?.version ?? null;
  return normalizeArray(history).filter((entry) => (entry.model_version ?? null) === modelVersion);
}

function scoreChartFromHistory(history, snapshot) {
  const label = assessmentLabel(snapshot);
  return {
    title: `${label} publication history`,
    subtitle: `Only publications from model ${snapshot?.model?.version ?? "unversioned"} are shown.`,
    unit: `${label} (%)`,
    series: [{
      label,
      points: history
        .filter((entry) => finite(entry.primary_score) !== null && entry.as_of)
        .map((entry) => ({ date: entry.as_of, value: finite(entry.primary_score), stance: entry.stance })),
    }],
  };
}

function renderHeadlines(snapshot) {
  const container = byId("headline-list");
  clearNode(container);
  const headlines = normalizeArray(snapshot.news?.recent_headlines).slice(0, 6);
  if (!headlines.length) {
    appendText(
      container,
      "li",
      snapshot.news?.headline_feed_available === false
        ? "Headline feed unavailable; no absence-of-news inference is made."
        : "No recent headlines were captured by the available feed.",
      "empty-state",
    );
    return;
  }
  headlines.forEach((headline) => {
    const item = document.createElement("li");
    const link = document.createElement("a");
    link.textContent = headline.title ?? "Untitled headline";
    const href = safeExternalHref(headline.link);
    if (href) {
      link.href = href;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      const external = appendText(link, "span", "↗", "external");
      external.setAttribute("aria-hidden", "true");
      appendText(link, "span", " (opens in a new tab)", "sr-only");
    } else {
      link.removeAttribute("href");
      link.setAttribute("aria-disabled", "true");
    }
    item.appendChild(link);
    const published = appendText(item, "time", formatDate(headline.published_at));
    const publishedDate = new Date(headline.published_at);
    if (!Number.isNaN(publishedDate.getTime())) published.dateTime = publishedDate.toISOString();
    container.appendChild(item);
  });
}

function storageGet(key) {
  try { return window.localStorage.getItem(key); } catch (error) { console.warn("Local storage unavailable", error); return null; }
}

function storageSet(key, value) {
  try { window.localStorage.setItem(key, value); return true; } catch (error) { console.warn("Local storage unavailable", error); return false; }
}

function wireNotes(snapshot) {
  const field = byId("local-note");
  const existing = storageGet(NOTE_KEY);
  if (existing) {
    field.value = existing;
    setText("note-status", "Saved locally in this browser.");
  }
  document.querySelector("[data-save-note]").addEventListener("click", () => {
    setText("note-status", storageSet(NOTE_KEY, field.value) ? "Saved locally in this browser." : "Browser storage is unavailable.");
  });
  document.querySelector("[data-export-note]").addEventListener("click", () => {
    const payload = { exported_at: new Date().toISOString(), forecast_id: snapshot.forecast_id ?? null, snapshot_generated_at: snapshot.generated_at, horizon: activeHorizon, note: field.value };
    let url = null;
    let link = null;
    try {
      if (typeof URL.createObjectURL !== "function") throw new Error("Object URL export is unsupported");
      url = URL.createObjectURL(new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" }));
      link = document.createElement("a");
      link.href = url;
      link.download = "fx-try-risk-note.json";
      document.body.appendChild(link);
      link.click();
      setText("note-status", "JSON export prepared. Your note remains stored only in this browser.");
    } catch (error) {
      console.warn("Note export unavailable", error);
      setText("note-status", "JSON export is unavailable in this browser context.");
    } finally {
      if (link) link.remove();
      if (url && typeof URL.revokeObjectURL === "function") {
        window.setTimeout(() => URL.revokeObjectURL(url), 0);
      }
    }
  });
}

function renderSnapshot(snapshot, history) {
  activeSnapshot = snapshot;
  activeHorizon = snapshot.primary_horizon ?? horizons(snapshot)[0];
  const briefing = snapshot.briefing ?? {};
  const model = snapshot.model ?? {};
  setText("house-call-title", briefing.stance ?? snapshot.stance ?? "Assessment published");
  setText("house-call-text", briefing.house_call ?? snapshot.headline ?? "No house assessment was published.");
  setText("generated-at", `Published ${formatDate(snapshot.generated_at, true)} · cutoff ${formatDate(snapshot.data_cutoff ?? snapshot.generated_at)}`);
  const health = snapshot.data_health ?? {};
  const availableSources = finite(health.available_source_count ?? health.fresh_count);
  const totalSources = finite(health.total_source_count);
  const coverage = finite(health.coverage_ratio);
  setText(
    "briefing-coverage",
    coverage === null
      ? "not published"
      : `${formatNumber(coverage * 100, 0)}%${availableSources !== null && totalSources !== null ? ` (${formatNumber(availableSources, 0)}/${formatNumber(totalSources, 0)})` : ""}`,
  );
  setText("briefing-caveat", capitalize(snapshot.data_health?.status ?? briefing.caveat_severity ?? "unknown"));
  setText("model-tag", `${model.label ?? model.name ?? (isCalibrated(snapshot) ? "Calibrated model" : "Uncalibrated model")} ${model.version ?? ""}`.trim());

  renderHorizonSelector(snapshot);
  renderAuthorityBadges(snapshot);
  updateHorizon(activeHorizon);
  renderTriggers(snapshot);
  renderExpertView(snapshot);
  renderCalibration(snapshot);
  renderHealth(snapshot);

  const spot = snapshot.market?.usd_try ?? {};
  setText("quick-spot", formatNumber(spot.latest, 4));
  setText("quick-spot-date", `TRY per USD · ${sourceObservationText(snapshot, [["EUR/TRY", "ecb_eurtry"], ["EUR/USD", "ecb_eurusd"]]).replace("Sources · ", "")}`);
  setText("quick-spot-change", formatChange(spot.change_20d));
  setText("quick-spot-change-date", sourceObservationText(snapshot, [["EUR/TRY", "ecb_eurtry"], ["EUR/USD", "ecb_eurusd"]]).replace("Sources · ", ""));
  setText("quick-peer-gap", formatSigned(snapshot.market?.try_gap_20d, " pp"));
  setText("quick-peer-date", sourceObservationText(snapshot, [["BRL", "ecb_eurbrl"], ["HUF", "ecb_eurhuf"], ["PLN", "ecb_eurpln"], ["ZAR", "ecb_eurzar"]]).replace("Sources · ", ""));
  setText("quick-policy", finite(snapshot.macro?.turkey?.policy_rate) === null ? "n/a" : `${formatNumber(snapshot.macro.turkey.policy_rate, 2)}%`);
  setText("quick-policy-date", sourceObservationText(snapshot, [["CBRT", "cbrt_policy_rate"]]).replace("Sources · ", ""));

  setText("market-regime", snapshot.market?.regime_label ?? "Market regime unavailable");
  setText("macro-regime", snapshot.macro?.regime_label ?? "Macro regime unavailable");
  setText("market-freshness", sourceObservationText(snapshot, [["ECB EUR/TRY", "ecb_eurtry"], ["EUR/USD", "ecb_eurusd"]]).replace("Sources · ", ""));
  setText("market-lens-date", "Source-dated below");
  setText("macro-lens-date", "Source-dated below");
  renderMetricGrid("market-metrics", [
    { label: "USD/TRY", value: formatNumber(spot.latest, 4), detail: `TRY per USD · 5-session ${formatChange(spot.change_5d)} · 20-session ${formatChange(spot.change_20d)}`, observationText: sourceObservationText(snapshot, [["ECB EUR/TRY", "ecb_eurtry"], ["ECB EUR/USD", "ecb_eurusd"]]) },
    { label: "TRY vs peer basket", value: formatSigned(snapshot.market?.try_gap_20d, " pp"), detail: `20-session gap · peer basket move ${formatChange(snapshot.market?.peer_avg_20d)}`, observationText: sourceObservationText(snapshot, [["BRL", "ecb_eurbrl"], ["HUF", "ecb_eurhuf"], ["PLN", "ecb_eurpln"], ["ZAR", "ecb_eurzar"]]) },
    { label: "VIX / VXEEM", value: `${formatNumber(snapshot.market?.volatility?.VIX, 2)} / ${formatNumber(snapshot.market?.volatility?.VXEEM, 2)}`, detail: `Index points · VVIX ${formatNumber(snapshot.market?.volatility?.VVIX, 2)} · OVX ${formatNumber(snapshot.market?.volatility?.OVX, 2)}`, observationText: sourceObservationText(snapshot, [["VIX", "cboe_vix"], ["VXEEM", "cboe_vxeem"], ["VVIX", "cboe_vvix"], ["OVX", "cboe_ovx"]]) },
    { label: "Market / vol pressure", value: `${formatNumber(snapshot.market?.scores?.market_pressure, 1)} / ${formatNumber(snapshot.market?.scores?.volatility_pressure, 1)}`, detail: "0–100 contextual lens scores; higher means more depreciation pressure", observationText: sourceObservationText(snapshot, [["ECB FX", "ecb_eurtry"], ["Cboe VIX", "cboe_vix"]]) },
  ]);
  renderMetricGrid("macro-metrics", [
    { label: "Fed funds / US 2Y", value: `${formatNumber(snapshot.macro?.global?.fed_funds, 2)}% / ${formatNumber(snapshot.macro?.global?.us_2y, 2)}%`, detail: `Percent p.a. · broad dollar 20-session ${formatChange(snapshot.macro?.global?.broad_dollar_change_20d)}`, observationText: sourceObservationText(snapshot, [["Fed funds", "fred_fedfunds"], ["US 2Y", "fred_dgs2"], ["Broad dollar", "fred_dtwexbgs"]]) },
    { label: "CBRT policy rate", value: `${formatNumber(snapshot.macro?.turkey?.policy_rate, 2)}%`, detail: "One-week repo rate, percent p.a.", observationText: sourceObservationText(snapshot, [["CBRT policy rate", "cbrt_policy_rate"]]) },
    { label: "Official reserve assets", value: finite(snapshot.macro?.turkey?.official_reserve_assets) === null ? "n/a" : `$${formatNumber(snapshot.macro.turkey.official_reserve_assets / 1000, 1)}bn`, detail: `USD billions · latest four-week change ${formatChange(snapshot.macro?.turkey?.official_reserve_assets_change_4w)}`, observationText: sourceObservationText(snapshot, [["CBRT reserves", "cbrt_reserves"]]) },
    { label: "Headline / chatter load", value: `${formatNumber(snapshot.news?.headline_count_14d, 0)} / ${formatNumber(snapshot.news?.chatter_count_14d, 0)}`, detail: "Items captured over the latest 14 days", observationText: sourceObservationText(snapshot, [["Google News", "google_news_rss"], ["Reddit", "reddit_rss"]]) },
  ]);

  const currentModelHistory = sameModelHistory(history, snapshot);
  renderLineChart("market-chart", "market-chart-legend", "market-chart-meta", "market-chart-table", snapshot.charts?.market_trend, "%");
  renderLineChart("score-chart", "score-chart-legend", "score-chart-meta", "score-chart-table", scoreChartFromHistory(currentModelHistory, snapshot), "%");
  renderHistory(currentModelHistory);
  renderHeadlines(snapshot);
  wireNotes(snapshot);
}

async function main() {
  try {
    const [snapshot, history] = await Promise.all([loadJson("./data/latest.json"), loadJson("./data/history.json")]);
    renderSnapshot(snapshot, history);
  } catch (error) {
    setText("house-call-title", "Snapshot unavailable");
    setText("house-call-text", "The published snapshot could not be loaded. If previewing locally, use an HTTP server instead of opening the file directly.");
    setText("primary-score", "—");
    setText("primary-meta", "No assessment available");
    setText("data-alert-title", "Data load failed");
    setText("data-alert-detail", "The interface could not retrieve the latest evidence pack.");
    byId("data-alert").className = "data-alert critical";
    console.error(error);
  }
}

document.addEventListener("DOMContentLoaded", main);
