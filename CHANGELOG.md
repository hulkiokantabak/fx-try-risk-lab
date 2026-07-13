# Changelog

## 2026-07-13

### Auditable probability-model rebuild

- replaced the heuristic score with purged, walk-forward empirical probability
  forecasts for both exact-terminal and any-reference-fix threshold events
- published horizon-level calibration diagnostics, uncertainty intervals,
  challenger estimates, authority labels, and target-purged climatology skill
- added an append-only, cryptographically identified v3 forecast ledger with
  separate terminal and path-resolution events
- added a version-bound expert archive that preserves individual curves,
  fixed horizon-specific aggregation weights, dissent, stress cases, and the
  complete five-round review record
- rebuilt the browser experience as an accessible, responsive research monitor
  with explicit model, expert, source-health, and limitation surfaces
- hardened source ingestion, archive parsing, browser rendering, GitHub Actions,
  and publication validation; expanded financial, content, compatibility, and
  security regression coverage

## 2026-04-16

### Final v1 polish

- added a browser-facing methodology page
- added lightweight market-trend and score-history charts
- added last-good source caching so flaky public feeds can fall back more gracefully
- tightened browser security with a content security policy and referrer policy
- moved GitHub Pages from legacy mode to an explicit Actions workflow
- expanded the GitHub README for public use

### Browser-first release

- restructured the project around a GitHub Pages static app
- published a briefing-first output with `House Call`, `Why This Read`, and `What Would Change It`
- improved mobile layout, safe link handling, and browser-side note handling
- simplified deployment so normal use is just a public browser link
