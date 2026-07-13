# FX TRY Risk Lab

[![CI](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/ci.yml)
[![Deploy Pages](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/pages.yml/badge.svg)](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/pages.yml)
[![CodeQL](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/codeql.yml/badge.svg)](https://github.com/hulkiokantabak/fx-try-risk-lab/actions/workflows/codeql.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](./LICENSE)

FX TRY Risk Lab is a transparent public research monitor for horizon-specific
Turkish lira depreciation risk. It joins an empirical USD/TRY model, source
health, contextual macro/market evidence, uncertainty, and an append-only track
record in a static browser application.

- **Live site:** <https://hulkiokantabak.github.io/fx-try-risk-lab/>
- **Methodology:** <https://hulkiokantabak.github.io/fx-try-risk-lab/methodology.html>
- **Latest machine-readable snapshot:**
  <https://hulkiokantabak.github.io/fx-try-risk-lab/data/latest.json>
- **Repository:** <https://github.com/hulkiokantabak/fx-try-risk-lab>

No login, server startup, or paid feed is required to read the published view.

## What the assessment means

For each horizon, the model asks whether the future derived ECB USD/TRY daily
reference rate will be at or above the baseline rate plus a fixed threshold:

| Horizon | Target observations | TRY-depreciation event |
|---|---:|---:|
| 1 week | 5 ECB trading observations | USD/TRY increase ≥ 2% |
| 1 month | 22 | increase ≥ 5% |
| 3 months | 66 | increase ≥ 10% |
| 6 months | 132 | increase ≥ 15% |
| 1 year | 264 | increase ≥ 25% |

The baseline is the latest date on which the required ECB EUR/TRY and EUR/USD
reference observations are both available. USD/TRY is derived as EUR/TRY divided
by EUR/USD. The aligned series contains only common observation dates: `t + N`
means exactly the Nth later common ECB trading observation, not an approximate
calendar target. These are reference rates, not executable dealer quotes.

The output is always expressed as a percentage. The browser labels it an
**experimental probability estimate** while the full validation gate is not met;
it is not a validated forecast probability. A horizon passes only with at least
50 walk-forward forecasts, positive Brier skill versus expanding climatology,
and expected calibration error no greater than 0.100. The model-level gate
requires every horizon to pass.

## Quantitative method

The empirical model uses only lagged values available at the data cutoff:

- 5-session USD/TRY momentum;
- 20-session momentum;
- 20-session annualized realized volatility; and
- short-term acceleration relative to the 20-session trend.

It estimates a regime-conditioned historical event rate, shrinks it toward the
as-of climatology, and applies reliability recalibration using only earlier
forecasts whose target windows had already closed. Backtesting uses a strict
expanding window with target-window purging. Random train/test splits are not
used.

Every snapshot publishes the model version, exact event definition, data
cutoff, baseline, horizon curve, signed drivers, uncertainty intervals, and
backtest diagnostics. Brier score, log loss, calibration error, and Brier skill
against expanding climatology are reported by horizon when samples are
sufficient.

### What the model does not use

Current macro, reserve, volatility, and news values are contextual evidence; they
do not alter the empirical estimate. They lack the complete point-in-time release
histories needed to backtest them without look-ahead bias. Adding them to the
model requires timestamped historical vintages and new out-of-sample evidence.

## Backtest evidence versus live track record

These are intentionally separate:

- **Backtest:** a reproducible historical simulation used for research and
  model comparison. It can still be affected by regime change and research
  choices.
- **Forecast ledger:** an append-only event log of estimates actually issued by
  the application. When an exact target observation becomes available, a
  resolution event records the realized move and outcome without rewriting the
  original forecast.

Forecast IDs bind the data cutoff, model version, and event thresholds. The
ledger is the authoritative live record; forecasts issued and horizon outcomes
resolved are shown separately from horizon-level backtest sample sizes. Backtest
metrics are not represented as live performance.

## Data health and graceful degradation

The refresh process validates payload shape, minimum observations, chronology,
plausible values, and observation age before a source can be used. A last-good
cache is retained for public feeds, but it is used only if it independently
passes the same semantic and freshness gates.

Missing, empty, stale, or malformed input is marked unavailable—it is never
silently converted into neutral evidence. The public snapshot discloses each
source's status, observation date, age, cache use, and most recent error. If the
ECB inputs required by the forecast are unusable, publication is blocked instead
of manufacturing a precise-looking estimate.

Source families include ECB exchange-rate reference data, FRED global rates and
dollar measures, Cboe volatility indices, CBRT policy and reserve publications,
and public headline feeds. Only the ECB-derived USD/TRY history drives the
current statistical model.

## Expert layer

The project retains four explicit analytical roles from its FX Experts method:

- **Atlas:** global macro and cross-asset conditions;
- **Bosphorus:** Turkish policy, balance-sheet, and political economy;
- **Flow:** positioning, liquidity, and market microstructure; and
- **Vega:** model risk, calibration, uncertainty, and falsification.

When a structured expert round is published, every role receives the same frozen
evidence pack. The house view, confidence, and dissent remain visible. Expert
judgment is an overlay, not silently mixed into the statistical estimate; no
expert view is shown when a round has not been published.

## Static application architecture

The supported product is intentionally small:

- `risklab/`: empirical forecast, data-quality, and ledger mechanics;
- `scripts/build_browser_data.py`: fetches sources and produces public artifacts;
- `scripts/validate_browser_bundle.py`: network-free semantic and security gate;
- `docs/`: GitHub Pages application and published JSON;
- `tests/`: quantitative, ledger, parser, and bundle-contract tests;
- `.github/workflows/`: CI, source refresh, CodeQL, and one-path Pages deployment;
- `legacy/`: archived server-first prototype and original design material.

The scheduled job refreshes data on weekday mornings. If artifacts change, its
single bot commit triggers one Pages deployment of that immutable commit SHA.

## Local verification and preview

Run the network-free gate:

```bash
python -m compileall -q scripts tests
python -m unittest discover -s tests -v
python scripts/validate_browser_bundle.py
node --check docs/app.js
```

Preview the already-built site:

```bash
python -m http.server 8080 --directory docs
```

Then open <http://127.0.0.1:8080>. Running
`python scripts/build_browser_data.py` contacts public sources and intentionally
updates the data artifacts, so do not use it for a network-free test.

Windows users can run `start-browser.ps1` when they intentionally want both a
fresh rebuild and local preview.

## Model risk and appropriate use

This is a research and monitoring tool, not investment advice, a trading signal,
or a guarantee. Important limitations include public-feed delay and revision,
reference-rate versus executable-price differences, overlapping long-horizon
outcomes, estimation uncertainty, structural breaks, policy discontinuities,
and the limited age of the live ledger.

Use the result as one transparent input to judgment. Inspect the event definition,
source health, uncertainty, and track record before interpreting the headline
number.

## Security and contributions

The application is static and has no first-party account or user database.
Feed-derived content is rendered as text rather than raw HTML; a Content Security
Policy, strict referrer policy, semantic validator, CodeQL, and Dependabot reduce
the attack surface. Personal notes stay in browser local storage.

See [SECURITY.md](./SECURITY.md) for private vulnerability reporting,
[CONTRIBUTING.md](./CONTRIBUTING.md) for model and data standards,
[DEPLOYMENT_GUIDE.md](./DEPLOYMENT_GUIDE.md) for operations, and
[CHANGELOG.md](./CHANGELOG.md) for release history.

## License

Released under the [MIT License](./LICENSE).
