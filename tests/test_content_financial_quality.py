from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import scripts.build_browser_data as builder
from risklab.forecast import _calibration_status, build_empirical_forecast


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Point:
    observed_at: datetime
    value: float


def minimal_context() -> tuple[dict, dict, dict]:
    market = {
        "usd_try": {"latest": 47.0},
        "try_gap_20d": 1.2,
        "volatility": {"VIX": 18.0, "VXEEM": 24.0},
    }
    macro = {
        "global": {
            "fed_funds": None,
            "us_2y": None,
            "broad_dollar_change_20d": None,
        },
        "turkey": {
            "policy_rate": None,
            "official_reserve_assets": None,
            "official_reserve_assets_change_4w": None,
        },
    }
    news = {"headline_count_14d": 0, "chatter_count_14d": 0}
    return market, macro, news


class CalibrationGateTests(unittest.TestCase):
    def test_zero_ece_is_a_valid_gate_value(self) -> None:
        self.assertEqual(
            _calibration_status(
                {
                    "forecast_count": 50,
                    "brier_skill_vs_climatology": 0.001,
                    "calibration_error": 0.0,
                }
            ),
            "calibrated",
        )

    def test_zero_skill_still_fails_the_gate(self) -> None:
        self.assertEqual(
            _calibration_status(
                {
                    "forecast_count": 50,
                    "brier_skill_vs_climatology": 0.0,
                    "calibration_error": 0.0,
                }
            ),
            "experimental",
        )

    def test_aggregate_model_status_matches_calibration_flag(self) -> None:
        points = [
            Point(datetime(2024, 1, 1) + timedelta(days=index), 40.0 + index / 1000)
            for index in range(750)
        ]

        def fake_estimate(*_args: object, **_kwargs: object) -> tuple[dict, list, float]:
            return (
                {
                    "sample": {"calibration_examples": 20},
                    "calibration_status": "calibrated",
                    "calibration": {},
                    "signed_drivers": [],
                },
                [],
                0.25,
            )

        with patch("risklab.forecast._estimate_horizon", side_effect=fake_estimate):
            forecast = build_empirical_forecast(
                points,
                {"1w": 2, "1m": 5, "3m": 10, "6m": 15, "1y": 25},
            )

        self.assertTrue(forecast["model"]["is_calibrated"])
        self.assertEqual(forecast["model"]["status"], "calibrated")
        self.assertEqual(forecast["model"]["output_type"], "calibrated_probability")

        def insufficient_local_evidence(*_args: object, **_kwargs: object) -> tuple[dict, list, float]:
            specification, predictions, probability = fake_estimate()
            specification["sample"]["calibration_examples"] = 19
            return specification, predictions, probability

        with patch("risklab.forecast._estimate_horizon", side_effect=insufficient_local_evidence):
            insufficient = build_empirical_forecast(
                points,
                {"1w": 2, "1m": 5, "3m": 10, "6m": 15, "1y": 25},
            )

        self.assertFalse(insufficient["model"]["is_calibrated"])
        self.assertEqual(insufficient["model"]["status"], "experimental")


class MissingEvidenceTests(unittest.TestCase):
    def test_missing_global_context_remains_unknown(self) -> None:
        market, macro, news = minimal_context()
        trigger = builder.build_trigger_cards(market, macro, news)[2]

        self.assertIn("UNKNOWN", trigger["now"])
        self.assertIn("UNKNOWN", trigger["detail"])
        self.assertNotIn("neutral", trigger["now"].casefold())
        self.assertNotIn("neutral", trigger["detail"].casefold())
        self.assertIn("UNKNOWN", builder.build_global_reason(macro))

    def test_dead_missing_input_risk_curve_is_not_exposed(self) -> None:
        self.assertFalse(hasattr(builder, "build_risk_curve"))

    def test_context_coverage_is_not_named_as_forecast_confidence_in_new_field(self) -> None:
        market, macro, news = minimal_context()
        briefing = builder.build_briefing("1m", 12.0, market, macro, news, [])

        self.assertIn("evidence_coverage", briefing)
        self.assertEqual(briefing["evidence_coverage"], briefing["confidence"])
        self.assertIn("UNKNOWN", briefing["house_call"])

    def test_stale_and_unavailable_source_lists_are_distinct(self) -> None:
        cache = {
            "sources": {
                "ecb_eurtry": {"label": "ECB EUR/TRY", "status": "fresh"},
                "ecb_eurusd": {"label": "ECB EUR/USD", "status": "fresh"},
                "cached_context": {"label": "Cached context", "status": "cached_fallback"},
                "missing_context": {"label": "Missing context", "status": "unavailable"},
            }
        }

        health = builder.build_data_health(cache, [])

        self.assertEqual(health["stale_count"], 1)
        self.assertEqual(health["unavailable_count"], 1)
        self.assertEqual(health["unavailable_sources"], ["missing_context"])
        self.assertEqual(
            health["unavailable_or_stale_sources"],
            ["cached_context", "missing_context"],
        )
        self.assertEqual(health["status"], "degraded")

    def test_forbidden_missing_as_neutral_phrases_are_absent(self) -> None:
        source = (ROOT / "scripts" / "build_browser_data.py").read_text(encoding="utf-8").casefold()
        self.assertNotIn("neutralized by missing", source)
        self.assertNotIn("kept the global backdrop neutral", source)
        self.assertNotIn("neutral external read", source)


if __name__ == "__main__":
    unittest.main()
