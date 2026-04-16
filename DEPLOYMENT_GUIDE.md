# Deployment Guide

This app is designed to run well as a public local workstation first, and then move to a small deployment with minimal extra infrastructure.

## Recommended Deployment Profile

- Keep the app behind HTTPS.
- Set `FX_ENVIRONMENT=production`.
- Set a strong `FX_SESSION_SECRET`.
- Keep storage local for `v1` unless you truly need shared multi-device access.
- Use the included `compose.yaml` plus `deploy/Caddyfile` if you want the smallest "usable from anywhere" setup.

## Minimal Environment

```env
FX_ENVIRONMENT=production
FX_SESSION_SECRET=replace-with-a-long-random-secret
FX_ALLOWED_HOSTS=your-domain.com,127.0.0.1,localhost
FX_HOST=0.0.0.0
FX_PORT=8000
FX_RELOAD=false
FX_FORWARDED_ALLOW_IPS=127.0.0.1
FX_SECURE_COOKIES=true
```

## Health Endpoints

- `GET /healthz`
  Returns a lightweight liveness check.
- `GET /readyz`
  Returns readiness state for storage and database access.

These endpoints stay public so a reverse proxy or host platform can monitor the service.

## Public Access

The app now runs publicly by default.

- browser users open the root URL directly
- reports open directly from the app
- `/healthz` and `/readyz` remain public for monitoring

If you ever want to add restrictions later, the clean place to do it is at the reverse proxy or host layer rather than inside the app itself.

## Local Run

From the project root:

```powershell
.\start.ps1
```

For a local production-mode smoke test:

```powershell
.\start-production.ps1
```

If you are testing over plain `http://127.0.0.1` instead of local HTTPS, set:

```env
FX_SECURE_COOKIES=false
```

Only use that override for local smoke testing. Keep it `true` for deployed HTTPS usage.

Then open:

```text
http://127.0.0.1:8000
```

## Container Deployment

1. Copy `.env.production.example` to `.env.production`
2. Set `FX_SESSION_SECRET` and `FX_ALLOWED_HOSTS`
3. Change `deploy/Caddyfile` from `your-domain.example.com` to your real domain
4. Start the stack:

```powershell
docker compose up -d --build
```

This stack keeps the architecture intentionally small:

- `app`: the FastAPI workstation with SQLite and local artifacts
- `caddy`: HTTPS termination and reverse proxy

Your persistent app state stays in the mounted `data/` folder.

## Reverse Proxy Notes

- Caddy will terminate TLS and proxy traffic to the app on port `8000`.
- Keep `FX_ALLOWED_HOSTS` aligned with the public domain you place in `deploy/Caddyfile`.
- If you later use a different reverse proxy, keep `/healthz` and `/readyz` public for monitoring.

## Deployment Notes

- SQLite is fine for `v1` public use.
- Keep the `data/` directory on persistent storage.
- If you later need shared access across devices, move the data layer behind a managed database only when that overhead is justified.
- If you place the app behind a reverse proxy, preserve the host header and terminate TLS at the proxy.

## Cross-Device QA Checklist

- phone width: landing and cycle pages stay readable without horizontal scroll
- tablet width: sidebar compresses cleanly and the briefing rail still wraps
- desktop width: follow-up delta and trust sections stay readable side by side
- `Quick Read`: hides heavy workup sections but keeps the answer, watchlist, glossary, and trust cards visible
- `Full Workup`: restores evidence, rounds, and operations without layout breakage
- PDF and HTML reports both open directly from the app

## Final Pre-Deploy Checklist

- `ruff check app tests`
- `pytest -q`
- confirm `/readyz` reports `ready`
- confirm the homepage opens directly
- confirm PDF and HTML reports open correctly
- confirm mobile browser layout still reads well in `Quick Read`
