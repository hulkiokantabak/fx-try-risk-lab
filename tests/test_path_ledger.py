from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from risklab.ledger import (
    IDENTITY_VERSION,
    LEGACY_IDENTITY_VERSION,
    LedgerIntegrityError,
    ledger_summary,
    update_forecast_ledger,
)
from scripts.validate_browser_bundle import ValidationError, validate_ledger


HORIZON_SESSIONS = {"1w": 2, "1m": 3, "3m": 4, "6m": 5, "1y": 6}
THRESHOLDS = {horizon: 5.0 for horizon in HORIZON_SESSIONS}
V2_KEYS = ("model", "data_cutoff", "baseline", "target", "event_definition", "horizons")


@dataclass(frozen=True)
class Point:
    observed_at: datetime
    value: float


def points() -> list[Point]:
    start = datetime(2026, 1, 1)
    # A 6% early touch reverses below the 1w terminal threshold.
    values = (100.0, 106.0, 102.0, 103.0, 108.0, 99.0, 110.0)
    return [Point(start + timedelta(days=index), value) for index, value in enumerate(values)]


def uncertainty(probability: float) -> dict:
    return {
        "lower_probability": max(0.0, probability - 5.0),
        "upper_probability": min(100.0, probability + 5.0),
        "level": 90,
        "method": "test moving-block interval",
        "effective_sample_size": 25,
    }


def forecast() -> dict:
    terminal_horizons = {}
    path_horizons = {}
    for index, (horizon, sessions) in enumerate(HORIZON_SESSIONS.items()):
        terminal_probability = 20.0 + index
        path_probability = 30.0 + index
        terminal_horizons[horizon] = {
            "sessions": sessions,
            "threshold_percent": THRESHOLDS[horizon],
            "probability": terminal_probability,
            "uncertainty": uncertainty(terminal_probability),
        }
        path_horizons[horizon] = {
            "sessions": sessions,
            "threshold_percent": THRESHOLDS[horizon],
            "probability": path_probability,
            "uncertainty": uncertainty(path_probability),
            "calibration": {"forecast_count": 100},
            "calibration_status": "experimental",
        }
    terminal_definition = {
        "statement": "USD/TRY meets the threshold at exactly t+h.",
        "thresholds_percent": copy.deepcopy(THRESHOLDS),
        "horizon_sessions": copy.deepcopy(HORIZON_SESSIONS),
        "baseline_rule": "latest derived ECB fix",
        "target_rule": "observation exactly h ECB sessions after baseline",
    }
    path_definition = {
        "statement": "Any observed derived ECB fix breaches the threshold from t+1 through t+h.",
        "measurement": "maximum derived ECB fix across t+1 through the complete window",
        "relationship_to_terminal": "A separate path contract from the primary terminal contract.",
        "thresholds_percent": copy.deepcopy(THRESHOLDS),
        "horizon_sessions": copy.deepcopy(HORIZON_SESSIONS),
        "baseline_rule": "latest derived ECB fix",
        "target_rule": "all observations t+1 through t+h inclusive",
    }
    return {
        "model": {"name": "test model", "version": "test-v3"},
        "data_cutoff": "2026-01-01T00:00:00Z",
        "baseline": {
            "pair": "USD/TRY",
            "value": 100.0,
            "observation_date": "2026-01-01",
        },
        "target": {"variable": "derived ECB USD/TRY reference rate"},
        "event_definition": terminal_definition,
        "horizons": terminal_horizons,
        "path_risk": {
            "contract": "any_time_breach",
            "primary": False,
            "event_definition": path_definition,
            "horizons": path_horizons,
        },
    }


def latest_for(payload: dict, forecast_id: str, *, include_path: bool = True) -> dict:
    latest = {
        "forecast_id": forecast_id,
        "data_cutoff": payload["data_cutoff"],
        "baseline": copy.deepcopy(payload["baseline"]),
        "model": copy.deepcopy(payload["model"]),
        "curve": {
            horizon: specification["probability"]
            for horizon, specification in payload["horizons"].items()
        },
        "thresholds": copy.deepcopy(payload["event_definition"]["thresholds_percent"]),
        "event_definition": copy.deepcopy(payload["event_definition"]),
        "uncertainty": {
            horizon: copy.deepcopy(specification["uncertainty"])
            for horizon, specification in payload["horizons"].items()
        },
        "forecast": copy.deepcopy(payload),
    }
    if include_path:
        latest["path_risk"] = copy.deepcopy(payload["path_risk"])
    return latest


def canonical_digest(content: dict) -> str:
    canonical = json.dumps(
        content,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class PathLedgerTests(unittest.TestCase):
    def test_v3_identity_seals_path_terms_and_rejects_tampering(self) -> None:
        payload = forecast()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            ledger, forecast_id = update_forecast_ledger(
                path,
                payload,
                points()[:1],
                issued_at="2026-01-01T12:00:00Z",
            )
            issue = ledger["events"][0]
            self.assertEqual(issue["identity"]["version"], IDENTITY_VERSION)
            self.assertEqual(issue["path_risk"], payload["path_risk"])
            validate_ledger(copy.deepcopy(ledger), latest_for(payload, forecast_id))

            issue["path_risk"]["horizons"]["1w"]["probability"] += 1.0
            path.write_text(json.dumps(ledger), encoding="utf-8")
            with self.assertRaisesRegex(LedgerIntegrityError, "no longer matches"):
                update_forecast_ledger(
                    path,
                    payload,
                    points()[:1],
                    issued_at="2026-01-02T12:00:00Z",
                )
            with self.assertRaisesRegex(ValidationError, "content digest"):
                validate_ledger(ledger, latest_for(payload, forecast_id))

    def test_touch_waits_for_complete_window_and_uses_immutable_baseline(self) -> None:
        payload = forecast()
        observations = points()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            ledger, forecast_id = update_forecast_ledger(
                path,
                payload,
                observations[:2],
                issued_at="2026-01-02T12:00:00Z",
            )
            # The 1w threshold already touched at t+1, but t+2 is unavailable.
            self.assertFalse(
                any(event["event_type"] == "path_outcome_resolved" for event in ledger["events"])
            )

            revised = list(observations[:3])
            revised[0] = Point(revised[0].observed_at, 200.0)
            ledger, repeated_id = update_forecast_ledger(
                path,
                payload,
                revised,
                issued_at="2026-01-03T12:00:00Z",
            )
            self.assertEqual(repeated_id, forecast_id)
            terminal = next(
                event
                for event in ledger["events"]
                if event["event_type"] == "outcome_resolved" and event["horizon"] == "1w"
            )
            path_result = next(
                event
                for event in ledger["events"]
                if event["event_type"] == "path_outcome_resolved" and event["horizon"] == "1w"
            )
            self.assertEqual(terminal["outcome"], 0)
            self.assertIs(path_result["outcome"], True)
            self.assertEqual(path_result["window_start_observation_date"], "2026-01-02")
            self.assertEqual(path_result["window_end_observation_date"], "2026-01-03")
            self.assertEqual(path_result["peak_observation_date"], "2026-01-02")
            self.assertEqual(path_result["peak_value"], 106.0)
            self.assertEqual(path_result["max_move_percent"], 6.0)
            validate_ledger(ledger, latest_for(payload, forecast_id))

            summary = ledger_summary(ledger)
            self.assertEqual(summary["resolved_horizon_outcomes"], 1)
            self.assertEqual(summary["resolved_terminal_outcomes"], 1)
            self.assertEqual(summary["resolved_path_outcomes"], 1)
            self.assertEqual(summary["resolved_outcomes_total"], 2)
            entry = ledger["entries"][0]
            self.assertEqual(entry["outcome"], entry["terminal_outcome"])
            self.assertEqual(entry["terminal_outcome"]["status"], "partially_resolved")
            self.assertEqual(entry["path_outcome"]["status"], "partially_resolved")

    def test_path_resolution_is_idempotent_and_ids_cannot_collide(self) -> None:
        payload = forecast()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            ledger, forecast_id = update_forecast_ledger(
                path,
                payload,
                points(),
                issued_at="2026-01-10T12:00:00Z",
            )
            before = path.read_bytes()
            repeated, repeated_id = update_forecast_ledger(
                path,
                payload,
                points(),
                issued_at="2026-02-10T12:00:00Z",
            )
            self.assertEqual(repeated_id, forecast_id)
            self.assertEqual(repeated, ledger)
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(
                len([event for event in ledger["events"] if event["event_type"] == "path_outcome_resolved"]),
                5,
            )
            self.assertEqual(len({event["event_id"] for event in ledger["events"]}), len(ledger["events"]))

            collision = copy.deepcopy(ledger)
            path_event = next(
                event for event in collision["events"] if event["event_type"] == "path_outcome_resolved"
            )
            path_event["event_id"] = f"{forecast_id}:{path_event['horizon']}:resolved"
            with self.assertRaisesRegex(ValidationError, "duplicate event_id"):
                validate_ledger(collision, latest_for(payload, forecast_id))

    def test_validator_recomputes_path_math_and_complete_window(self) -> None:
        payload = forecast()
        with tempfile.TemporaryDirectory() as directory:
            ledger, forecast_id = update_forecast_ledger(
                Path(directory) / "ledger.json",
                payload,
                points(),
                issued_at="2026-01-10T12:00:00Z",
            )
        latest = latest_for(payload, forecast_id)
        mutations = (
            (
                "immutable issued baseline",
                lambda event: event.__setitem__("peak_value", event["peak_value"] + 1.0),
            ),
            (
                "window end",
                lambda event: event.__setitem__("window_end_observation_date", "2026-01-20"),
            ),
            (
                "event definition",
                lambda event: event["event_definition"].__setitem__("measurement", "silently changed"),
            ),
        )
        for expected, mutate in mutations:
            with self.subTest(expected=expected):
                changed = copy.deepcopy(ledger)
                path_event = next(
                    event
                    for event in changed["events"]
                    if event["event_type"] == "path_outcome_resolved" and event["horizon"] == "1w"
                )
                mutate(path_event)
                with self.assertRaisesRegex(ValidationError, expected):
                    validate_ledger(changed, copy.deepcopy(latest))

    def test_v2_identity_remains_verifiable_and_v3_is_appended(self) -> None:
        payload = forecast()
        v2_content = {key: copy.deepcopy(payload[key]) for key in V2_KEYS}
        digest = canonical_digest(v2_content)
        old_id = f"fxtry-{payload['data_cutoff']}-{digest[:24]}"
        old_issue = {
            "event_type": "forecast_issued",
            "event_id": f"{old_id}:issued",
            "forecast_id": old_id,
            "recorded_at": "2026-01-01T10:00:00Z",
            "identity": {"version": LEGACY_IDENTITY_VERSION, "content_sha256": digest},
            "model_version": payload["model"]["version"],
            **v2_content,
        }
        old_ledger = {"schema_version": "1.0", "events": [old_issue]}
        old_latest_payload = copy.deepcopy(v2_content)
        validate_ledger(
            copy.deepcopy(old_ledger),
            latest_for(old_latest_payload, old_id, include_path=False),
        )

        tampered = copy.deepcopy(old_ledger)
        tampered["events"][0]["horizons"]["1w"]["probability"] += 1.0
        with self.assertRaisesRegex(ValidationError, "content digest"):
            validate_ledger(tampered, latest_for(old_latest_payload, old_id, include_path=False))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.json"
            path.write_text(json.dumps(old_ledger), encoding="utf-8")
            ledger, new_id = update_forecast_ledger(
                path,
                payload,
                points()[:1],
                issued_at="2026-01-02T10:00:00Z",
            )
        self.assertNotEqual(old_id, new_id)
        self.assertEqual(ledger["schema_version"], "1.1")
        self.assertEqual(
            [event.get("identity", {}).get("version") for event in ledger["events"]],
            [LEGACY_IDENTITY_VERSION, IDENTITY_VERSION],
        )
        validate_ledger(ledger, latest_for(payload, new_id))


if __name__ == "__main__":
    unittest.main()
