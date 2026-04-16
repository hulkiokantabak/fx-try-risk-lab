# FX TRY Risk Lab

[![CI](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/ci.yml)
[![Deploy Pages](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/pages.yml/badge.svg)](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/pages.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)

FX TRY Risk Lab is a lean browser app for tracking Turkish lira depreciation risk from public macro, market, reserve, and headline signals.

## Open In A Browser

- Live site: [https://hulkiokantabak.github.io/fx-try-risk-lab/](https://hulkiokantabak.github.io/fx-try-risk-lab/)
- Latest raw snapshot: [https://hulkiokantabak.github.io/fx-try-risk-lab/data/latest.json](https://hulkiokantabak.github.io/fx-try-risk-lab/data/latest.json)
- Repo: [https://github.com/hulkiokantabak/fx-try-risk-lab](https://github.com/hulkiokantabak/fx-try-risk-lab)

The repo is structured so normal use is simple: open the browser link and read the latest published view. No codespaces, server startup, or login wall is required.

## What The App Answers

The app is built to answer one practical question:

`How elevated is Turkish lira depreciation risk right now, why, and what would change the read?`

It does that with a simple briefing-first structure:

- `House Call`: the current overall read in plain language
- `Primary Probability`: the main risk score for the selected horizon
- `Why This Read`: the three main forces shaping the view
- `What Would Change It`: the key triggers that would materially shift the call
- `Full Brief`: the deeper curve, supporting evidence, caveats, history, headlines, and notes

## What You See In The Browser

The browser app is intentionally staged so the top answers come first and the research depth stays below the fold.

### 1. House Call

This is the plain-English summary of the current TRY risk view, such as:

- `Balanced but fragile`
- `Pressure building`
- `Acute stress`

### 2. Probability And Horizon

The large score is the primary event-probability read for the active horizon. The horizon curve below it keeps the full `1w / 1m / 3m / 6m / 1y` range visible so the app does not hide term-structure differences.

### 3. Why This Read

This section compresses the signal into three categories:

- `Pressure`
- `Support`
- `Unclear`

That keeps the app honest without forcing users to interpret a dashboard full of small boxes.

### 4. What Would Change It

These are the main watch triggers that would meaningfully move the view. This section is important because the app is meant to guide judgment, not only publish a score.

### 5. Full Brief

The lower sections keep the research depth available:

- horizon curve
- market and macro context
- caveats and warnings
- headline flow
- history
- personal notes stored in the local browser

## What The Risk Scores Mean

The app publishes a probability-style risk read rather than a vague index. In practical terms, the main score is meant to represent the chance that TRY weakens beyond the defined threshold for the chosen horizon.

Current default thresholds are:

- `1w`: `2%`
- `1m`: `5%`
- `3m`: `10%`
- `6m`: `15%`
- `1y`: `25%`

These thresholds are designed to be usable for public monitoring and can be recalibrated later if the project evolves.

## Data Sources

The browser edition uses public feeds and degrades gracefully when some are slow, blocked, or incomplete.

Main source families:

- ECB exchange rates
- Cboe volatility indices
- FRED rates and dollar series when available
- CBRT public policy-rate and reserve pages
- Google News RSS
- optional social-chatter RSS fallback when available

The latest snapshot always carries caveats and warnings when the data picture is incomplete.

## Trust Model And Limitations

This project is designed to be useful, transparent, and lightweight. It is not a trading signal engine and it is not a promise of predictive accuracy.

Important limits:

- public feeds can be delayed, incomplete, rate-limited, or blocked
- the app simplifies a complex FX problem into a readable public brief
- the score is only as good as the current public evidence set
- no static site can replace real-time institutional market data

This is best thought of as a disciplined public-monitoring tool, not a substitute for a professional execution desk.

## Local Quick Start

If you want to preview or rebuild the browser app locally:

1. Run `.\start-browser.ps1`
2. Open `http://127.0.0.1:8080`

That script rebuilds the published JSON snapshot first, validates the browser bundle, and then serves the `docs/` folder over a tiny local HTTP server.

## How Publishing Works

This repo is GitHub Pages first.

Publishing model:

1. `scripts/build_browser_data.py` builds the latest snapshot
2. it writes `docs/data/latest.json`
3. it updates `docs/data/history.json`
4. the browser app in `docs/` reads those files directly
5. GitHub Pages serves the result as a normal public website

## GitHub Automation

The repo uses two lightweight GitHub Actions workflows:

- `CI`: validates the browser bundle on pushes and pull requests
- `Deploy Pages`: publishes the static site to GitHub Pages

There is also a scheduled refresh workflow that updates `docs/data` on weekday mornings.

## Repository Structure

- `docs/`: public browser app and published data
- `docs/data/latest.json`: latest published snapshot
- `docs/data/history.json`: published estimate history
- `scripts/build_browser_data.py`: snapshot builder
- `scripts/validate_browser_bundle.py`: static bundle validator
- `.github/workflows/ci.yml`: main validation workflow
- `.github/workflows/pages.yml`: GitHub Pages deployment workflow
- `.github/workflows/refresh-browser-data.yml`: scheduled data refresh
- `start-browser.ps1`: one-command local preview
- `legacy/`: archived server-first and design material

## Security And Privacy

The public browser app is static. There is no live backend, user database, or server-side login flow.

Current browser hardening includes:

- no raw `innerHTML` rendering for feed-backed content
- safe link handling for external URLs
- a browser-side content security policy
- a strict referrer policy
- local notes are stored only in the user's own browser

If you find a security issue, please use [SECURITY.md](./SECURITY.md).

## Archived Material

The older FastAPI workstation, design specs, and skill-pack material still exist in `legacy/`, but they are archived there so the root of the repository stays focused on the public browser app.

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](./CONTRIBUTING.md) for workflow notes and [DEPLOYMENT_GUIDE.md](./DEPLOYMENT_GUIDE.md) for the browser publishing model.

## License

This project is released under the [MIT License](./LICENSE).
