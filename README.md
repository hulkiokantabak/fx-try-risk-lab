# FX TRY Risk Lab

[![CI](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)
[![Open in GitHub Codespaces](https://github.com/codespaces/badge.svg)](https://codespaces.new/hulkiokantabak/fx-try-risk-lab?quickstart=1)

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

## Use It From GitHub

The simplest GitHub-native browser workflow is now built in:

1. Click the `Open in GitHub Codespaces` button above
2. GitHub creates a browser-based development environment for the repo
3. The app installs automatically and starts on port `8000`
4. Codespaces opens the forwarded browser preview for you

This makes the program usable directly from GitHub, in a browser, without adding Render or another external app platform.

The repo includes a ready-made dev container so Codespaces knows how to install and run the workstation automatically.

## GitHub Limits

- GitHub repo: where people read, download, and contribute to the code
- GitHub Codespaces: the GitHub-native way to run this app in a browser
- permanent public website: still requires a real host outside plain GitHub repository storage

This project is a dynamic FastAPI app, so a repository by itself cannot become a 24/7 public website. GitHub can host the code, run CI, and launch the app in Codespaces, but it does not turn the repo itself into a permanent server.

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
