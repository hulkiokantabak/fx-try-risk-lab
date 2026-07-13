# Deployment Guide

FX TRY Risk Lab is deployed as a static GitHub Pages application. There is no
production application server and no runtime secret in the browser.

## Production flow

The scheduled refresh and normal product deployment are deliberately separate:

1. **Refresh Browser Data** checks out `main`, runs the snapshot builder, then
   runs unit, semantic, and JavaScript checks.
2. If public data changed, it creates one bot commit and pushes it to `main`.
3. That push triggers **Deploy Pages** exactly once.
4. **Deploy Pages** checks out the immutable triggering SHA, validates the
   bundle again, uploads `docs/`, and deploys it through the protected
   `github-pages` environment.

There is no `workflow_run` deployment trigger. That avoids duplicate deployment
runs and avoids accidentally deploying the pre-refresh SHA. Concurrency groups
cancel superseded CI/Page runs while never cancelling an in-progress data
refresh. Every job has a bounded timeout.

## One-time repository setup

1. In **Settings → Pages**, choose **GitHub Actions** as the source.
2. Keep the `github-pages` environment and its deployment history enabled.
3. Enable private vulnerability reporting and CodeQL/default code-scanning
   alerts under **Settings → Code security** where the repository plan permits.
4. Keep branch protection on `main` appropriate to the repository's maintenance
   model. Required checks should include `CI`.

No custom access token is required. The refresh workflow receives scoped
`contents: write`; Pages deployment uses scoped `pages: write` and
`id-token: write`. All other workflow permissions default to read-only.

## Local quality gate

Run these checks before a release:

```bash
python -m compileall -q scripts tests
python -m unittest discover -s tests -v
python scripts/validate_browser_bundle.py
node --check docs/app.js
```

To preview existing published artifacts without changing them:

```bash
python -m http.server 8080 --directory docs
```

Then open <http://127.0.0.1:8080>. `start-browser.ps1` also runs the networked
builder, so use it only when a refresh is intended.

## Publication contract

The bundle validator checks more than file presence. It verifies finite numeric
ranges, exact horizon alignment, event and model metadata, calibration labels,
source health and freshness, chronological unique history/ledger entries, and
coherence among the latest snapshot, history tail, cache references, and ledger
tail. It also rejects high-risk JavaScript rendering patterns.

Versioned releases must preserve:

- `docs/data/latest.json`: the current decision surface;
- `docs/data/history.json`: chronological public snapshot history;
- `docs/data/source_cache.json`: last-known source payloads, never presented as
  live without disclosure;
- `docs/data/forecast_ledger.json`: immutable issued forecasts plus append-only,
  separately identified terminal and path resolutions; and
- `docs/data/expert-latest.json`: the optional expert archive, displayed only
  when its forecast, model, event, cutoff, and evidence identities exactly bind
  to the current snapshot.

Schema and model changes should advance their versions. A model release must
state whether the output is an uncalibrated risk index, an experimental
probability, or a calibrated probability supported by out-of-sample evidence.

## Source degradation

A source outage is not automatically a deployment failure. The builder may use
a valid last-known observation when the model allows it, but the snapshot must
identify its age and cached state. If freshness, coverage, or minimum-row gates
fail, the affected lens is excluded or the forecast is withheld/degraded; it is
not silently replaced with a neutral score.

When several critical sources are unavailable, prefer publishing an explicit
`insufficient data` state over publishing a precise-looking estimate.

## Recovery and rollback

- **Bad UI/code deploy:** revert the responsible commit. The revert push will
  create a new, auditable Pages deployment.
- **Bad current snapshot:** publish a correction with a new forecast ID and a
  visible note. Do not rewrite an already issued forecast.
- **Compromised source:** disable that adapter, mark it unavailable, rebuild,
  and document the incident.
- **Exposed secret:** revoke it first. Treat git history as exposed even after a
  deletion commit.
- **Failed refresh push:** inspect concurrent changes, rerun from current
  `main`, and never force-push automated data.

GitHub retains workflow and environment deployment records. The repository
history remains the source of truth for what was publicly deployed.
