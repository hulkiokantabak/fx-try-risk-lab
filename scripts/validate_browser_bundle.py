from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def main() -> None:
    latest_path = DOCS / "data" / "latest.json"
    history_path = DOCS / "data" / "history.json"
    index_path = DOCS / "index.html"
    app_js_path = DOCS / "app.js"
    style_path = DOCS / "style.css"

    for path in [latest_path, history_path, index_path, app_js_path, style_path]:
        require(path.exists(), f"Missing required browser file: {path}")

    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    history = json.loads(history_path.read_text(encoding="utf-8"))
    index_html = index_path.read_text(encoding="utf-8")
    app_js = app_js_path.read_text(encoding="utf-8")

    required_latest_keys = {
        "generated_at",
        "primary_horizon",
        "thresholds",
        "curve",
        "primary_score",
        "headline",
        "summary",
        "market",
        "macro",
        "news",
        "reasons",
        "watchlist",
        "history_entry",
    }
    require(required_latest_keys.issubset(latest), "latest.json is missing required top-level keys")
    require(isinstance(history, list) and history, "history.json must contain at least one snapshot")

    curve = latest["curve"]
    thresholds = latest["thresholds"]
    require(isinstance(curve, dict) and curve, "latest.json curve must be a non-empty object")
    require(set(curve) == set(thresholds), "curve and threshold horizons must match")
    require(latest["primary_horizon"] in curve, "primary_horizon must exist in curve")

    summary = latest["summary"]
    require(
        {"deck", "primary_message", "market_message", "macro_message", "news_message"}.issubset(summary),
        "latest.json summary is missing required keys",
    )
    require(isinstance(latest["reasons"], list) and latest["reasons"], "latest.json reasons must be non-empty")
    require(isinstance(latest["watchlist"], list) and latest["watchlist"], "latest.json watchlist must be non-empty")

    newest_history = history[-1]
    require(newest_history["as_of"] == latest["history_entry"]["as_of"], "history tail does not match latest history_entry")
    require(
        newest_history["primary_score"] == latest["history_entry"]["primary_score"],
        "history tail primary_score does not match latest history_entry",
    )

    require("./style.css" in index_html, "index.html must load the browser stylesheet")
    require("./app.js" in index_html, "index.html must load the browser script")
    require("./data/latest.json" in app_js, "app.js must request the latest snapshot JSON")
    require("./data/history.json" in app_js, "app.js must request the history JSON")

    print("Browser bundle validation passed.")


if __name__ == "__main__":
    main()
