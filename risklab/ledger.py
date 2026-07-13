"""Append-only issued-forecast and outcome event ledger."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from .forecast import MODEL_VERSION


LEDGER_SCHEMA_VERSION = "1.1"
IDENTITY_VERSION = "sha256-canonical-json-v3"
LEGACY_IDENTITY_VERSION = "sha256-canonical-json-v2"
IDENTITY_DIGEST_LENGTH = 24
ISSUED_CONTENT_KEYS_V2 = ("model", "data_cutoff", "baseline", "target", "event_definition", "horizons")
ISSUED_CONTENT_KEYS = (*ISSUED_CONTENT_KEYS_V2, "path_risk")
IDENTITY_CONTENT_KEYS = {
    LEGACY_IDENTITY_VERSION: ISSUED_CONTENT_KEYS_V2,
    IDENTITY_VERSION: ISSUED_CONTENT_KEYS,
}
RESOLUTION_POLICY = (
    "first run on which the complete ECB-observation window is available; terminal and "
    "any-reference-fix outcomes are separate events"
)


class LedgerIntegrityError(ValueError):
    """Raised when an existing ledger cannot be safely extended."""


def update_forecast_ledger(
    path: Path,
    forecast: dict,
    points: Sequence[object],
    *,
    issued_at: str,
    persist: bool = True,
) -> tuple[dict, str]:
    ledger = _load(path)
    events = ledger["events"]
    _verify_all_issued_identities(events)
    forecast_id, content_digest, issued_content = _forecast_identity(forecast)
    issued_ids = {
        event.get("forecast_id")
        for event in events
        if event.get("event_type") == "forecast_issued"
    }
    changed = ledger.get("schema_version") != LEDGER_SCHEMA_VERSION
    ledger["schema_version"] = LEDGER_SCHEMA_VERSION
    policy = ledger.get("policy")
    if isinstance(policy, dict) and policy.get("resolution") != RESOLUTION_POLICY:
        policy["resolution"] = RESOLUTION_POLICY
        changed = True
    if forecast_id not in issued_ids:
        _require_append_timestamp(events, issued_at)
        events.append(
            _issued_event(
                forecast_id,
                issued_content,
                issued_at,
                content_digest=content_digest,
            )
        )
        changed = True
    else:
        _verify_existing_identity(events, forecast_id, content_digest)
    changed = bool(_append_resolutions(events, points, issued_at)) or changed
    ledger["entries"] = _entries_projection(events)
    if changed or "updated_at" not in ledger:
        ledger["updated_at"] = issued_at
    if persist:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")
    return ledger, forecast_id


def ledger_summary(ledger: dict) -> dict:
    issued = [event for event in ledger.get("events", []) if event.get("event_type") == "forecast_issued"]
    terminal_resolved = [event for event in ledger.get("events", []) if event.get("event_type") == "outcome_resolved"]
    path_resolved = [event for event in ledger.get("events", []) if event.get("event_type") == "path_outcome_resolved"]
    first = min((event.get("recorded_at") for event in ledger.get("events", [])), default=None)
    return {
        "policy": "append-only; events are retained indefinitely",
        "issued_forecasts": len(issued),
        # Backward-compatible alias: this field has always counted primary,
        # exact-terminal horizon outcomes only.
        "resolved_horizon_outcomes": len(terminal_resolved),
        "resolved_terminal_outcomes": len(terminal_resolved),
        "resolved_path_outcomes": len(path_resolved),
        "resolved_outcomes_total": len(terminal_resolved) + len(path_resolved),
        "first_recorded_at": first,
        "ledger_schema_version": LEDGER_SCHEMA_VERSION,
    }


def _load(path: Path) -> dict:
    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LedgerIntegrityError(f"cannot safely extend unreadable ledger {path}") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
            raise LedgerIntegrityError(f"cannot safely extend malformed ledger {path}")
        return payload
    return {
        "schema_version": LEDGER_SCHEMA_VERSION,
        "policy": {
            "storage": "append-only event log",
            "retention": "indefinite (never trimmed; exceeds one year)",
            "resolution": RESOLUTION_POLICY,
        },
        "events": [],
    }


def _forecast_identity(forecast: dict) -> tuple[str, str, dict]:
    """Return a content-addressed identity for the complete issued forecast.

    The canonical content is deliberately broader than cutoff/model/thresholds:
    a revised baseline, probability, interval, event rule, or horizon diagnostic
    is a distinct issuance even when its market-data date did not change.
    """

    required = set(ISSUED_CONTENT_KEYS)
    missing = sorted(required - set(forecast))
    if missing:
        raise LedgerIntegrityError(f"forecast is missing identity content: {', '.join(missing)}")
    issued_content = {
        key: forecast[key]
        for key in ISSUED_CONTENT_KEYS
    }
    # A JSON round trip both detaches the append-only record from the caller and
    # guarantees the same strict serialization that is hashed below.
    try:
        canonical = _canonical_json(issued_content)
        issued_content = json.loads(canonical)
    except (TypeError, ValueError) as exc:
        raise LedgerIntegrityError("forecast identity content must be finite JSON") from exc
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    forecast_id = f"fxtry-{issued_content['data_cutoff']}-{digest[:IDENTITY_DIGEST_LENGTH]}"
    return forecast_id, digest, issued_content


def _issued_event(
    forecast_id: str,
    issued_content: dict,
    issued_at: str,
    *,
    content_digest: str,
) -> dict:
    return {
        "event_type": "forecast_issued",
        "event_id": f"{forecast_id}:issued",
        "forecast_id": forecast_id,
        "recorded_at": issued_at,
        "identity": {
            "version": IDENTITY_VERSION,
            "content_sha256": content_digest,
        },
        "model_version": issued_content["model"]["version"],
        **issued_content,
    }


def _append_resolutions(events: list[dict], points: Sequence[object], recorded_at: str) -> int:
    events_by_id: dict[str, dict] = {}
    for existing in events:
        event_id = existing.get("event_id")
        if not isinstance(event_id, str):
            continue
        if event_id in events_by_id:
            raise LedgerIntegrityError(f"ledger contains duplicate event_id {event_id}")
        events_by_id[event_id] = existing
    date_to_index = {point.observed_at.strftime("%Y-%m-%d"): index for index, point in enumerate(points)}
    appended = 0
    for event in list(events):
        if event.get("event_type") != "forecast_issued":
            continue
        baseline = event.get("baseline", {})
        baseline_date = str(baseline.get("observation_date") or event.get("data_cutoff", ""))[:10]
        baseline_index = date_to_index.get(baseline_date)
        if baseline_index is None:
            continue
        try:
            baseline_value = float(baseline["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise LedgerIntegrityError(
                f"issued forecast {event.get('forecast_id')} has no valid immutable baseline"
            ) from exc
        if not math.isfinite(baseline_value) or baseline_value <= 0:
            raise LedgerIntegrityError(
                f"issued forecast {event.get('forecast_id')} has no valid immutable baseline"
            )
        for horizon, specification in event.get("horizons", {}).items():
            resolution_id = f"{event['forecast_id']}:{horizon}:resolved"
            if _resolution_exists(events_by_id, resolution_id, "outcome_resolved"):
                continue
            target_index = baseline_index + int(specification["sessions"])
            if target_index >= len(points):
                continue
            target = points[target_index]
            # The baseline value is the one sealed into the issuance event.
            # Source revisions may update the historical series but must never
            # rewrite the terms against which the forecast is scored.
            target_value = _finite_positive_value(
                target.value,
                f"terminal target for {event.get('forecast_id')}.{horizon}",
            )
            realized_move = ((target_value / baseline_value) - 1.0) * 100.0
            _require_append_timestamp(events, recorded_at)
            resolution = {
                "event_type": "outcome_resolved",
                "event_id": resolution_id,
                "forecast_id": event["forecast_id"],
                "horizon": horizon,
                "recorded_at": recorded_at,
                "target_observation_date": target.observed_at.strftime("%Y-%m-%d"),
                "target_value": round(target_value, 6),
                "realized_move_percent": round(realized_move, 6),
                "threshold_percent": specification["threshold_percent"],
                "outcome": int(realized_move >= float(specification["threshold_percent"])),
                "issued_probability": specification["probability"],
                "model_version": event.get("model_version", MODEL_VERSION),
            }
            events.append(resolution)
            events_by_id[resolution_id] = resolution
            appended += 1

        path_risk = event.get("path_risk")
        if not isinstance(path_risk, dict):
            continue
        identity = event.get("identity")
        if not isinstance(identity, dict) or identity.get("version") != IDENTITY_VERSION:
            raise LedgerIntegrityError(
                f"issued forecast {event.get('forecast_id')} contains unsealed path risk"
            )
        path_definition = path_risk.get("event_definition")
        path_horizons = path_risk.get("horizons")
        if not isinstance(path_definition, dict) or not isinstance(path_horizons, dict):
            raise LedgerIntegrityError(
                f"issued forecast {event.get('forecast_id')} has malformed sealed path risk"
            )
        for horizon, specification in path_horizons.items():
            resolution_id = f"{event['forecast_id']}:path:{horizon}:resolved"
            if _resolution_exists(events_by_id, resolution_id, "path_outcome_resolved"):
                continue
            try:
                sessions = int(specification["sessions"])
            except (KeyError, TypeError, ValueError) as exc:
                raise LedgerIntegrityError(
                    f"issued path forecast {event.get('forecast_id')}.{horizon} has invalid sessions"
                ) from exc
            target_index = baseline_index + sessions
            # A touch may happen early, but a zero (and therefore the scored
            # event) is knowable only when the complete t+1..t+h window exists.
            if sessions <= 0 or target_index >= len(points):
                continue
            window = points[baseline_index + 1 : target_index + 1]
            if len(window) != sessions:
                continue
            peak = max(
                window,
                key=lambda point: _finite_positive_value(
                    point.value,
                    f"path observation for {event.get('forecast_id')}.{horizon}",
                ),
            )
            peak_value = _finite_positive_value(
                peak.value,
                f"path peak for {event.get('forecast_id')}.{horizon}",
            )
            max_move = ((peak_value / baseline_value) - 1.0) * 100.0
            threshold = float(specification["threshold_percent"])
            _require_append_timestamp(events, recorded_at)
            resolution = {
                "event_type": "path_outcome_resolved",
                "event_id": resolution_id,
                "forecast_id": event["forecast_id"],
                "horizon": horizon,
                "recorded_at": recorded_at,
                "contract": "any_time_breach",
                "event_definition": json.loads(_canonical_json(path_definition)),
                "window_sessions": sessions,
                "window_start_observation_date": window[0].observed_at.strftime("%Y-%m-%d"),
                "window_end_observation_date": window[-1].observed_at.strftime("%Y-%m-%d"),
                "peak_observation_date": peak.observed_at.strftime("%Y-%m-%d"),
                "peak_value": round(peak_value, 6),
                "max_move_percent": round(max_move, 6),
                "threshold_percent": specification["threshold_percent"],
                "outcome": bool(max_move >= threshold),
                "issued_probability": specification["probability"],
                "model_version": event.get("model_version", MODEL_VERSION),
            }
            events.append(resolution)
            events_by_id[resolution_id] = resolution
            appended += 1
    return appended


def _resolution_exists(events_by_id: dict[str, dict], event_id: str, expected_type: str) -> bool:
    existing = events_by_id.get(event_id)
    if existing is None:
        return False
    if existing.get("event_type") != expected_type:
        raise LedgerIntegrityError(f"event_id collision at {event_id}")
    return True


def _finite_positive_value(value: object, label: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise LedgerIntegrityError(f"{label} must be a finite positive number") from exc
    if not math.isfinite(number) or number <= 0:
        raise LedgerIntegrityError(f"{label} must be a finite positive number")
    return number


def _verify_existing_identity(events: list[dict], forecast_id: str, content_digest: str) -> None:
    matches = [
        event
        for event in events
        if event.get("event_type") == "forecast_issued" and event.get("forecast_id") == forecast_id
    ]
    if len(matches) != 1:
        raise LedgerIntegrityError(f"ledger contains duplicate issuance identity {forecast_id}")
    identity = matches[0].get("identity")
    if not isinstance(identity, dict) or identity.get("version") != IDENTITY_VERSION:
        raise LedgerIntegrityError(f"forecast identity collision for {forecast_id}")
    if identity.get("content_sha256") != content_digest:
        raise LedgerIntegrityError(f"forecast identity collision for {forecast_id}")


def _verify_all_issued_identities(events: list[dict]) -> None:
    """Verify every versioned issue before extending the append-only log."""

    for event in events:
        if event.get("event_type") != "forecast_issued":
            continue
        identity = event.get("identity")
        if identity is None:
            # Pre-content-addressed records remain readable. They cannot carry
            # path terms because those would not be protected by an identity.
            if "path_risk" in event:
                raise LedgerIntegrityError(
                    f"issued forecast {event.get('forecast_id')} contains unsealed path risk"
                )
            continue
        if not isinstance(identity, dict):
            raise LedgerIntegrityError(
                f"issued forecast {event.get('forecast_id')} has malformed identity metadata"
            )
        version = identity.get("version")
        keys = IDENTITY_CONTENT_KEYS.get(version)
        if keys is None:
            raise LedgerIntegrityError(
                f"issued forecast {event.get('forecast_id')} has unsupported identity version"
            )
        if version == LEGACY_IDENTITY_VERSION and "path_risk" in event:
            raise LedgerIntegrityError(
                f"issued forecast {event.get('forecast_id')} contains path risk outside its v2 identity"
            )
        if any(key not in event for key in keys):
            raise LedgerIntegrityError(
                f"issued forecast {event.get('forecast_id')} has incomplete identity content"
            )
        event_content = {key: event[key] for key in keys}
        try:
            event_digest = hashlib.sha256(_canonical_json(event_content).encode("utf-8")).hexdigest()
        except (TypeError, ValueError) as exc:
            raise LedgerIntegrityError(
                f"issued forecast {event.get('forecast_id')} has invalid identity content"
            ) from exc
        if identity.get("content_sha256") != event_digest:
            raise LedgerIntegrityError(
                f"issued forecast {event.get('forecast_id')} content no longer matches its identity"
            )
        expected_id = f"fxtry-{event['data_cutoff']}-{event_digest[:IDENTITY_DIGEST_LENGTH]}"
        if event.get("forecast_id") != expected_id:
            raise LedgerIntegrityError(
                f"issued forecast {event.get('forecast_id')} content no longer matches its identity"
            )


def _require_append_timestamp(events: list[dict], recorded_at: str) -> None:
    if not events:
        return
    previous = events[-1].get("recorded_at")
    if not isinstance(previous, str):
        raise LedgerIntegrityError("cannot append to ledger whose last event has no recorded_at timestamp")
    try:
        previous_time = datetime.fromisoformat(previous.replace("Z", "+00:00"))
        recorded_time = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise LedgerIntegrityError("ledger event timestamps must be timezone-qualified ISO-8601 values") from exc
    if previous_time.tzinfo is None or recorded_time.tzinfo is None:
        raise LedgerIntegrityError("ledger event timestamps must include a timezone")
    previous_time = previous_time.astimezone(UTC)
    recorded_time = recorded_time.astimezone(UTC)
    if recorded_time < previous_time:
        raise LedgerIntegrityError(
            f"cannot append event at {recorded_at} after ledger event recorded at {previous}"
        )


def _canonical_json(value: dict) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _entries_projection(events: list[dict]) -> list[dict]:
    """Compatibility projection: one immutable issuance row per forecast."""
    terminal_resolutions: dict[str, dict[str, dict]] = {}
    path_resolutions: dict[str, dict[str, dict]] = {}
    for event in events:
        if event.get("event_type") == "outcome_resolved":
            terminal_resolutions.setdefault(str(event["forecast_id"]), {})[str(event["horizon"])] = event
        elif event.get("event_type") == "path_outcome_resolved":
            path_resolutions.setdefault(str(event["forecast_id"]), {})[str(event["horizon"])] = event
    entries = []
    for event in events:
        if event.get("event_type") != "forecast_issued":
            continue
        horizon_results = terminal_resolutions.get(str(event["forecast_id"]), {})
        path_horizon_results = path_resolutions.get(str(event["forecast_id"]), {})
        curve = {
            horizon: specification["probability"]
            for horizon, specification in event.get("horizons", {}).items()
        }
        path_curve = {
            horizon: specification["probability"]
            for horizon, specification in event.get("path_risk", {}).get("horizons", {}).items()
        }
        terminal_outcome = {
            "status": _projection_status(horizon_results, len(curve)),
            "horizons": horizon_results,
        }
        path_outcome = {
            "status": _projection_status(path_horizon_results, len(path_curve), unavailable="not_issued"),
            "horizons": path_horizon_results,
        }
        entries.append(
            {
                "forecast_id": event["forecast_id"],
                "issued_at": event["recorded_at"],
                "data_cutoff": event["data_cutoff"],
                "model_version": event["model_version"],
                "primary_horizon": "1m",
                "curve": curve,
                "path_curve": path_curve,
                # Keep outcome as an alias for clients built against schema 1.0.
                "outcome": terminal_outcome,
                "terminal_outcome": terminal_outcome,
                "path_outcome": path_outcome,
            }
        )
    return entries


def _projection_status(results: dict[str, dict], expected: int, *, unavailable: str = "pending") -> str:
    if expected == 0:
        return unavailable
    if len(results) == expected:
        return "resolved"
    if results:
        return "partially_resolved"
    return "pending"


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
