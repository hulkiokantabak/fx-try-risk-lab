# Contributing

FX TRY Risk Lab welcomes focused improvements to data reliability, model
evaluation, explanation, accessibility, and the static browser experience.

## Development workflow

1. Branch from the current `main`.
2. Keep each change narrow enough to review and reproduce.
3. Run the network-free quality gate:

   ```bash
   python -m compileall -q scripts tests
   python -m unittest discover -s tests -v
   python scripts/validate_browser_bundle.py
   node --check docs/app.js
   ```

4. Preview `docs/` through a local HTTP server, not by opening the HTML as a
   `file://` URL. On Windows, `start-browser.ps1` performs a refresh and starts
   the server; only use it when intentionally updating public data.
5. Open a pull request that explains the user-visible result, data or model
   assumptions, failure behavior, and verification performed.

The unit tests and bundle validator are network-free. The snapshot builder is
not: it contacts public sources and can change every file under `docs/data/`.
Do not include incidental data refreshes in an unrelated pull request.

## Financial and model standards

- Define the forecast event precisely: pair, direction, horizon, threshold,
  observation convention, and issue/data-cutoff times.
- Call an output a **probability** only when its calibration is measured on
  genuinely out-of-sample forecasts and published with sample size and scoring
  evidence. Otherwise label it a risk index or experimental estimate.
- Compare every candidate model with a simple base-rate baseline. Prefer
  purged, walk-forward evaluation to random train/test splits for time series.
- Publish uncertainty, model version, calibration status, and data health next
  to the forecast.
- Never treat missing or stale input as a neutral observation without an
  explicit, documented model rule and warning.
- Do not tune on the public outcome ledger or rewrite old forecasts after their
  outcomes are known.

The four expert roles—Atlas, Bosphorus, Flow, and Vega—are an interpretive layer.
They must use the same frozen evidence pack, preserve dissent, and remain
visibly separate from the statistical baseline unless a documented combination
method has been validated out of sample.

## Adding or changing a source

A source adapter should include:

- a stable source identifier and authoritative URL;
- parser tests using local fixtures;
- explicit units, frequency, timezone, and observation date semantics;
- minimum row and recency requirements;
- a documented cached/stale/unavailable path; and
- source-health metadata that lets readers see degradation.

Never log response bodies that might contain credentials. Redirects and
external URLs must stay within an intentional allowlist. A successful HTTP
status is not sufficient validation.

## Forecast history and outcome ledger

Forecast identifiers are unique and immutable. History and ledger timestamps
must be strictly chronological. When an outcome becomes observable, append or
fill the outcome fields without changing the original issue time, model,
event, threshold, or estimate. Corrections require a visible correction record,
not silent replacement.

## Browser and editorial standards

- Keep essential interpretation available without relying on color alone.
- Preserve keyboard navigation, visible focus, reduced-motion support, readable
  contrast, and mobile layouts.
- Render feed-derived text with `textContent`/text nodes; do not use raw
  `innerHTML`, `eval`, `document.write`, or dynamic function construction.
- Put units and dates beside values. Distinguish observation date, forecast
  issue time, and page generation time.
- State limitations in the decision surface, not only in methodology notes.
- Use plain language and avoid implying certainty, trading advice, or causal
  proof that the evidence does not support.

## Pull-request checklist

- [ ] Network-free tests and bundle validation pass.
- [ ] JavaScript parses and the browser has been checked at mobile and desktop
      widths.
- [ ] New data has provenance, units, freshness, and failure behavior.
- [ ] Forecast/event/model schema changes include validator and test changes.
- [ ] No historical forecast was silently mutated.
- [ ] No secret, personal data, machine-specific file, or unrelated refresh is
      included.
- [ ] User-facing changes are documented.

Sensitive findings belong in a private advisory; see [SECURITY.md](./SECURITY.md).
