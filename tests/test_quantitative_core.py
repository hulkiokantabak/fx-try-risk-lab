from __future__ import annotations

import importlib.util
import json
import math
import tempfile
import unittest
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from risklab.forecast import (
    HORIZON_SESSIONS,
    MODEL_VERSION,
    FeatureRow,
    LabelledRow,
    _feature_rows,
    _labelled_rows,
    _metrics,
    _uncertainty_interval,
    _walk_forward_backtest,
    build_empirical_forecast,
)
from risklab.ledger import update_forecast_ledger
from risklab.quality import DataQualityError, checksum, validate_series


ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("build_browser_data", ROOT / "scripts" / "build_browser_data.py")
builder = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = builder
SPEC.loader.exec_module(builder)


@dataclass(frozen=True)
class Point:
    observed_at: datetime
    value: float


def synthetic_points(count: int = 1_650) -> list[Point]:
    start = datetime(2019, 1, 2)
    points = []
    value = 5.25
    for index in range(count):
        # Deterministic drift, alternating regimes and a smooth volatility cycle.
        regime = 0.00055 if (index // 180) % 2 == 0 else 0.00115
        shock = math.sin(index / 11.0) * 0.0022 + math.sin(index / 47.0) * 0.0011
        value *= math.exp(regime + shock)
        points.append(Point(start + timedelta(days=index), value))
    return points


class ForecastTests(unittest.TestCase):
    def test_forecast_contract_and_probability_bounds(self) -> None:
        forecast = build_empirical_forecast(
            synthetic_points(),
            {"1w": 2, "1m": 5, "3m": 10, "6m": 15, "1y": 25},
        )
        self.assertEqual(forecast["model"]["version"], MODEL_VERSION)
        self.assertIsInstance(forecast["model"]["is_calibrated"], bool)
        self.assertEqual(set(forecast["horizons"]), set(HORIZON_SESSIONS))
        for horizon in forecast["horizons"].values():
            self.assertGreaterEqual(horizon["probability"], 0)
            self.assertLessEqual(horizon["probability"], 100)
            self.assertLessEqual(
                horizon["uncertainty"]["lower_probability"],
                horizon["probability"],
            )
            self.assertGreaterEqual(
                horizon["uncertainty"]["upper_probability"],
                horizon["probability"],
            )
            self.assertGreater(horizon["calibration"]["forecast_count"], 0)
            self.assertIn("moving-block bootstrap", horizon["uncertainty"]["method"])
            self.assertEqual(
                horizon["uncertainty"]["block_length_sessions"],
                horizon["sessions"],
            )

    def test_climatology_benchmark_is_stored_from_strictly_resolved_labels(self) -> None:
        points = synthetic_points(1_200)
        features = _feature_rows(points)
        labelled = _labelled_rows(points, features, sessions=66, threshold=10.0)
        predictions = _walk_forward_backtest(
            labelled,
            {row.index: row for row in features},
            sessions=66,
        )
        self.assertGreater(len(predictions), 10)
        for prediction in predictions:
            resolved_at_issue = [
                row for row in labelled if row.target_index < prediction.forecast_index
            ]
            expected = (
                sum(row.outcome for row in resolved_at_issue) + 1.0
            ) / (len(resolved_at_issue) + 2.0)
            self.assertAlmostEqual(prediction.climatology_probability, expected)

        expected_brier = sum(
            (prediction.climatology_probability - prediction.outcome) ** 2
            for prediction in predictions
        ) / len(predictions)
        metrics = _metrics(predictions)
        self.assertEqual(metrics["climatology_brier_score"], round(expected_brier, 4))
        self.assertIn("target_index before forecast_index", metrics["climatology_protocol"])

    def test_moving_block_interval_is_deterministic_and_retains_dependence(self) -> None:
        def training_rows(outcomes: list[int]) -> list[LabelledRow]:
            return [
                LabelledRow(
                    FeatureRow(
                        index=index,
                        observed_at=datetime(2020, 1, 1) + timedelta(days=index),
                        momentum_5d=0.0,
                        momentum_20d=0.0,
                        realized_volatility_20d=10.0,
                        acceleration=0.0,
                    ),
                    target_index=index + 50,
                    outcome=outcome,
                )
                for index, outcome in enumerate(outcomes, start=20)
            ]

        current = FeatureRow(
            index=1_000,
            observed_at=datetime(2026, 1, 1),
            momentum_5d=0.0,
            momentum_20d=0.0,
            realized_volatility_20d=10.0,
            acceleration=0.0,
        )
        clustered = training_rows([0] * 400 + [1] * 400)
        alternating = training_rows([0, 1] * 400)
        clustered_interval = _uncertainty_interval(
            0.5,
            raw_probability=0.5,
            current=current,
            training=clustered,
            earlier_predictions=[],
            sessions=50,
            as_of_index=1_001,
        )
        repeated_interval = _uncertainty_interval(
            0.5,
            raw_probability=0.5,
            current=current,
            training=clustered,
            earlier_predictions=[],
            sessions=50,
            as_of_index=1_001,
        )
        alternating_interval = _uncertainty_interval(
            0.5,
            raw_probability=0.5,
            current=current,
            training=alternating,
            earlier_predictions=[],
            sessions=50,
            as_of_index=1_001,
        )
        self.assertEqual(clustered_interval, repeated_interval)
        clustered_width = clustered_interval.upper - clustered_interval.lower
        alternating_width = alternating_interval.upper - alternating_interval.lower
        self.assertGreater(clustered_width, alternating_width * 3)

    def test_forecast_rejects_short_history(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 750"):
            build_empirical_forecast(
                synthetic_points(749),
                {"1w": 2, "1m": 5, "3m": 10, "6m": 15, "1y": 25},
            )


class DataQualityTests(unittest.TestCase):
    def test_empty_and_duplicate_series_are_invalid(self) -> None:
        with self.assertRaises(DataQualityError):
            validate_series([], minimum_count=1)
        duplicate = [Point(datetime(2026, 1, 1), 1.0), Point(datetime(2026, 1, 1), 1.1)]
        with self.assertRaises(DataQualityError):
            validate_series(duplicate, minimum_count=2)

    def test_fred_observation_date_header(self) -> None:
        csv_text = "observation_date,DGS2\n2026-07-08,4.25\n2026-07-09,4.20\n"
        with patch.object(builder, "fetch_text", return_value=csv_text):
            points = builder.fetch_fred_series("DGS2")
        self.assertEqual(len(points), 2)
        self.assertEqual(points[-1].value, 4.2)

    def test_invalid_fetch_does_not_replace_last_good_cache(self) -> None:
        points = synthetic_points(800)
        payload = builder.serialize_series(points)
        original_checksum = checksum(payload)
        now = datetime.now(UTC).replace(tzinfo=None)
        # Keep the cache observationally fresh for the fallback gate.
        shift = now - points[-1].observed_at
        points = [Point(point.observed_at + shift, point.value) for point in points]
        payload = builder.serialize_series(points)
        original_checksum = checksum(payload)
        cache = {
            "schema_version": "2.0",
            "sources": {
                "ecb_eurtry": {
                    "label": "ECB EUR/TRY",
                    "payload": payload,
                    "fetched_at": now.isoformat(),
                    "latest_observation": points[-1].observed_at.isoformat(),
                    "item_count": len(points),
                    "checksum_sha256": original_checksum,
                    "status": "fresh",
                }
            },
        }
        warnings: list[str] = []
        result = builder.try_fetch(
            "ECB EUR/TRY",
            lambda: [],
            [],
            warnings,
            cache,
            "ecb_eurtry",
            builder.serialize_series,
            builder.deserialize_series,
        )
        self.assertEqual(len(result), len(points))
        self.assertEqual(checksum(cache["sources"]["ecb_eurtry"]["payload"]), original_checksum)
        self.assertEqual(cache["sources"]["ecb_eurtry"]["status"], "cached_fallback")


class LedgerTests(unittest.TestCase):
    def test_ledger_is_idempotent_and_resolves_without_rewriting_issue(self) -> None:
        points = synthetic_points()
        issue_points = points[:-300]
        forecast = build_empirical_forecast(
            issue_points,
            {"1w": 2, "1m": 5, "3m": 10, "6m": 15, "1y": 25},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            ledger, forecast_id = update_forecast_ledger(
                path,
                forecast,
                issue_points,
                issued_at="2026-01-01T00:00:00Z",
            )
            self.assertEqual(len(ledger["events"]), 1)
            ledger, repeated_id = update_forecast_ledger(
                path,
                forecast,
                points,
                issued_at="2026-12-31T00:00:00Z",
            )
            self.assertEqual(forecast_id, repeated_id)
            issued = [event for event in ledger["events"] if event["event_type"] == "forecast_issued"]
            resolved = [event for event in ledger["events"] if event["event_type"] == "outcome_resolved"]
            self.assertEqual(len(issued), 1)
            self.assertEqual(len(resolved), 5)
            self.assertEqual(json.loads(path.read_text())["policy"]["retention"], "indefinite (never trimmed; exceeds one year)")


if __name__ == "__main__":
    unittest.main()
