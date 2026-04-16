const noteKey = "fx-try-risk-lab-note";

async function loadJson(path) {
  const response = await fetch(path, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`Failed to load ${path}`);
  }
  return response.json();
}

function renderCurve(snapshot) {
  const curveGrid = document.getElementById("curve-grid");
  curveGrid.innerHTML = "";

  for (const [horizon, score] of Object.entries(snapshot.curve)) {
    const card = document.createElement("article");
    card.className = "curve-card";
    if (horizon === snapshot.primary_horizon) {
      card.classList.add("primary");
    }
    const threshold = snapshot.thresholds[horizon];
    card.innerHTML = `
      <span class="eyebrow">${horizon}</span>
      <strong>${score.toFixed(1)}</strong>
      <p>Chance TRY weakens more than ${threshold}%.</p>
    `;
    curveGrid.appendChild(card);
  }
}

function renderReasons(snapshot) {
  const container = document.getElementById("reasons");
  container.innerHTML = "";
  snapshot.reasons.forEach((reason) => {
    const row = document.createElement("article");
    row.className = "reason";
    row.innerHTML = `
      <div class="reason-title">
        <span>${reason.title}</span>
        <span class="badge">${reason.score.toFixed(1)}</span>
      </div>
      <p class="reason-detail">${reason.detail}</p>
    `;
    container.appendChild(row);
  });
}

function renderWatchlist(snapshot) {
  const container = document.getElementById("watchlist");
  container.innerHTML = "";
  snapshot.watchlist.forEach((item) => {
    const li = document.createElement("li");
    li.textContent = item;
    container.appendChild(li);
  });
}

function renderMetricGrid(targetId, items) {
  const container = document.getElementById(targetId);
  container.innerHTML = "";
  items.forEach((item) => {
    const article = document.createElement("article");
    article.className = "metric";
    article.innerHTML = `
      <span class="eyebrow">${item.label}</span>
      <strong>${item.value}</strong>
      <p>${item.detail}</p>
    `;
    container.appendChild(article);
  });
}

function formatChange(value, digits = 2) {
  if (value === null || value === undefined) {
    return "n/a";
  }
  return `${value > 0 ? "+" : ""}${value.toFixed(digits)}%`;
}

function renderHistory(history) {
  const body = document.getElementById("history-body");
  body.innerHTML = "";
  history
    .slice()
    .reverse()
    .forEach((entry) => {
      const row = document.createElement("tr");
      row.innerHTML = `
        <td>${entry.as_of.slice(0, 10)}</td>
        <td>${entry.primary_horizon}</td>
        <td>${entry.primary_score.toFixed(1)}</td>
        <td>${entry.market_regime}</td>
        <td>${entry.macro_regime}</td>
      `;
      body.appendChild(row);
    });
}

function renderHeadlines(snapshot) {
  const container = document.getElementById("headline-list");
  container.innerHTML = "";
  snapshot.news.recent_headlines.forEach((headline) => {
    const li = document.createElement("li");
    const safeHref = headline.link || "#";
    li.innerHTML = `<a href="${safeHref}" target="_blank" rel="noreferrer">${headline.title}</a> <span class="stamp">(${headline.published_at})</span>`;
    container.appendChild(li);
  });
}

function restoreNote() {
  const field = document.getElementById("local-note");
  const existing = window.localStorage.getItem(noteKey);
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
    window.localStorage.setItem(noteKey, noteField.value);
    status.textContent = "Saved locally on this browser.";
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
    link.click();
    URL.revokeObjectURL(url);
  });
}

async function main() {
  try {
    const [snapshot, history] = await Promise.all([
      loadJson("./data/latest.json"),
      loadJson("./data/history.json"),
    ]);

    document.getElementById("deck").textContent = snapshot.summary.deck;
    document.getElementById("primary-score").textContent = snapshot.primary_score.toFixed(1);
    document.getElementById("primary-meta").textContent =
      `${snapshot.primary_horizon} primary horizon · ${snapshot.headline}`;
    document.getElementById("generated-at").textContent =
      `Last built ${snapshot.generated_at.replace("T", " ").replace("Z", " UTC")}`;

    renderCurve(snapshot);
    renderReasons(snapshot);
    renderWatchlist(snapshot);
    renderHistory(history);
    renderHeadlines(snapshot);

    document.getElementById("market-regime").textContent = snapshot.market.regime_label;
    document.getElementById("macro-regime").textContent = snapshot.macro.regime_label;

    renderMetricGrid("market-metrics", [
      {
        label: "USD/TRY",
        value: snapshot.market.usd_try.latest?.toFixed(4) ?? "n/a",
        detail: `5d ${formatChange(snapshot.market.usd_try.change_5d)} · 20d ${formatChange(snapshot.market.usd_try.change_20d)}`,
      },
      {
        label: "TRY vs peers",
        value: formatChange(snapshot.market.try_gap_20d),
        detail: `Peer basket 20d average is ${formatChange(snapshot.market.peer_avg_20d)}.`,
      },
      {
        label: "VIX / VXEEM",
        value: `${snapshot.market.volatility.VIX ?? "n/a"} / ${snapshot.market.volatility.VXEEM ?? "n/a"}`,
        detail: `VVIX ${snapshot.market.volatility.VVIX ?? "n/a"} · OVX ${snapshot.market.volatility.OVX ?? "n/a"}`,
      },
      {
        label: "Pressure scores",
        value: `${snapshot.market.scores.market_pressure.toFixed(1)} / ${snapshot.market.scores.volatility_pressure.toFixed(1)}`,
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
        value: snapshot.macro.turkey.policy_rate?.toFixed(2) ?? "n/a",
        detail: "CBRT one-week repo rate",
      },
      {
        label: "Official reserves",
        value: snapshot.macro.turkey.official_reserve_assets?.toFixed(1) ?? "n/a",
        detail: `Latest window ${formatChange(snapshot.macro.turkey.official_reserve_assets_change_4w)}`,
      },
      {
        label: "Headline load",
        value: `${snapshot.news.headline_count_14d} / ${snapshot.news.chatter_count_14d}`,
        detail: "Google News / social chatter in 14 days",
      },
    ]);

    restoreNote();
    wireNoteActions(snapshot);
  } catch (error) {
    document.getElementById("deck").textContent =
      "The browser snapshot could not be loaded. If you are previewing locally, use start-browser.ps1 or a simple HTTP server instead of opening index.html directly.";
    console.error(error);
  }
}

document.addEventListener("DOMContentLoaded", main);
