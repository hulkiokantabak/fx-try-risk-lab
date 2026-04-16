# Deployment Guide

This project now has one primary delivery model:

- a static GitHub Pages site for normal browser use

The goal is to keep operation simple. Users should open one browser link and read the latest published TRY risk snapshot without spinning up a server.

## Primary Browser Link

Once GitHub Pages is enabled for the repository, the browser link is:

`https://hulkiokantabak.github.io/fx-try-risk-lab/`

## How The Lean Browser Model Works

1. `scripts/build_browser_data.py` fetches public data from the current source list.
2. It writes the latest snapshot to `docs/data/latest.json`.
3. It appends or updates the published estimate history in `docs/data/history.json`.
4. The static site in `docs/` reads those files in the browser.
5. GitHub Pages serves the `docs/` folder as a normal website.

## Main Files

- `docs/index.html`: browser app shell
- `docs/style.css`: browser styling
- `docs/app.js`: client-side rendering and local notes
- `docs/data/latest.json`: latest published risk snapshot
- `docs/data/history.json`: published estimate history
- `scripts/build_browser_data.py`: snapshot generator
- `scripts/validate_browser_bundle.py`: lightweight static validator
- `.github/workflows/refresh-browser-data.yml`: scheduled data refresh

## Daily Refresh Flow

The scheduled workflow:

- runs on weekday mornings
- rebuilds the static data files
- validates the browser bundle
- commits `docs/data` changes back to `main`

The normal CI workflow stays intentionally lean and validates the published browser bundle instead of booting the older server stack.

## Local Preview

You can preview the browser version locally without running FastAPI:

1. Run `.\start-browser.ps1`
2. Open `http://127.0.0.1:8080`

That script rebuilds the snapshot first, then serves `docs/` through a tiny local HTTP server so the browser app can load its JSON correctly.

## Source Strategy

The lean browser edition prefers simple public feeds:

- ECB exchange rates
- Cboe volatility CSV files
- FRED graph CSV files when available
- CBRT public policy-rate and reserve pages
- Google News RSS

Some public feeds may be slow, partially unavailable, or temporarily blocked. The snapshot builder is designed to degrade gracefully and publish a usable neutral fallback instead of failing the whole site.

## GitHub Reality Check

GitHub Pages can host this project because the browser app is now static.

GitHub Pages cannot host the older FastAPI server directly. That is why the product was restructured around prebuilt JSON snapshots and a static browser interface.

## Legacy Server Path

The older FastAPI workstation and earlier product-design material still exist in `legacy/`, but they are archived there so the main repository surface stays browser-first and easy to understand.
