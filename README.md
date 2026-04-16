# FX TRY Risk Lab

[![CI](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)

FX TRY Risk Lab is a lean browser app for tracking Turkish lira depreciation risk from public macro, market, reserve, and headline signals.

## Open In A Browser

- Live site: `https://hulkiokantabak.github.io/fx-try-risk-lab/`
- Repo: `https://github.com/hulkiokantabak/fx-try-risk-lab`

This repo now treats GitHub Pages as the primary product surface. Anyone should be able to open the browser link without spinning up a server, creating a codespace, or installing Python.

## What It Does

- publishes a static browser snapshot of TRY depreciation risk
- shows the full horizon curve for `1w / 1m / 3m / 6m / 1y`
- explains the top pressure points and watch items in plain English
- stores your personal notes locally in your browser
- keeps a published history of previous model estimates

## Core Features

- GitHub Pages static app in `docs/`
- public-source snapshot builder in `scripts/build_browser_data.py`
- scheduled browser-data refresh workflow
- daily snapshot history in `docs/data/history.json`
- simple local notes with no login and no backend

## Local Quick Start

1. Run `.\start-browser.ps1`
2. Open `http://127.0.0.1:8080`
3. Or push to GitHub and use the Pages URL

## Data Sources

- ECB exchange rates
- Cboe volatility indices
- FRED rates and dollar series when available
- CBRT policy-rate and reserve pages
- Google News RSS
- optional social-chatter RSS fallback when available

## Lean Structure

- `docs/`: the browser app and published data
- `scripts/build_browser_data.py`: the snapshot builder
- `scripts/validate_browser_bundle.py`: the lightweight browser check
- `.github/workflows/refresh-browser-data.yml`: the scheduled refresh job
- `start-browser.ps1`: local one-command preview
- `legacy/`: archived server-first and design material, no longer needed for normal use

## Public Repo Notes

- the browser app is static and public
- routine data refreshes commit only `docs/data`
- CI ignores `docs/data` pushes so scheduled refreshes stay quiet

## Archived Material

The older FastAPI workstation, product specs, and skill-pack material were moved under `legacy/` so the root of the repository stays focused on the public browser app.

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](./CONTRIBUTING.md) for workflow notes and [SECURITY.md](./SECURITY.md) for responsible disclosure guidance.

## License

This project is released under the [MIT License](./LICENSE).
