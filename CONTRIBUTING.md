# Contributing

Thanks for contributing to FX TRY Risk Lab.

## Workflow

1. Fork the repo or create a feature branch from `main`.
2. Make focused changes with clear reasoning.
3. Run:
   - `python scripts/validate_browser_bundle.py`
   - `python scripts/build_browser_data.py`
4. Open a pull request with:
   - what changed
   - why it changed
   - how you verified it

## Development Notes

- Keep the browser experience simple.
- Prefer public data sources over premium assumptions.
- Preserve the static GitHub Pages delivery path.
- Avoid committing local secrets or random machine-specific files.

## Scope

Good contributions include:

- new public-source adapters
- better browser readability and explanation
- stronger snapshot validation or resilience
- UI clarity improvements that keep the one-link browser workflow simple
- docs improvements
