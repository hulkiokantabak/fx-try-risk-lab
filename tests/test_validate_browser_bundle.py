from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from scripts.validate_browser_bundle import (
    ValidationError,
    validate_bundle,
    validate_history,
    validate_latest,
    validate_ledger,
)


HORIZONS = {"1w": 10.0, "1m": 20.0, "3m": 30.0, "6m": 40.0, "1y": 50.0}
THRESHOLDS = {"1w": 2.0, "1m": 5.0, "3m": 10.0, "6m": 15.0, "1y": 25.0}


def history_entry(as_of: str = "2026-01-01T12:00:00Z") -> dict:
    return {
        "as_of": as_of,
        "primary_horizon": "1m",
        "primary_score": 20.0,
        "curve": copy.deepcopy(HORIZONS),
    }


def v1_snapshot() -> dict:
    entry = history_entry()
    return {
        "generated_at": entry["as_of"],
        "primary_horizon": "1m",
        "thresholds": copy.deepcopy(THRESHOLDS),
        "curve": copy.deepcopy(HORIZONS),
        "primary_score": 20.0,
        "headline": "Test snapshot",
        "briefing": {
            "stance": "Watch",
            "probability": 20.0,
            "primary_horizon": "1m",
            "confidence": "low",
            "caveat_severity": "high",
            "caveat_message": "Fixture only",
            "house_call": "Fixture only",
        },
        "summary": {
            "deck": "Fixture",
            "primary_message": "Fixture",
            "market_message": "Fixture",
            "macro_message": "Fixture",
            "news_message": "Fixture",
        },
        "why_read": [{"title": "Fixture"}],
        "trigger_cards": [{"title": "Fixture"}],
        "charts": {"market_trend": [], "score_history": []},
        "market": {},
        "macro": {},
        "news": {},
        "reasons": ["Fixture"],
        "watchlist": ["Fixture"],
        "history_entry": entry,
    }


def v2_snapshot() -> dict:
    snapshot = v1_snapshot()
    snapshot["generated_at"] = "2026-01-01T12:00:00Z"
    snapshot.update(
        {
            "schema_version": "2.0",
            "forecast_id": "fxtry-20260101T120000Z-v2",
            "data_cutoff": "2026-01-01",
            "model": {
                "name": "fixture",
                "version": "2.0.0",
                "status": "experimental",
                "method": "expanding-window fixture",
                "training_protocol": "past data only",
            },
            "event_definition": {
                "statement": "USD/TRY reaches the horizon threshold.",
                "thresholds_percent": copy.deepcopy(THRESHOLDS),
                "horizon_sessions": {"1w": 5, "1m": 22, "3m": 66, "6m": 132, "1y": 264},
                "baseline_rule": "latest common observation",
                "target_rule": "exact future session",
            },
            "baseline": {
                "pair": "USD/TRY",
                "value": 42.0,
                "observation_date": "2026-01-01",
                "source": "fixture",
                "price_type": "daily reference rate",
            },
            "uncertainty": {
                horizon: {
                    "lower_probability": max(0, score - 10),
                    "upper_probability": min(100, score + 10),
                    "level": 0.9,
                    "method": "fixture interval",
                    "effective_sample_size": 50,
                }
                for horizon, score in HORIZONS.items()
            },
            "data_health": {
                "overall_status": "degraded",
                "sources": [
                    {
                        "key": "ecb_eurtry",
                        "status": "cached_fallback",
                        "latest_observation": "2026-01-01",
                        "age_days": 0.0,
                        "used_cache": True,
                    }
                ],
            },
            "calibration": {
                "protocol": "strict expanding window",
                "metrics": {
                    horizon: {
                        "status": "available",
                        "forecast_count": 100,
                        "event_count": 20,
                        "brier_score": 0.18,
                        "log_loss": 0.52,
                        "climatology_brier_score": 0.2,
                        "brier_skill_vs_climatology": 0.1,
                        "calibration_error": 0.05,
                    }
                    for horizon in HORIZONS
                },
                "metric_definitions": {"brier_score": "lower is better"},
            },
        }
    )
    snapshot["history_entry"]["forecast_id"] = snapshot["forecast_id"]
    return snapshot


class LatestValidationTests(unittest.TestCase):
    def test_valid_versioned_snapshot(self) -> None:
        validate_latest(v2_snapshot())

    def test_non_finite_number_is_rejected(self) -> None:
        snapshot = v1_snapshot()
        snapshot["curve"]["1m"] = float("nan")
        with self.assertRaisesRegex(ValidationError, "non-finite"):
            validate_latest(snapshot)

    def test_horizon_mismatch_is_rejected(self) -> None:
        snapshot = v1_snapshot()
        del snapshot["thresholds"]["1y"]
        with self.assertRaisesRegex(ValidationError, "exactly"):
            validate_latest(snapshot)

    def test_baseline_must_match_cutoff(self) -> None:
        snapshot = v2_snapshot()
        snapshot["baseline"]["observation_date"] = "2025-12-31"
        with self.assertRaisesRegex(ValidationError, "must equal data_cutoff"):
            validate_latest(snapshot)

    def test_available_source_requires_freshness(self) -> None:
        snapshot = v2_snapshot()
        source = snapshot["data_health"]["sources"][0]
        source.pop("latest_observation")
        source.pop("age_days")
        with self.assertRaisesRegex(ValidationError, "must report freshness"):
            validate_latest(snapshot)


class HistoryAndLedgerTests(unittest.TestCase):
    def test_duplicate_history_timestamp_is_rejected(self) -> None:
        latest = v1_snapshot()
        entries = [history_entry(), history_entry()]
        with self.assertRaisesRegex(ValidationError, "strictly chronological"):
            validate_history(entries, latest)

    def test_duplicate_ledger_id_is_rejected(self) -> None:
        latest = v2_snapshot()
        entry = {
            "forecast_id": latest["forecast_id"],
            "issued_at": latest["generated_at"],
            "primary_horizon": "1m",
        }
        with self.assertRaisesRegex(ValidationError, "duplicate forecast_id"):
            validate_ledger([entry, copy.deepcopy(entry)], latest)


def write_static_shell(root: Path) -> None:
    (root / "docs" / "index.html").write_text(
        '<meta name="theme-color"><meta name="referrer">'
        '<meta http-equiv="Content-Security-Policy"><link href="./style.css">'
        '<a href="./methodology.html"></a><script src="./app.js"></script>',
        encoding="utf-8",
    )
    (root / "docs" / "app.js").write_text(
        "fetch('./data/latest.json'); fetch('./data/history.json');", encoding="utf-8"
    )
    for relative in ("docs/style.css", "docs/methodology.html", "CHANGELOG.md", "SECURITY.md"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")


class NetworkFreeBundleFixtureTests(unittest.TestCase):
    def test_minimal_static_bundle_validates_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "docs" / "data"
            data.mkdir(parents=True)
            latest = v1_snapshot()
            (data / "latest.json").write_text(json.dumps(latest), encoding="utf-8")
            (data / "history.json").write_text(json.dumps([latest["history_entry"]]), encoding="utf-8")
            (data / "source_cache.json").write_text(json.dumps({"fixture": []}), encoding="utf-8")
            write_static_shell(root)
            validate_bundle(root)

    def test_versioned_bundle_joins_latest_history_cache_and_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "docs" / "data"
            data.mkdir(parents=True)
            latest = v2_snapshot()
            forecast_id = latest["forecast_id"]
            horizon_payload = {
                horizon: {
                    "sessions": latest["event_definition"]["horizon_sessions"][horizon],
                    "threshold_percent": THRESHOLDS[horizon],
                    "probability": HORIZONS[horizon],
                    "uncertainty": latest["uncertainty"][horizon],
                }
                for horizon in HORIZONS
            }
            ledger = {
                "schema_version": "1.0",
                "policy": {"storage": "append-only"},
                "events": [
                    {
                        "event_type": "forecast_issued",
                        "event_id": f"{forecast_id}:issued",
                        "forecast_id": forecast_id,
                        "recorded_at": latest["generated_at"],
                        "model_version": latest["model"]["version"],
                        "data_cutoff": latest["data_cutoff"],
                        "baseline": latest["baseline"],
                        "horizons": horizon_payload,
                    }
                ],
            }
            (data / "latest.json").write_text(json.dumps(latest), encoding="utf-8")
            (data / "history.json").write_text(json.dumps([latest["history_entry"]]), encoding="utf-8")
            (data / "source_cache.json").write_text(
                json.dumps({"schema_version": "2.0", "sources": {"ecb_eurtry": {"payload": []}}}),
                encoding="utf-8",
            )
            (data / "forecast_ledger.json").write_text(json.dumps(ledger), encoding="utf-8")
            write_static_shell(root)
            validate_bundle(root)


if __name__ == "__main__":
    unittest.main()
