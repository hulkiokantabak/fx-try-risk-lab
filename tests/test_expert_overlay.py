from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.build_browser_data import build_path_risk, load_expert_view
from scripts.validate_browser_bundle import validate_expert_view, validate_path_risk


HORIZONS = ("1w", "1m", "3m", "6m", "1y")
THRESHOLDS = {"1w": 2, "1m": 5, "3m": 10, "6m": 15, "1y": 25}
SESSIONS = {"1w": 5, "1m": 22, "3m": 66, "6m": 132, "1y": 264}
FORECAST_ID = "fxtry-2026-07-10T00:00:00Z-canonical-example"
MODEL_VERSION = "empirical-regime-v2.2.0"


def expert_payload() -> dict:
    curves = {
        "Atlas": {"1w": 7, "1m": 9, "3m": 20, "6m": 33, "1y": 48},
        "Bosphorus": {"1w": 6, "1m": 9, "3m": 22, "6m": 34, "1y": 50},
        "Flow": {"1w": 6, "1m": 9, "3m": 22, "6m": 33, "1y": 49},
        "Vega": {"1w": 7, "1m": 9, "3m": 18, "6m": 29, "1y": 47},
    }
    confidence = {"Atlas": 36, "Bosphorus": 39, "Flow": 39, "Vega": 34}
    return {
        "schema_version": "1.0",
        "status": "complete",
        "evidence": {
            "forecast_id": FORECAST_ID,
            "model_version": MODEL_VERSION,
            "frozen_at": "2026-07-13T10:00:00Z",
            "data_cutoff": "2026-07-10T00:00:00Z",
            "empirical_curve": {"1w": 9.1, "1m": 9.2, "3m": 19.8, "6m": 26.8, "1y": 43.9},
        },
        "house": {
            "curve": {"1w": 6.5, "1m": 9.0, "3m": 20.6, "6m": 32.8, "1y": 48.8},
            "confidence": {"score": 37, "label": "low"},
            "summary": "Low-confidence weighted expert judgment on a frozen empirical evidence pack.",
            "aggregation": "fixed role weights by horizon",
        },
        "disagreement": {
            "ranges": {
                "1w": {"min": 6, "max": 7},
                "1m": {"min": 9, "max": 9},
                "3m": {"min": 18, "max": 22},
                "6m": {"min": 29, "max": 34},
                "1y": {"min": 47, "max": 50},
            },
            "minority_view": "Vega keeps the medium-horizon tail below the policy-led views.",
            "stress_view": "A discontinuous policy or liquidity shock can invalidate every smooth curve.",
        },
        "final_experts": [
            {
                "role": role,
                "curve": curve,
                "confidence": confidence[role],
                "stance": "Low-confidence conditional assessment",
                "rationale": "Final round view formed from the shared frozen evidence.",
            }
            for role, curve in curves.items()
        ],
        "rounds": [],
        "specialists": [],
        "accepted_improvements": [],
        "deferred_improvements": [],
        "triggers": [],
    }


def path_risk_payload() -> dict:
    horizons = {}
    for index, horizon in enumerate(HORIZONS):
        probability = 12.0 + index * 10
        horizons[horizon] = {
            "sessions": SESSIONS[horizon],
            "threshold_percent": THRESHOLDS[horizon],
            "probability": probability,
            "uncertainty": {
                "lower_probability": probability - 2,
                "upper_probability": probability + 2,
                "level": 90,
                "method": "moving-block bootstrap",
                "effective_sample_size": 100,
            },
            "calibration": {"forecast_count": 100},
            "calibration_status": "calibrated" if horizon in {"1w", "1m"} else "experimental",
        }
    return {
        "contract": "any_time_breach",
        "event_definition": {
            "statement": "An any-time breach occurs if any observed fix meets the threshold.",
            "measurement": "maximum observed reference rate from t+1 through the complete window",
            "relationship_to_terminal": "This is a different event from terminal probability and is not interchangeable.",
            "thresholds_percent": THRESHOLDS,
            "horizon_sessions": SESSIONS,
        },
        "horizons": horizons,
    }


class ExpertOverlayTests(unittest.TestCase):
    def test_loader_attaches_only_an_exact_evidence_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "expert-latest.json"
            payload = expert_payload()
            path.write_text(json.dumps(payload), encoding="utf-8")

            self.assertEqual(
                load_expert_view(path, forecast_id=FORECAST_ID, model_version=MODEL_VERSION),
                payload,
            )
            self.assertIsNone(
                load_expert_view(path, forecast_id=f"{FORECAST_ID}-new", model_version=MODEL_VERSION)
            )
            self.assertIsNone(
                load_expert_view(path, forecast_id=FORECAST_ID, model_version="empirical-regime-v9")
            )

    def test_loader_rejects_a_malformed_archive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "expert-latest.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaises(ValueError):
                load_expert_view(path, forecast_id=FORECAST_ID, model_version=MODEL_VERSION)

    def test_expert_contract_preserves_roles_ranges_and_evidence_identity(self) -> None:
        latest = {
            "forecast_id": FORECAST_ID,
            "model": {"version": MODEL_VERSION},
            "data_cutoff": "2026-07-10T00:00:00Z",
            "curve": {"1w": 9.1, "1m": 9.2, "3m": 19.8, "6m": 26.8, "1y": 43.9},
        }
        validate_expert_view(expert_payload(), latest)

    def test_path_risk_contract_is_separate_and_semantically_valid(self) -> None:
        payload = path_risk_payload()
        validate_path_risk(payload, THRESHOLDS)
        forecast = {
            "touch_event_definition": payload["event_definition"],
            "touch_horizons": payload["horizons"],
            "touch_backtest": {"metrics": {}},
        }
        normalized = build_path_risk(forecast)
        self.assertEqual(normalized["contract"], "any_time_breach")
        self.assertIn("relationship_to_terminal", normalized["event_definition"])


if __name__ == "__main__":
    unittest.main()
