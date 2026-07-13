from __future__ import annotations

import copy
import json
import math
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from risklab.forecast import build_empirical_forecast
from risklab.ledger import LedgerIntegrityError, update_forecast_ledger
from scripts.validate_browser_bundle import ValidationError, validate_ledger


THRESHOLDS = {"1w": 2, "1m": 5, "3m": 10, "6m": 15, "1y": 25}


@dataclass(frozen=True)
class Point:
    observed_at: datetime
    value: float


def synthetic_points(count: int = 1_650) -> list[Point]:
    start = datetime(2019, 1, 2)
    value = 5.25
    points: list[Point] = []
    for index in range(count):
        regime = 0.00055 if (index // 180) % 2 == 0 else 0.00115
        shock = math.sin(index / 11.0) * 0.0022 + math.sin(index / 47.0) * 0.0011
        value *= math.exp(regime + shock)
        points.append(Point(start + timedelta(days=index), value))
    return points


def latest_for(forecast: dict, forecast_id: str) -> dict:
    return {
        "forecast_id": forecast_id,
        "data_cutoff": forecast["data_cutoff"],
        "baseline": copy.deepcopy(forecast["baseline"]),
        "model": copy.deepcopy(forecast["model"]),
        "curve": {
            horizon: specification["probability"]
            for horizon, specification in forecast["horizons"].items()
        },
        "thresholds": copy.deepcopy(forecast["event_definition"]["thresholds_percent"]),
        "event_definition": copy.deepcopy(forecast["event_definition"]),
        "uncertainty": {
            horizon: copy.deepcopy(specification["uncertainty"])
            for horizon, specification in forecast["horizons"].items()
        },
        "forecast": copy.deepcopy(forecast),
    }


class LedgerIdentityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.points = synthetic_points()
        cls.issue_points = cls.points[:-300]
        cls.forecast = build_empirical_forecast(cls.issue_points, THRESHOLDS)

    def test_identity_covers_baseline_and_horizon_curve(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            ledger, original_id = update_forecast_ledger(
                path,
                self.forecast,
                [],
                issued_at="2026-01-01T00:00:00Z",
            )

            revised_baseline = copy.deepcopy(self.forecast)
            revised_baseline["baseline"]["value"] += 0.000001
            revised_baseline["baseline"]["spot"] += 0.000001
            ledger, baseline_id = update_forecast_ledger(
                path,
                revised_baseline,
                [],
                issued_at="2026-01-02T00:00:00Z",
            )

            revised_curve = copy.deepcopy(self.forecast)
            revised_curve["horizons"]["1m"]["probability"] += 0.1
            ledger, curve_id = update_forecast_ledger(
                path,
                revised_curve,
                [],
                issued_at="2026-01-03T00:00:00Z",
            )

            self.assertEqual(len({original_id, baseline_id, curve_id}), 3)
            self.assertEqual(
                len([event for event in ledger["events"] if event["event_type"] == "forecast_issued"]),
                3,
            )

    def test_resolution_uses_immutable_issued_baseline(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            ledger, forecast_id = update_forecast_ledger(
                path,
                self.forecast,
                self.issue_points,
                issued_at="2026-01-01T00:00:00Z",
            )
            baseline_index = len(self.issue_points) - 1
            revised_points = list(self.points)
            original_baseline = float(self.forecast["baseline"]["value"])
            revised_points[baseline_index] = Point(
                revised_points[baseline_index].observed_at,
                original_baseline * 1.8,
            )
            ledger, repeated_id = update_forecast_ledger(
                path,
                self.forecast,
                revised_points,
                issued_at="2026-12-31T00:00:00Z",
            )

            self.assertEqual(repeated_id, forecast_id)
            resolution = next(
                event
                for event in ledger["events"]
                if event["event_type"] == "outcome_resolved" and event["horizon"] == "1m"
            )
            target = revised_points[baseline_index + self.forecast["horizons"]["1m"]["sessions"]]
            expected = round(((target.value / original_baseline) - 1.0) * 100.0, 6)
            revised_history_result = round(((target.value / revised_points[baseline_index].value) - 1.0) * 100.0, 6)
            self.assertEqual(resolution["realized_move_percent"], expected)
            self.assertNotEqual(resolution["realized_move_percent"], revised_history_result)
            validate_ledger(ledger, latest_for(self.forecast, forecast_id))

    def test_no_op_update_is_byte_stable_and_does_not_duplicate_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            first, forecast_id = update_forecast_ledger(
                path,
                self.forecast,
                self.issue_points,
                issued_at="2026-01-01T00:00:00Z",
            )
            before = path.read_bytes()
            repeated, repeated_id = update_forecast_ledger(
                path,
                self.forecast,
                self.issue_points,
                issued_at="2026-06-01T00:00:00Z",
            )
            self.assertEqual(repeated_id, forecast_id)
            self.assertEqual(repeated, first)
            self.assertEqual(path.read_bytes(), before)

    def test_unreadable_existing_ledger_is_never_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            path.write_text("not-json", encoding="utf-8")
            with self.assertRaises(LedgerIntegrityError):
                update_forecast_ledger(
                    path,
                    self.forecast,
                    [],
                    issued_at="2026-01-01T00:00:00Z",
                )
            self.assertEqual(path.read_text(encoding="utf-8"), "not-json")

    def test_tampered_content_addressed_issue_cannot_be_extended(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            ledger, _forecast_id = update_forecast_ledger(
                path,
                self.forecast,
                [],
                issued_at="2026-01-01T00:00:00Z",
            )
            ledger["events"][0]["baseline"]["value"] += 1.0
            path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
            before = path.read_bytes()
            with self.assertRaisesRegex(LedgerIntegrityError, "no longer matches"):
                update_forecast_ledger(
                    path,
                    self.forecast,
                    [],
                    issued_at="2026-02-01T00:00:00Z",
                )
            self.assertEqual(path.read_bytes(), before)


class LedgerValidatorCrossCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        points = synthetic_points()
        cls.forecast = build_empirical_forecast(points[:-300], THRESHOLDS)
        with tempfile.TemporaryDirectory() as directory:
            ledger, forecast_id = update_forecast_ledger(
                Path(directory) / "ledger.json",
                cls.forecast,
                [],
                issued_at="2026-01-01T00:00:00Z",
            )
        cls.ledger = ledger
        cls.latest = latest_for(cls.forecast, forecast_id)

    def test_valid_content_addressed_ledger(self) -> None:
        validate_ledger(copy.deepcopy(self.ledger), copy.deepcopy(self.latest))

    def test_tampered_issued_content_breaks_digest(self) -> None:
        ledger = copy.deepcopy(self.ledger)
        ledger["events"][0]["horizons"]["1m"]["probability"] += 0.1
        with self.assertRaisesRegex(ValidationError, "content digest"):
            validate_ledger(ledger, copy.deepcopy(self.latest))

    def test_latest_baseline_curve_and_model_must_match_issuance(self) -> None:
        mutations = (
            ("baseline", lambda latest: latest["baseline"].__setitem__("value", latest["baseline"]["value"] + 0.1)),
            ("curve.1m", lambda latest: latest["curve"].__setitem__("1m", latest["curve"]["1m"] + 0.1)),
            ("model", lambda latest: latest["model"].__setitem__("method", "silently revised")),
        )
        for expected, mutate in mutations:
            with self.subTest(field=expected):
                latest = copy.deepcopy(self.latest)
                mutate(latest)
                with self.assertRaisesRegex(ValidationError, expected):
                    validate_ledger(copy.deepcopy(self.ledger), latest)


if __name__ == "__main__":
    unittest.main()
