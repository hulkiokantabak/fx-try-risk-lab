from __future__ import annotations

import math
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta

from risklab.forecast import (
    HORIZON_SESSIONS,
    MODEL_VERSION,
    Prediction,
    _coherent_touch_predictions,
    _feature_rows,
    _labelled_rows,
    _metrics,
    _touch_labelled_rows,
    _walk_forward_backtest,
    build_empirical_forecast,
)


@dataclass(frozen=True)
class Point:
    observed_at: datetime
    value: float


def synthetic_points(count: int = 1_650) -> list[Point]:
    start = datetime(2019, 1, 2)
    value = 5.25
    points = []
    for index in range(count):
        regime = 0.00055 if (index // 180) % 2 == 0 else 0.00115
        shock = math.sin(index / 11.0) * 0.0022 + math.sin(index / 47.0) * 0.0011
        value *= math.exp(regime + shock)
        points.append(Point(start + timedelta(days=index), value))
    return points


class TouchLabelTests(unittest.TestCase):
    def test_touch_outcome_dominates_terminal_outcome_for_identical_rows(self) -> None:
        start = datetime(2024, 1, 1)
        values = [100.0] * 180
        # Repeated breaches reverse before the five-observation terminal fix.
        for index in range(25, len(values), 12):
            values[index] = 108.0
            if index + 1 < len(values):
                values[index + 1] = 100.0
        points = [Point(start + timedelta(days=index), value) for index, value in enumerate(values)]
        features = _feature_rows(points)
        terminal = _labelled_rows(points, features, sessions=5, threshold=5.0)
        touch = _touch_labelled_rows(points, features, sessions=5, threshold=5.0)

        self.assertEqual(len(terminal), len(touch))
        self.assertTrue(any(path.outcome > end.outcome for end, path in zip(terminal, touch)))
        for end, path in zip(terminal, touch):
            self.assertEqual(end.features, path.features)
            self.assertEqual(end.target_index, path.target_index)
            self.assertGreaterEqual(path.outcome, end.outcome)

    def test_touch_labels_and_backtest_are_strictly_complete_window_purged(self) -> None:
        points = synthetic_points(1_200)
        features = _feature_rows(points)
        sessions = 66
        labelled = _touch_labelled_rows(points, features, sessions=sessions, threshold=10.0)

        # Even an early breach carries the full window-end resolution index.
        for row in labelled:
            self.assertEqual(row.target_index, row.features.index + sessions)

        predictions = _walk_forward_backtest(
            labelled,
            {row.index: row for row in features},
            sessions=sessions,
        )
        self.assertGreater(len(predictions), 10)
        for prediction in predictions:
            admitted = [row for row in labelled if row.target_index < prediction.forecast_index]
            self.assertTrue(all(row.target_index < prediction.forecast_index for row in admitted))
            expected_climatology = (sum(row.outcome for row in admitted) + 1.0) / (len(admitted) + 2.0)
            self.assertAlmostEqual(prediction.climatology_probability, expected_climatology)

    def test_probability_projection_preserves_unconstrained_series(self) -> None:
        def prediction(index: int, probability: float, outcome: int) -> Prediction:
            return Prediction(
                forecast_index=index,
                target_index=index + 5,
                raw_probability=probability,
                probability=probability,
                climatology_probability=0.3,
                outcome=outcome,
                conditional_count=50,
                calibration_count=25,
            )

        terminal = [prediction(100, 0.4, 1), prediction(105, 0.3, 0)]
        touch = [prediction(100, 0.2, 1), prediction(105, 0.5, 0)]
        coherent, adjusted, maximum = _coherent_touch_predictions(touch, terminal)

        self.assertEqual(adjusted, 1)
        self.assertAlmostEqual(maximum, 0.2)
        self.assertEqual([item.probability for item in coherent], [0.4, 0.5])
        self.assertEqual([item.unconstrained_probability for item in coherent], [0.2, 0.5])
        self.assertNotEqual(_metrics(coherent)["brier_score"], _metrics(touch)["brier_score"])


class TouchForecastContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.thresholds = {"1w": 2, "1m": 5, "3m": 10, "6m": 15, "1y": 25}
        cls.points = synthetic_points()
        cls.forecast = build_empirical_forecast(cls.points, cls.thresholds)

    def test_path_risk_contract_bounds_metrics_and_benchmark(self) -> None:
        forecast = self.forecast
        self.assertEqual(MODEL_VERSION, "empirical-regime-v2.2.0")
        self.assertEqual(forecast["model"]["primary_contract"], "exact_terminal")
        self.assertEqual(forecast["model"]["secondary_contract"], "any_time_breach")

        path_risk = forecast["path_risk"]
        self.assertEqual(path_risk["contract"], "any_time_breach")
        self.assertFalse(path_risk["primary"])
        definition = path_risk["event_definition"]
        self.assertIn("t+1 through t+h, inclusive", definition["statement"])
        self.assertIn("separate path-dependent contract", definition["relationship_to_terminal"])
        self.assertIn("intraday", definition["observation_limit"])
        self.assertEqual(set(path_risk["horizons"]), set(HORIZON_SESSIONS))
        self.assertEqual(set(path_risk["backtest"]["metrics"]), set(HORIZON_SESSIONS))
        self.assertIn("mathematical lower bound", path_risk["coherence_constraint"]["rationale"])

        features = _feature_rows(self.points)
        by_index = {row.index: row for row in features}
        saw_adjusted_backtest = False
        saw_changed_metric = False

        for horizon, sessions in HORIZON_SESSIONS.items():
            terminal = forecast["horizons"][horizon]
            touch = path_risk["horizons"][horizon]
            self.assertEqual(touch["event"]["event_type"], "any_time_breach")
            self.assertEqual(touch["event"]["window_start_offset"], 1)
            self.assertEqual(touch["event"]["window_end_offset"], sessions)
            self.assertTrue(touch["event"]["window_inclusive"])
            self.assertIn("max(USDTRY[t+1]", touch["event"]["formula"])
            self.assertIn("touches or exceeds", touch["event"]["contract_text"])

            self.assertGreaterEqual(touch["probability"], 0.0)
            self.assertLessEqual(touch["probability"], 100.0)
            self.assertLessEqual(touch["uncertainty"]["lower_probability"], touch["probability"])
            self.assertGreaterEqual(touch["uncertainty"]["upper_probability"], touch["probability"])
            self.assertGreaterEqual(touch["uncertainty"]["upper_probability"], terminal["probability"])
            self.assertEqual(touch["uncertainty"]["block_length_sessions"], sessions)
            self.assertGreater(touch["calibration"]["forecast_count"], 0)
            self.assertIn(touch["calibration_status"], {"calibrated", "experimental"})

            benchmark = touch["benchmark"]
            self.assertEqual(benchmark["role"], "challenger")
            self.assertGreaterEqual(benchmark["probability"], 0.0)
            self.assertLessEqual(benchmark["probability"], 100.0)
            self.assertAlmostEqual(
                benchmark["delta_model_minus_benchmark_percentage_points"],
                touch["probability"] - benchmark["probability"],
                delta=0.11,
            )
            self.assertEqual(benchmark["training_examples"], touch["sample"]["training_examples"])

            constraint = touch["coherence_constraint"]
            self.assertEqual(
                touch["probability"],
                max(touch["unconstrained_probability"], constraint["terminal_floor_probability"]),
            )
            self.assertGreaterEqual(touch["probability"], terminal["probability"])
            self.assertEqual(
                constraint["metrics_probability_series"],
                "coherence_constrained_any_time_breach",
            )

            challenger = terminal["challenger"]
            self.assertEqual(challenger["role"], "challenger")
            self.assertGreaterEqual(challenger["probability"], 0.0)
            self.assertLessEqual(challenger["probability"], 100.0)
            self.assertAlmostEqual(
                challenger["delta_model_minus_challenger_percentage_points"],
                terminal["probability"] - challenger["probability"],
                delta=0.11,
            )

            terminal_predictions = _walk_forward_backtest(
                _labelled_rows(
                    self.points,
                    features,
                    sessions=sessions,
                    threshold=self.thresholds[horizon],
                ),
                by_index,
                sessions=sessions,
            )
            unconstrained_touch_predictions = _walk_forward_backtest(
                _touch_labelled_rows(
                    self.points,
                    features,
                    sessions=sessions,
                    threshold=self.thresholds[horizon],
                ),
                by_index,
                sessions=sessions,
            )
            constrained_predictions, adjusted_count, maximum_adjustment = (
                _coherent_touch_predictions(
                    unconstrained_touch_predictions,
                    terminal_predictions,
                )
            )
            expected_metrics = _metrics(constrained_predictions)
            published_metrics = touch["calibration"]
            for key, expected in expected_metrics.items():
                self.assertEqual(published_metrics[key], expected)
            self.assertEqual(
                published_metrics["coherence_adjusted_forecast_count"],
                adjusted_count,
            )
            self.assertEqual(
                published_metrics["coherence_max_adjustment_percentage_points"],
                round(maximum_adjustment * 100.0, 4),
            )
            self.assertEqual(path_risk["backtest"]["metrics"][horizon], published_metrics)
            if adjusted_count:
                saw_adjusted_backtest = True
                if (
                    published_metrics["brier_score"]
                    != published_metrics["unconstrained_metrics"]["brier_score"]
                ):
                    saw_changed_metric = True

        self.assertTrue(saw_adjusted_backtest)
        self.assertTrue(saw_changed_metric)

    def test_path_risk_estimation_is_deterministic(self) -> None:
        repeated = build_empirical_forecast(synthetic_points(), self.thresholds)
        self.assertEqual(repeated["path_risk"], self.forecast["path_risk"])


if __name__ == "__main__":
    unittest.main()
