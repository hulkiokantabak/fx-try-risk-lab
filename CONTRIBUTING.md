# Contributing

Thanks for contributing to FX TRY Risk Lab.

## Workflow

1. Fork the repo or create a feature branch from `main`.
2. Make focused changes with clear reasoning.
3. Run:
   - `python -m ruff check app tests`
   - `python -m pytest -q`
4. Open a pull request with:
   - what changed
   - why it changed
   - how you verified it

## Development Notes

- Keep the app local-first.
- Prefer public data sources over premium assumptions.
- Preserve the evidence-first, dissent-friendly `FX Experts` workflow.
- Avoid committing local secrets, `.env.production`, or generated `data/`.

## Scope

Good contributions include:

- new public-source adapters
- better reporting/export polish
- backtesting and calibration improvements
- UI clarity improvements that preserve analyst workflow
- deployment/docs improvements
