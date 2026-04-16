# FX TRY Risk Lab

[![CI](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/hulkiokantabak/fx-try-risk-lab)

FX TRY Risk Lab is a local-first research workstation for assessing Turkish lira depreciation risk with public macro, market, news, and debate-driven evidence.

## What It Does

- builds frozen assessment cycles with Round 0 evidence packs
- runs the `FX Experts` debate workflow across Rounds 1-4
- produces a house view, disagreement range, stress flags, and backtesting
- exports PDF-first assessment briefs with an HTML twin
- runs publicly by default once deployed; the GitHub repo hosts the code, not the live app

## Core Features

- public-source ingestion across CBRT, ECB, IMF, FRED, and market-volatility feeds
- frozen assessment cycles with replayable Round 0 evidence packs
- multi-agent `FX Experts` debate rounds with disagreement preserved instead of flattened
- follow-up cycle lineage, delta summaries, and realized-outcome backtesting
- analyst briefing mode with `Quick Read / Full Workup`
- PDF-first exports with HTML twins

## Local Quick Start

1. Create and activate a local virtual environment.
2. Install the project: `pip install -e .[dev]`
3. Start the app: `.\start.ps1`
4. Open [http://127.0.0.1:8000](http://127.0.0.1:8000)

## Deployment Quick Start

1. Copy `.env.production.example` to `.env.production`
2. Set your domain and session secret
3. Update `deploy/Caddyfile` with your real domain
4. Run `docker compose up -d --build`

## One-Click Permanent Hosting

The repo now includes a `render.yaml` blueprint for the simplest permanent public deployment path:

1. Open the `Deploy to Render` button above
2. Sign in to Render
3. Approve the service and persistent disk
4. Wait for the first deploy to finish

That creates a real hosted browser URL on Render instead of a temporary tunnel link. Because this app stores SQLite data and exported reports on disk, truly permanent hosting requires a service with persistent storage.

## GitHub vs Browser Link

- GitHub repo: where people read, download, and contribute to the code
- deployed app URL: the real browser link people use to run the workstation

This project is a dynamic FastAPI app, so GitHub alone does not create the live browser URL. You still need to deploy it to a host or run it from your own machine.

## Public Repo Notes

- the app is public-by-default and does not require an internal password wall
- local/dev data stays out of Git via `.gitignore`
- production-only secrets belong in `.env.production`, not in the repo

## Main Entry Points

- App runner: `python -m app.serve`
- Local dev: `.\start.ps1`
- Local production-mode smoke run: `.\start-production.ps1`
- Deployment guide: [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)

## Current Product Shape

- local-first SQLite storage
- public CBRT, ECB, IMF, FRED, and market-volatility ingestion
- follow-up cycle lineage and delta analysis
- analyst briefing mode with `Quick Read / Full Workup`
- health endpoints at `/healthz` and `/readyz`

## Deployment Model

The default production path is intentionally simple:

- one app container
- one Caddy reverse proxy for HTTPS
- one persistent `data/` volume

That keeps the app usable from anywhere without forcing a separate database or a complicated cloud stack on day one.

## Contributing

Contributions are welcome. Start with [CONTRIBUTING.md](./CONTRIBUTING.md) for workflow notes and [SECURITY.md](./SECURITY.md) for responsible disclosure guidance.

## License

This project is released under the [MIT License](./LICENSE).
