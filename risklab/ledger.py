"""Append-only issued-forecast and outcome event ledger."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from .forecast import MODEL_VERSION


LEDGER_SCHEMA_VERSION = "1.0"
IDENTITY_VERSION = "sha256-canonical-json-v2"
IDENTITY_DIGEST_LENGTH = 24
ISSUED_CONTENT_KEYS = ("model", "data_cutoff", "baseline", "target", "event_definition", "horizons")


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
    forecast_id, content_digest, issued_content = _forecast_identity(forecast)
    issued_ids = {
        event.get("forecast_id")
        for event in events
        if event.get("event_type") == "forecast_issued"
    }
    changed = False
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
    resolved = [event for event in ledger.get("events", []) if event.get("event_type") == "outcome_resolved"]
    first = min((event.get("recorded_at") for event in ledger.get("events", [])), default=None)
    return {
        "policy": "append-only; events are retained indefinitely",
        "issued_forecasts": len(issued),
        "resolved_horizon_outcomes": len(resolved),
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
            "resolution": "first run on which the exact target ECB trading observation is available",
        },
        "events": [],
    }


def _forecast_identity(forecast: dict) -> tuple[str, str, dict]:
    """Return a content-addressed identity for the complete issued forecast.

    The canonical content is deliberately broader than cutoff/model/thresholds:
    a revised baseline, probability, interval, event rule, or horizon diagnostic
    is a distinct issuance even when its market-data date did not change.
    """

    required = {"model", "data_cutoff", "baseline", "target", "event_definition", "horizons"}
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
    resolved_ids = {
        event.get("event_id")
        for event in events
        if event.get("event_type") == "outcome_resolved"
    }
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
            if resolution_id in resolved_ids:
                continue
            target_index = baseline_index + int(specification["sessions"])
            if target_index >= len(points):
                continue
            target = points[target_index]
            # The baseline value is the one sealed into the issuance event.
            # Source revisions may update the historical series but must never
            # rewrite the terms against which the forecast is scored.
            realized_move = ((float(target.value) / baseline_value) - 1.0) * 100.0
            _require_append_timestamp(events, recorded_at)
            events.append(
                {
                    "event_type": "outcome_resolved",
                    "event_id": resolution_id,
                    "forecast_id": event["forecast_id"],
                    "horizon": horizon,
                    "recorded_at": recorded_at,
                    "target_observation_date": target.observed_at.strftime("%Y-%m-%d"),
                    "target_value": round(float(target.value), 6),
                    "realized_move_percent": round(realized_move, 6),
                    "threshold_percent": specification["threshold_percent"],
                    "outcome": int(realized_move >= float(specification["threshold_percent"])),
                    "issued_probability": specification["probability"],
                    "model_version": event.get("model_version", MODEL_VERSION),
                }
            )
            resolved_ids.add(resolution_id)
            appended += 1
    return appended


def _verify_existing_identity(events: list[dict], forecast_id: str, content_digest: str) -> None:
    matches = [
        event
        for event in events
        if event.get("event_type") == "forecast_issued" and event.get("forecast_id") == forecast_id
    ]
    if len(matches) != 1:
        raise LedgerIntegrityError(f"ledger contains duplicate issuance identity {forecast_id}")
    identity = matches[0].get("identity")
    if isinstance(identity, dict) and identity.get("version") == IDENTITY_VERSION:
        event = matches[0]
        if any(key not in event for key in ISSUED_CONTENT_KEYS):
            raise LedgerIntegrityError(f"issued forecast {forecast_id} has incomplete identity content")
        event_content = {key: event[key] for key in ISSUED_CONTENT_KEYS}
        try:
            event_digest = hashlib.sha256(_canonical_json(event_content).encode("utf-8")).hexdigest()
        except (TypeError, ValueError) as exc:
            raise LedgerIntegrityError(f"issued forecast {forecast_id} has invalid identity content") from exc
        if identity.get("content_sha256") != event_digest:
            raise LedgerIntegrityError(f"issued forecast {forecast_id} content no longer matches its identity")
        if event_digest != content_digest:
            raise LedgerIntegrityError(f"forecast identity collision for {forecast_id}")


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
    resolutions: dict[str, dict[str, dict]] = {}
    for event in events:
        if event.get("event_type") == "outcome_resolved":
            resolutions.setdefault(str(event["forecast_id"]), {})[str(event["horizon"])] = event
    entries = []
    for event in events:
        if event.get("event_type") != "forecast_issued":
            continue
        horizon_results = resolutions.get(str(event["forecast_id"]), {})
        curve = {
            horizon: specification["probability"]
            for horizon, specification in event.get("horizons", {}).items()
        }
        entries.append(
            {
                "forecast_id": event["forecast_id"],
                "issued_at": event["recorded_at"],
                "data_cutoff": event["data_cutoff"],
                "model_version": event["model_version"],
                "primary_horizon": "1m",
                "curve": curve,
                "outcome": {
                    "status": "resolved" if len(horizon_results) == len(curve) else "partially_resolved" if horizon_results else "pending",
                    "horizons": horizon_results,
                },
            }
        )
    return entries


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
