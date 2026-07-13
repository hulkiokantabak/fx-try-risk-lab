"""Validate the published static bundle without contacting external services.

The validator is deliberately stricter than a JSON parse.  A syntactically
valid snapshot can still be dangerous when horizons disagree, numbers are not
finite, freshness metadata is absent, or the public history cannot be joined to
the latest forecast.  CI and the refresh job both run this module before data
can be published.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping


ROOT = Path(__file__).resolve().parent.parent
HORIZONS = ("1w", "1m", "3m", "6m", "1y")
HORIZON_SET = set(HORIZONS)
LEDGER_IDENTITY_VERSION_V2 = "sha256-canonical-json-v2"
LEDGER_IDENTITY_VERSION = "sha256-canonical-json-v3"
LEDGER_IDENTITY_DIGEST_LENGTH = 24
LEDGER_CONTENT_KEYS_V2 = ("model", "data_cutoff", "baseline", "target", "event_definition", "horizons")
LEDGER_CONTENT_KEYS = (*LEDGER_CONTENT_KEYS_V2, "path_risk")
LEDGER_CONTENT_KEYS_BY_VERSION = {
    LEDGER_IDENTITY_VERSION_V2: LEDGER_CONTENT_KEYS_V2,
    LEDGER_IDENTITY_VERSION: LEDGER_CONTENT_KEYS,
}
FORECAST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
ALLOWED_HEALTH = {
    "healthy",
    "good",
    "complete",
    "degraded",
    "partial",
    "stale",
    "critical",
    "insufficient",
    "unavailable",
    "missing",
    "cached",
    "cached_fallback",
    "live",
    "fresh",
    "blocked",
    "stale_or_invalid",
    "unknown",
}


class ValidationError(ValueError):
    """Raised when a published artifact violates the data contract."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationError(message)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValidationError(f"Invalid JSON in {path}: {exc}") from exc


def require_object(value: Any, label: str) -> Mapping[str, Any]:
    require(isinstance(value, dict), f"{label} must be an object")
    return value


def require_list(value: Any, label: str, *, nonempty: bool = False) -> list[Any]:
    require(isinstance(value, list), f"{label} must be an array")
    if nonempty:
        require(bool(value), f"{label} must not be empty")
    return value


def require_keys(value: Mapping[str, Any], keys: Iterable[str], label: str) -> None:
    missing = sorted(set(keys) - set(value))
    require(not missing, f"{label} is missing required keys: {', '.join(missing)}")


def require_string(value: Any, label: str, *, nonempty: bool = True) -> str:
    require(isinstance(value, str), f"{label} must be a string")
    if nonempty:
        require(bool(value.strip()), f"{label} must not be blank")
    return value


def require_number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    require(isinstance(value, (int, float)) and not isinstance(value, bool), f"{label} must be a number")
    number = float(value)
    require(math.isfinite(number), f"{label} must be finite")
    if minimum is not None:
        require(number >= minimum, f"{label} must be >= {minimum}")
    if maximum is not None:
        require(number <= maximum, f"{label} must be <= {maximum}")
    return number


def parse_timestamp(value: Any, label: str) -> datetime:
    text = require_string(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{label} must be an ISO-8601 timestamp") from exc
    require(parsed.tzinfo is not None, f"{label} must include a timezone")
    return parsed.astimezone(UTC)


def parse_temporal(value: Any, label: str) -> datetime:
    """Accept a dated market observation or a timezone-qualified issue time."""

    text = require_string(value, label)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return datetime.fromisoformat(text).replace(tzinfo=UTC)
        except ValueError as exc:
            raise ValidationError(f"{label} must be a valid ISO-8601 date") from exc
    return parse_timestamp(value, label)


def validate_json_numbers(value: Any, label: str = "JSON") -> None:
    """Reject NaN/Infinity anywhere, including in optional source payloads."""

    if isinstance(value, bool) or value is None or isinstance(value, str):
        return
    if isinstance(value, (int, float)):
        require(math.isfinite(float(value)), f"{label} contains a non-finite number")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            validate_json_numbers(item, f"{label}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            require(isinstance(key, str), f"{label} contains a non-string object key")
            validate_json_numbers(item, f"{label}.{key}")
        return
    raise ValidationError(f"{label} contains unsupported value type {type(value).__name__}")


def validate_horizon_map(
    value: Any,
    label: str,
    *,
    minimum: float,
    maximum: float,
) -> Mapping[str, Any]:
    mapping = require_object(value, label)
    require(set(mapping) == HORIZON_SET, f"{label} must contain exactly {', '.join(HORIZONS)}")
    for horizon in HORIZONS:
        require_number(mapping[horizon], f"{label}.{horizon}", minimum=minimum, maximum=maximum)
    return mapping


def validate_event_definition(event: Any, thresholds: Mapping[str, Any]) -> None:
    event = require_object(event, "latest.event_definition")
    # Current contract: one exact statement plus threshold and session maps.
    if "thresholds_percent" in event:
        require_keys(
            event,
            {"statement", "thresholds_percent", "horizon_sessions", "baseline_rule", "target_rule"},
            "latest.event_definition",
        )
        event_thresholds = validate_horizon_map(
            event["thresholds_percent"],
            "latest.event_definition.thresholds_percent",
            minimum=0.000001,
            maximum=100,
        )
        sessions = validate_horizon_map(
            event["horizon_sessions"],
            "latest.event_definition.horizon_sessions",
            minimum=1,
            maximum=1000,
        )
        for horizon, value in sessions.items():
            require(isinstance(value, int) and not isinstance(value, bool), f"session count for {horizon} must be an integer")
        require_string(event["statement"], "latest.event_definition.statement")
        require_string(event["baseline_rule"], "latest.event_definition.baseline_rule")
        require_string(event["target_rule"], "latest.event_definition.target_rule")
    else:
        # Compatibility with early v2 fixtures; versioned production snapshots
        # use the branch above.
        require_keys(event, {"pair", "direction", "thresholds", "measurement"}, "latest.event_definition")
        pair = require_string(event["pair"], "latest.event_definition.pair").upper().replace("/", "")
        require(pair == "USDTRY", "latest.event_definition.pair must identify USD/TRY")
        direction = require_string(event["direction"], "latest.event_definition.direction").casefold()
        require(
            direction in {"try_depreciation", "try depreciation", "usdtry_increase", "usd/try increase"},
            "latest.event_definition.direction must define TRY depreciation / USDTRY increase",
        )
        event_thresholds = validate_horizon_map(
            event["thresholds"], "latest.event_definition.thresholds", minimum=0.000001, maximum=100
        )
        require_string(event["measurement"], "latest.event_definition.measurement")
    require(dict(event_thresholds) == dict(thresholds), "event-definition thresholds must match published thresholds")


def validate_model(model: Any) -> None:
    model = require_object(model, "latest.model")
    require_keys(model, {"name", "version", "status", "method"}, "latest.model")
    require_string(model["name"], "latest.model.name")
    require_string(model["version"], "latest.model.version")
    status = require_string(model["status"], "latest.model.status").casefold()
    require(status in {"experimental", "research", "calibrated"}, "latest.model.status is unsupported")
    require_string(model["method"], "latest.model.method")
    if "training_protocol" in model:
        require_string(model["training_protocol"], "latest.model.training_protocol")
    if "probability_scale" in model:
        require_string(model["probability_scale"], "latest.model.probability_scale")


def validate_baseline(value: Any, data_cutoff: datetime) -> None:
    baseline = require_object(value, "latest.baseline")
    require_keys(baseline, {"pair", "value", "observation_date", "source", "price_type"}, "latest.baseline")
    pair = require_string(baseline["pair"], "latest.baseline.pair").upper().replace("/", "")
    require(pair == "USDTRY", "latest.baseline.pair must identify USD/TRY")
    require_number(baseline["value"], "latest.baseline.value", minimum=0.000001)
    observed = parse_temporal(baseline["observation_date"], "latest.baseline.observation_date")
    require(observed == data_cutoff, "latest baseline observation must equal data_cutoff")
    require_string(baseline["source"], "latest.baseline.source")
    require_string(baseline["price_type"], "latest.baseline.price_type")


def validate_uncertainty(value: Any, curve: Mapping[str, Any]) -> None:
    uncertainty = require_object(value, "latest.uncertainty")
    require(set(uncertainty) == HORIZON_SET, "latest.uncertainty must match the canonical horizons")
    for horizon in HORIZONS:
        interval = require_object(uncertainty[horizon], f"latest.uncertainty.{horizon}")
        require_keys(
            interval,
            {"lower_probability", "upper_probability", "level", "method", "effective_sample_size"},
            f"latest.uncertainty.{horizon}",
        )
        lower = require_number(interval["lower_probability"], f"uncertainty.{horizon}.lower_probability", minimum=0, maximum=100)
        upper = require_number(interval["upper_probability"], f"uncertainty.{horizon}.upper_probability", minimum=0, maximum=100)
        require(lower <= float(curve[horizon]) <= upper, f"curve.{horizon} must lie inside its uncertainty interval")
        require(lower <= upper, f"uncertainty.{horizon} lower must not exceed upper")
        level = require_number(interval["level"], f"uncertainty.{horizon}.level", minimum=0, maximum=100)
        require(level > 0, f"uncertainty.{horizon}.level must be positive")
        require_number(interval["effective_sample_size"], f"uncertainty.{horizon}.effective_sample_size", minimum=1)
        require_string(interval["method"], f"uncertainty.{horizon}.method")


def validate_path_risk(value: Any, thresholds: Mapping[str, Any]) -> None:
    """Validate the optional, separately estimated any-time-breach contract."""

    path_risk = require_object(value, "latest.path_risk")
    require_keys(path_risk, {"contract", "event_definition", "horizons"}, "latest.path_risk")
    contract = require_string(path_risk["contract"], "latest.path_risk.contract").casefold()
    require(contract == "any_time_breach", "latest.path_risk.contract must be any_time_breach")

    event = require_object(path_risk["event_definition"], "latest.path_risk.event_definition")
    require_keys(
        event,
        {"statement", "measurement", "relationship_to_terminal", "thresholds_percent", "horizon_sessions"},
        "latest.path_risk.event_definition",
    )
    statement = require_string(event["statement"], "latest.path_risk.event_definition.statement").casefold()
    measurement = require_string(event["measurement"], "latest.path_risk.event_definition.measurement").casefold()
    relationship = require_string(
        event["relationship_to_terminal"],
        "latest.path_risk.event_definition.relationship_to_terminal",
    ).casefold()
    require("any" in statement and ("breach" in statement or "touch" in statement), "path-risk statement must define an any-time breach")
    require("t+1" in measurement or "maximum" in measurement or "window" in measurement, "path-risk measurement must define the observation window")
    require(
        "terminal" in relationship
        and ("not interchangeable" in relationship or "different" in relationship or "separate" in relationship),
        "path risk must state that it is separate from terminal probability",
    )
    path_thresholds = validate_horizon_map(
        event["thresholds_percent"],
        "latest.path_risk.event_definition.thresholds_percent",
        minimum=0.000001,
        maximum=100,
    )
    require(dict(path_thresholds) == dict(thresholds), "path-risk thresholds must match the primary contract")
    sessions = validate_horizon_map(
        event["horizon_sessions"],
        "latest.path_risk.event_definition.horizon_sessions",
        minimum=1,
        maximum=1000,
    )
    for horizon, count in sessions.items():
        require(isinstance(count, int) and not isinstance(count, bool), f"path-risk sessions.{horizon} must be an integer")

    horizons = require_object(path_risk["horizons"], "latest.path_risk.horizons")
    require(set(horizons) == HORIZON_SET, "latest.path_risk.horizons must match canonical horizons")
    curve: dict[str, float] = {}
    intervals: dict[str, Any] = {}
    for horizon in HORIZONS:
        item = require_object(horizons[horizon], f"latest.path_risk.horizons.{horizon}")
        require_keys(
            item,
            {"sessions", "threshold_percent", "probability", "uncertainty", "calibration", "calibration_status"},
            f"latest.path_risk.horizons.{horizon}",
        )
        count = item["sessions"]
        require(isinstance(count, int) and not isinstance(count, bool), f"path-risk horizons.{horizon}.sessions must be an integer")
        require(count == sessions[horizon], f"path-risk horizons.{horizon}.sessions must match its event definition")
        require_number(item["threshold_percent"], f"path-risk horizons.{horizon}.threshold_percent", minimum=0.000001, maximum=100)
        require(float(item["threshold_percent"]) == float(thresholds[horizon]), f"path-risk horizons.{horizon} threshold must match primary contract")
        curve[horizon] = require_number(item["probability"], f"path-risk horizons.{horizon}.probability", minimum=0, maximum=100)
        intervals[horizon] = item["uncertainty"]
        calibration_status = require_string(item["calibration_status"], f"path-risk horizons.{horizon}.calibration_status").casefold()
        require(calibration_status in {"calibrated", "experimental"}, f"unsupported path-risk calibration status for {horizon}")
        calibration = require_object(item["calibration"], f"latest.path_risk.horizons.{horizon}.calibration")
        if "forecast_count" in calibration:
            require_number(calibration["forecast_count"], f"path-risk calibration {horizon}.forecast_count", minimum=0)
    validate_uncertainty(intervals, curve)


def validate_expert_view(value: Any, latest: Mapping[str, Any]) -> None:
    """Validate frozen-evidence expert judgment without treating it as model output."""

    expert = require_object(value, "latest.expert_view")
    require_keys(
        expert,
        {"status", "evidence", "house", "disagreement", "final_experts"},
        "latest.expert_view",
    )
    require_string(expert["status"], "latest.expert_view.status")
    evidence = require_object(expert["evidence"], "latest.expert_view.evidence")
    require_keys(evidence, {"forecast_id", "model_version"}, "latest.expert_view.evidence")
    require(
        evidence["forecast_id"] == latest.get("forecast_id"),
        "expert evidence forecast_id must exactly match latest.forecast_id",
    )
    model = require_object(latest.get("model"), "latest.model")
    require(
        evidence["model_version"] == model.get("version"),
        "expert evidence model_version must exactly match latest.model.version",
    )
    if "frozen_at" in evidence:
        parse_timestamp(evidence["frozen_at"], "latest.expert_view.evidence.frozen_at")
    if "data_cutoff" in evidence:
        require(evidence["data_cutoff"] == latest.get("data_cutoff"), "expert evidence data_cutoff must match latest")
    if "empirical_curve" in evidence:
        empirical_curve = validate_horizon_map(
            evidence["empirical_curve"],
            "latest.expert_view.evidence.empirical_curve",
            minimum=0,
            maximum=100,
        )
        require(dict(empirical_curve) == dict(latest["curve"]), "expert evidence empirical_curve must match latest")

    house = require_object(expert["house"], "latest.expert_view.house")
    require_keys(house, {"curve", "confidence", "summary"}, "latest.expert_view.house")
    house_curve = validate_horizon_map(house["curve"], "latest.expert_view.house.curve", minimum=0, maximum=100)
    house_confidence = house["confidence"]
    if isinstance(house_confidence, dict):
        require_keys(house_confidence, {"score", "label"}, "latest.expert_view.house.confidence")
        require_number(house_confidence["score"], "latest.expert_view.house.confidence.score", minimum=0, maximum=100)
        require_string(house_confidence["label"], "latest.expert_view.house.confidence.label")
    else:
        require_number(house_confidence, "latest.expert_view.house.confidence", minimum=0, maximum=100)
    require_string(house["summary"], "latest.expert_view.house.summary")
    aggregation = house.get("aggregation", house.get("aggregation_method", house.get("method")))
    aggregation = require_string(aggregation, "latest.expert_view.house.aggregation")
    require("weight" in aggregation.casefold(), "expert house aggregation must disclose its weighting method")

    disagreement = require_object(expert["disagreement"], "latest.expert_view.disagreement")
    require_keys(disagreement, {"ranges", "minority_view", "stress_view"}, "latest.expert_view.disagreement")
    ranges = require_object(disagreement["ranges"], "latest.expert_view.disagreement.ranges")
    require(set(ranges) == HORIZON_SET, "expert disagreement ranges must match canonical horizons")
    for horizon in HORIZONS:
        interval = require_object(ranges[horizon], f"expert disagreement range {horizon}")
        require_keys(interval, {"min", "max"}, f"expert disagreement range {horizon}")
        lower = require_number(interval["min"], f"expert disagreement range {horizon}.min", minimum=0, maximum=100)
        upper = require_number(interval["max"], f"expert disagreement range {horizon}.max", minimum=0, maximum=100)
        require(lower <= float(house_curve[horizon]) <= upper, f"expert house curve.{horizon} must lie inside disagreement range")
    require_string(disagreement["minority_view"], "latest.expert_view.disagreement.minority_view")
    require_string(disagreement["stress_view"], "latest.expert_view.disagreement.stress_view")

    members = require_list(expert["final_experts"], "latest.expert_view.final_experts", nonempty=True)
    require(len(members) == 4, "latest.expert_view.final_experts must contain exactly four core roles")
    expected_roles = {"atlas", "bosphorus", "flow", "vega"}
    observed_roles: set[str] = set()
    for index, raw_member in enumerate(members):
        member = require_object(raw_member, f"latest.expert_view.final_experts[{index}]")
        require_keys(member, {"curve", "confidence", "stance"}, f"latest.expert_view.final_experts[{index}]")
        role = require_string(member.get("role", member.get("name")), f"expert member {index}.role").casefold()
        observed_roles.add(role)
        validate_horizon_map(member["curve"], f"expert member {role}.curve", minimum=0, maximum=100)
        require_number(member["confidence"], f"expert member {role}.confidence", minimum=0, maximum=100)
        require_string(member["stance"], f"expert member {role}.stance")
        if "rationale" in member:
            require_string(member["rationale"], f"expert member {role}.rationale")
    require(observed_roles == expected_roles, "expert final roles must be Atlas, Bosphorus, Flow and Vega")


def iter_health_sources(data_health: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    raw = data_health.get("sources", [])
    if isinstance(raw, dict):
        return [(str(key), require_object(value, f"latest.data_health.sources.{key}")) for key, value in raw.items()]
    sources = require_list(raw, "latest.data_health.sources", nonempty=True)
    result: list[tuple[str, Mapping[str, Any]]] = []
    for index, source in enumerate(sources):
        source = require_object(source, f"latest.data_health.sources[{index}]")
        source_id = source.get("key", source.get("id", source.get("name")))
        result.append((require_string(source_id, f"latest.data_health.sources[{index}].id"), source))
    return result


def validate_data_health(value: Any, *, generated_at: datetime) -> list[tuple[str, Mapping[str, Any]]]:
    health = require_object(value, "latest.data_health")
    require_keys(health, {"sources"}, "latest.data_health")
    status_value = health.get("overall_status", health.get("status"))
    status = require_string(status_value, "latest.data_health.overall_status").casefold()
    require(status in ALLOWED_HEALTH, f"latest.data_health.overall_status has unsupported value {status!r}")
    if "coverage_ratio" in health:
        require_number(health["coverage_ratio"], "latest.data_health.coverage_ratio", minimum=0, maximum=1)
    if "coverage_pct" in health:
        require_number(health["coverage_pct"], "latest.data_health.coverage_pct", minimum=0, maximum=100)

    sources = iter_health_sources(health)
    require(bool(sources), "latest.data_health.sources must not be empty")
    seen: set[str] = set()
    for source_id, source in sources:
        require(source_id not in seen, f"duplicate data-health source id: {source_id}")
        seen.add(source_id)
        source_status = require_string(source.get("status"), f"data-health source {source_id}.status").casefold()
        require(source_status in ALLOWED_HEALTH, f"data-health source {source_id} has unsupported status")
        timestamp_key = next(
            (
                key
                for key in ("latest_observation", "as_of", "observed_at", "updated_at", "fetched_at", "data_cutoff")
                if source.get(key) is not None
            ),
            None,
        )
        age_key = next(
            (key for key in ("age_hours", "freshness_hours", "freshness_days", "age_days") if source.get(key) is not None),
            None,
        )
        require(
            timestamp_key is not None
            or age_key is not None
            or source_status in {"unavailable", "missing", "stale_or_invalid", "unknown"},
            f"data-health source {source_id} must report freshness or be unavailable",
        )
        if timestamp_key:
            observed = parse_temporal(source[timestamp_key], f"data-health source {source_id}.{timestamp_key}")
            require(observed <= generated_at, f"data-health source {source_id} cannot be dated after generation")
        if age_key:
            require_number(source[age_key], f"data-health source {source_id}.{age_key}", minimum=0)
    return sources


def validate_calibration(value: Any, model: Mapping[str, Any]) -> None:
    calibration = require_object(value, "latest.calibration")
    if "metrics" in calibration:
        require_keys(calibration, {"protocol", "metrics", "metric_definitions"}, "latest.calibration")
        require_string(calibration["protocol"], "latest.calibration.protocol")
        metrics = require_object(calibration["metrics"], "latest.calibration.metrics")
        require(set(metrics) == HORIZON_SET, "latest.calibration.metrics must match canonical horizons")
        for horizon in HORIZONS:
            item = require_object(metrics[horizon], f"latest.calibration.metrics.{horizon}")
            require_keys(
                item,
                {
                    "status",
                    "forecast_count",
                    "event_count",
                    "brier_score",
                    "log_loss",
                    "climatology_brier_score",
                    "brier_skill_vs_climatology",
                    "calibration_error",
                },
                f"latest.calibration.metrics.{horizon}",
            )
            require_string(item["status"], f"calibration metric {horizon}.status")
            count = require_number(item["forecast_count"], f"calibration metric {horizon}.forecast_count", minimum=0)
            events = require_number(item["event_count"], f"calibration metric {horizon}.event_count", minimum=0)
            require(events <= count, f"calibration metric {horizon} event_count cannot exceed forecast_count")
            for key in ("brier_score", "log_loss", "climatology_brier_score", "calibration_error"):
                if item[key] is not None:
                    maximum = 1 if key != "log_loss" else None
                    require_number(item[key], f"calibration metric {horizon}.{key}", minimum=0, maximum=maximum)
            if item["brier_skill_vs_climatology"] is not None:
                require_number(item["brier_skill_vs_climatology"], f"calibration metric {horizon}.brier_skill_vs_climatology")
    else:
        # Compatibility with the early versioned fixture contract.
        require_keys(calibration, {"status", "method"}, "latest.calibration")
        status = require_string(calibration["status"], "latest.calibration.status").casefold()
        require(status in {"calibrated", "experimental", "uncalibrated", "insufficient_history"}, "unsupported calibration status")
        require_string(calibration["method"], "latest.calibration.method")
        for key in ("brier_score", "log_loss"):
            if calibration.get(key) is not None:
                require_number(calibration[key], f"latest.calibration.{key}", minimum=0)
        if "sample_size" in calibration:
            require_number(calibration["sample_size"], "latest.calibration.sample_size", minimum=0)
        output_type = str(model.get("output_type", "")).casefold()
        if output_type in {"probability", "calibrated_probability"}:
            require(status == "calibrated", "a probability output requires calibration.status=calibrated")
            require(calibration.get("sample_size", 0) > 0, "a probability output requires a positive calibration sample")


def validate_latest(latest: Any) -> Mapping[str, Any]:
    latest = require_object(latest, "latest.json")
    validate_json_numbers(latest, "latest.json")
    required = {
        "generated_at",
        "primary_horizon",
        "thresholds",
        "curve",
        "primary_score",
        "headline",
        "briefing",
        "summary",
        "why_read",
        "trigger_cards",
        "charts",
        "market",
        "macro",
        "news",
        "reasons",
        "watchlist",
        "history_entry",
    }
    require_keys(latest, required, "latest.json")

    generated_at = parse_timestamp(latest["generated_at"], "latest.generated_at")
    primary_horizon = require_string(latest["primary_horizon"], "latest.primary_horizon")
    require(primary_horizon in HORIZON_SET, "latest.primary_horizon is unsupported")
    thresholds = validate_horizon_map(latest["thresholds"], "latest.thresholds", minimum=0.000001, maximum=100)
    curve = validate_horizon_map(latest["curve"], "latest.curve", minimum=0, maximum=100)
    primary_score = require_number(latest["primary_score"], "latest.primary_score", minimum=0, maximum=100)
    require(primary_score == float(curve[primary_horizon]), "primary_score must match curve at primary_horizon")
    require_string(latest["headline"], "latest.headline")

    summary = require_object(latest["summary"], "latest.summary")
    require_keys(summary, {"deck", "primary_message", "market_message", "macro_message", "news_message"}, "latest.summary")
    briefing = require_object(latest["briefing"], "latest.briefing")
    require_keys(
        briefing,
        {"stance", "probability", "primary_horizon", "confidence", "caveat_severity", "caveat_message", "house_call"},
        "latest.briefing",
    )
    require(briefing["primary_horizon"] == primary_horizon, "briefing primary horizon must match latest")
    require_number(briefing["probability"], "latest.briefing.probability", minimum=0, maximum=100)
    require(float(briefing["probability"]) == primary_score, "briefing probability/score must match primary_score")
    require_list(latest["why_read"], "latest.why_read", nonempty=True)
    require_list(latest["trigger_cards"], "latest.trigger_cards", nonempty=True)
    charts = require_object(latest["charts"], "latest.charts")
    require_keys(charts, {"market_trend", "score_history"}, "latest.charts")
    require_list(latest["reasons"], "latest.reasons", nonempty=True)
    require_list(latest["watchlist"], "latest.watchlist", nonempty=True)

    history_entry = require_object(latest["history_entry"], "latest.history_entry")
    parse_timestamp(history_entry.get("as_of"), "latest.history_entry.as_of")
    require(history_entry.get("primary_horizon") == primary_horizon, "history_entry primary horizon must match latest")
    require_number(history_entry.get("primary_score"), "latest.history_entry.primary_score", minimum=0, maximum=100)
    require(float(history_entry["primary_score"]) == primary_score, "history_entry score must match latest")
    validate_horizon_map(history_entry.get("curve"), "latest.history_entry.curve", minimum=0, maximum=100)

    # Version 2 snapshots must publish the metadata that makes forecasts
    # reproducible and prevents an index from masquerading as a probability.
    if "schema_version" in latest:
        require_string(latest["schema_version"], "latest.schema_version")
        require_keys(
            latest,
            {
                "forecast_id",
                "model",
                "data_cutoff",
                "baseline",
                "uncertainty",
                "data_health",
                "calibration",
                "event_definition",
            },
            "versioned latest.json",
        )
        forecast_id = require_string(latest["forecast_id"], "latest.forecast_id")
        require(bool(FORECAST_ID_RE.fullmatch(forecast_id)), "latest.forecast_id has an invalid format")
        cutoff = parse_temporal(latest["data_cutoff"], "latest.data_cutoff")
        require(cutoff <= generated_at, "latest.data_cutoff cannot be after generated_at")
        model = require_object(latest["model"], "latest.model")
        validate_model(model)
        validate_event_definition(latest["event_definition"], thresholds)
        validate_baseline(latest["baseline"], cutoff)
        validate_uncertainty(latest["uncertainty"], curve)
        validate_data_health(latest["data_health"], generated_at=generated_at)
        validate_calibration(latest["calibration"], model)
        if "path_risk" in latest:
            validate_path_risk(latest["path_risk"], thresholds)
        if "expert_view" in latest:
            validate_expert_view(latest["expert_view"], latest)
        if "forecast_id" in history_entry:
            require(history_entry["forecast_id"] == forecast_id, "history_entry forecast_id must match latest")
    return latest


def validate_history(history: Any, latest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    entries = require_list(history, "history.json", nonempty=True)
    validated: list[Mapping[str, Any]] = []
    previous: datetime | None = None
    ids: set[str] = set()
    for index, raw in enumerate(entries):
        entry = require_object(raw, f"history[{index}]")
        require_keys(entry, {"as_of", "primary_horizon", "primary_score", "curve"}, f"history[{index}]")
        as_of = parse_timestamp(entry["as_of"], f"history[{index}].as_of")
        require(previous is None or as_of > previous, "history entries must be strictly chronological and unique")
        previous = as_of
        horizon = require_string(entry["primary_horizon"], f"history[{index}].primary_horizon")
        require(horizon in HORIZON_SET, f"history[{index}] has unsupported primary horizon")
        curve = validate_horizon_map(entry["curve"], f"history[{index}].curve", minimum=0, maximum=100)
        score = require_number(entry["primary_score"], f"history[{index}].primary_score", minimum=0, maximum=100)
        require(score == float(curve[horizon]), f"history[{index}] score does not match its curve")
        if "forecast_id" in entry:
            forecast_id = require_string(entry["forecast_id"], f"history[{index}].forecast_id")
            require(forecast_id not in ids, f"duplicate forecast_id in history: {forecast_id}")
            ids.add(forecast_id)
        validated.append(entry)

    newest = validated[-1]
    latest_entry = require_object(latest["history_entry"], "latest.history_entry")
    require(newest["as_of"] == latest_entry["as_of"], "history tail as_of does not match latest history_entry")
    require(newest["primary_score"] == latest_entry["primary_score"], "history tail score does not match latest history_entry")
    if latest.get("forecast_id") is not None:
        require(newest.get("forecast_id") == latest["forecast_id"], "history tail forecast_id does not match latest")
    return validated


def ledger_entries(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    ledger = require_object(value, "forecast ledger")
    key = "events" if "events" in ledger else "entries"
    require_keys(ledger, {key}, "forecast ledger")
    return require_list(ledger[key], f"forecast ledger.{key}", nonempty=True)


def canonical_ledger_content_digest(
    issue: Mapping[str, Any],
    identity_version: str | None = None,
) -> str:
    """Recompute a v2 or v3 issuance content address independently."""

    if identity_version is None:
        identity = issue.get("identity")
        identity_version = identity.get("version") if isinstance(identity, dict) else None
    keys = LEDGER_CONTENT_KEYS_BY_VERSION.get(str(identity_version))
    require(keys is not None, "forecast ledger has unsupported identity version")
    require_keys(issue, keys, "forecast ledger identity content")
    content = {key: issue[key] for key in keys}
    try:
        canonical = json.dumps(
            content,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError("forecast ledger identity content must be finite JSON") from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def validate_ledger(value: Any, latest: Mapping[str, Any]) -> None:
    entries = ledger_entries(value)
    require(bool(entries), "forecast ledger must not be empty")
    forecast_ids: set[str] = set()
    event_ids: set[str] = set()
    resolution_keys: set[tuple[str, str, str]] = set()
    previous: datetime | None = None
    issued: list[Mapping[str, Any]] = []
    issued_by_id: dict[str, Mapping[str, Any]] = {}
    terminal_resolutions: list[Mapping[str, Any]] = []
    path_resolutions: list[Mapping[str, Any]] = []
    for index, raw in enumerate(entries):
        entry = require_object(raw, f"forecast ledger[{index}]")
        # Production uses an append-only event log. Keep the early entry-list
        # format readable for a single migration cycle.
        if "event_type" not in entry:
            require_keys(entry, {"forecast_id", "issued_at", "primary_horizon"}, f"forecast ledger[{index}]")
            forecast_id = require_string(entry["forecast_id"], f"forecast ledger[{index}].forecast_id")
            require(bool(FORECAST_ID_RE.fullmatch(forecast_id)), f"forecast ledger[{index}] has invalid forecast_id")
            require(forecast_id not in forecast_ids, f"duplicate forecast_id in ledger: {forecast_id}")
            forecast_ids.add(forecast_id)
            issued_at = parse_timestamp(entry["issued_at"], f"forecast ledger[{index}].issued_at")
            require(previous is None or issued_at > previous, "forecast ledger must be append-only and strictly chronological")
            previous = issued_at
            require(entry["primary_horizon"] in HORIZON_SET, f"forecast ledger[{index}] has unsupported horizon")
            issued.append(entry)
            issued_by_id[forecast_id] = entry
            continue

        require_keys(
            entry,
            {"event_type", "event_id", "forecast_id", "recorded_at"},
            f"forecast ledger[{index}]",
        )
        event_id = require_string(entry["event_id"], f"forecast ledger[{index}].event_id")
        require(event_id not in event_ids, f"duplicate event_id in ledger: {event_id}")
        event_ids.add(event_id)
        forecast_id = require_string(entry["forecast_id"], f"forecast ledger[{index}].forecast_id")
        require(bool(FORECAST_ID_RE.fullmatch(forecast_id)), f"forecast ledger[{index}] has invalid forecast_id")
        recorded_at = parse_timestamp(entry["recorded_at"], f"forecast ledger[{index}].recorded_at")
        require(previous is None or recorded_at >= previous, "forecast ledger events must be chronological")
        previous = recorded_at
        event_type = require_string(entry["event_type"], f"forecast ledger[{index}].event_type")
        if event_type == "forecast_issued":
            require(forecast_id not in forecast_ids, f"duplicate issued forecast_id in ledger: {forecast_id}")
            forecast_ids.add(forecast_id)
            require(
                event_id == f"{forecast_id}:issued",
                f"forecast ledger[{index}] issuance event_id is not canonical",
            )
            require_keys(
                entry,
                {"model_version", "data_cutoff", "baseline", "horizons"},
                f"forecast ledger[{index}]",
            )
            model_version = require_string(entry["model_version"], f"forecast ledger[{index}].model_version")
            cutoff = parse_temporal(entry["data_cutoff"], f"forecast ledger[{index}].data_cutoff")
            baseline = require_object(entry["baseline"], f"forecast ledger[{index}].baseline")
            require_number(baseline.get("value"), f"forecast ledger[{index}].baseline.value", minimum=0.000001)
            baseline_date = parse_temporal(
                baseline.get("observation_date", entry["data_cutoff"]),
                f"forecast ledger[{index}].baseline.observation_date",
            )
            require(baseline_date == cutoff, f"forecast ledger[{index}] baseline must match data_cutoff")
            horizons = require_object(entry["horizons"], f"forecast ledger[{index}].horizons")
            require(set(horizons) == HORIZON_SET, f"forecast ledger[{index}].horizons must match canonical horizons")
            for horizon, specification_raw in horizons.items():
                specification = require_object(specification_raw, f"ledger {forecast_id}.{horizon}")
                require_keys(
                    specification,
                    {"sessions", "threshold_percent", "probability", "uncertainty"},
                    f"ledger {forecast_id}.{horizon}",
                )
                sessions = specification["sessions"]
                require(isinstance(sessions, int) and not isinstance(sessions, bool) and sessions > 0, f"ledger {forecast_id}.{horizon}.sessions must be a positive integer")
                require_number(specification["threshold_percent"], f"ledger {forecast_id}.{horizon}.threshold_percent", minimum=0.000001, maximum=100)
                probability = require_number(specification["probability"], f"ledger {forecast_id}.{horizon}.probability", minimum=0, maximum=100)
                uncertainty = require_object(specification["uncertainty"], f"ledger {forecast_id}.{horizon}.uncertainty")
                require_keys(uncertainty, {"lower_probability", "upper_probability"}, f"ledger {forecast_id}.{horizon}.uncertainty")
                lower = require_number(
                    uncertainty["lower_probability"],
                    f"ledger {forecast_id}.{horizon}.uncertainty.lower_probability",
                    minimum=0,
                    maximum=100,
                )
                upper = require_number(
                    uncertainty["upper_probability"],
                    f"ledger {forecast_id}.{horizon}.uncertainty.upper_probability",
                    minimum=0,
                    maximum=100,
                )
                require(lower <= probability <= upper, f"ledger {forecast_id}.{horizon} probability must lie inside uncertainty")

            identity_raw = entry.get("identity")
            if identity_raw is not None:
                identity = require_object(identity_raw, f"forecast ledger[{index}].identity")
                require_keys(identity, {"version", "content_sha256"}, f"forecast ledger[{index}].identity")
                identity_version = require_string(
                    identity["version"],
                    f"forecast ledger[{index}].identity.version",
                )
                require(
                    identity_version in LEDGER_CONTENT_KEYS_BY_VERSION,
                    f"forecast ledger[{index}] has unsupported identity version",
                )
                identity_keys = LEDGER_CONTENT_KEYS_BY_VERSION[identity_version]
                require_keys(entry, identity_keys, f"forecast ledger[{index}] identity content")
                if identity_version == LEDGER_IDENTITY_VERSION_V2:
                    require(
                        "path_risk" not in entry,
                        f"forecast ledger[{index}] v2 identity must not carry unsealed path risk",
                    )
                digest = require_string(identity["content_sha256"], f"forecast ledger[{index}].identity.content_sha256")
                require(bool(re.fullmatch(r"[0-9a-f]{64}", digest)), f"forecast ledger[{index}] has invalid content digest")
                recomputed = canonical_ledger_content_digest(entry, identity_version)
                require(digest == recomputed, f"forecast ledger[{index}] content digest does not match issued content")
                expected_id = f"fxtry-{entry['data_cutoff']}-{digest[:LEDGER_IDENTITY_DIGEST_LENGTH]}"
                require(forecast_id == expected_id, f"forecast ledger[{index}] forecast_id does not match issued content")
                model = require_object(entry["model"], f"forecast ledger[{index}].model")
                require(model.get("version") == model_version, f"forecast ledger[{index}] model version fields disagree")
                event_thresholds = {
                    horizon: horizons[horizon]["threshold_percent"]
                    for horizon in HORIZONS
                }
                validate_event_definition(entry["event_definition"], event_thresholds)
                if identity_version == LEDGER_IDENTITY_VERSION:
                    validate_path_risk(entry["path_risk"], event_thresholds)

            issued.append(entry)
            issued_by_id[forecast_id] = entry
        elif event_type == "outcome_resolved":
            require_keys(
                entry,
                {
                    "horizon",
                    "target_observation_date",
                    "target_value",
                    "realized_move_percent",
                    "threshold_percent",
                    "outcome",
                    "issued_probability",
                    "model_version",
                },
                f"forecast ledger[{index}]",
            )
            horizon = require_string(entry["horizon"], f"forecast ledger[{index}].horizon")
            require(horizon in HORIZON_SET, f"forecast ledger[{index}] has unsupported resolved horizon")
            resolution_key = (forecast_id, "terminal", horizon)
            require(resolution_key not in resolution_keys, f"duplicate resolution for {forecast_id}.{horizon}")
            resolution_keys.add(resolution_key)
            require(
                event_id == f"{forecast_id}:{horizon}:resolved",
                f"forecast ledger[{index}] resolution event_id is not canonical",
            )
            parse_temporal(entry["target_observation_date"], f"forecast ledger[{index}].target_observation_date")
            require_number(entry["target_value"], f"forecast ledger[{index}].target_value", minimum=0.000001)
            require_number(entry["realized_move_percent"], f"forecast ledger[{index}].realized_move_percent")
            require_number(entry["threshold_percent"], f"forecast ledger[{index}].threshold_percent", minimum=0.000001, maximum=100)
            require(entry["outcome"] in {0, 1}, f"forecast ledger[{index}].outcome must be 0 or 1")
            require_number(entry["issued_probability"], f"forecast ledger[{index}].issued_probability", minimum=0, maximum=100)
            terminal_resolutions.append(entry)
        elif event_type == "path_outcome_resolved":
            require_keys(
                entry,
                {
                    "horizon",
                    "contract",
                    "event_definition",
                    "window_sessions",
                    "window_start_observation_date",
                    "window_end_observation_date",
                    "peak_observation_date",
                    "peak_value",
                    "max_move_percent",
                    "threshold_percent",
                    "outcome",
                    "issued_probability",
                    "model_version",
                },
                f"forecast ledger[{index}]",
            )
            horizon = require_string(entry["horizon"], f"forecast ledger[{index}].horizon")
            require(horizon in HORIZON_SET, f"forecast ledger[{index}] has unsupported path horizon")
            resolution_key = (forecast_id, "path", horizon)
            require(
                resolution_key not in resolution_keys,
                f"duplicate path resolution for {forecast_id}.{horizon}",
            )
            resolution_keys.add(resolution_key)
            require(
                event_id == f"{forecast_id}:path:{horizon}:resolved",
                f"forecast ledger[{index}] path resolution event_id is not canonical",
            )
            require(
                require_string(entry["contract"], f"forecast ledger[{index}].contract").casefold()
                == "any_time_breach",
                f"forecast ledger[{index}] path contract must be any_time_breach",
            )
            sessions = entry["window_sessions"]
            require(
                isinstance(sessions, int) and not isinstance(sessions, bool) and sessions > 0,
                f"forecast ledger[{index}].window_sessions must be a positive integer",
            )
            parse_temporal(
                entry["window_start_observation_date"],
                f"forecast ledger[{index}].window_start_observation_date",
            )
            parse_temporal(
                entry["window_end_observation_date"],
                f"forecast ledger[{index}].window_end_observation_date",
            )
            parse_temporal(
                entry["peak_observation_date"],
                f"forecast ledger[{index}].peak_observation_date",
            )
            require_number(entry["peak_value"], f"forecast ledger[{index}].peak_value", minimum=0.000001)
            require_number(entry["max_move_percent"], f"forecast ledger[{index}].max_move_percent")
            require_number(
                entry["threshold_percent"],
                f"forecast ledger[{index}].threshold_percent",
                minimum=0.000001,
                maximum=100,
            )
            require(
                isinstance(entry["outcome"], bool),
                f"forecast ledger[{index}].outcome must be boolean for path risk",
            )
            require_number(
                entry["issued_probability"],
                f"forecast ledger[{index}].issued_probability",
                minimum=0,
                maximum=100,
            )
            path_resolutions.append(entry)
        else:
            raise ValidationError(f"forecast ledger[{index}] has unsupported event_type {event_type!r}")
    require(bool(issued), "forecast ledger must contain a forecast_issued event")
    latest_forecast_id = require_string(latest.get("forecast_id"), "latest.forecast_id")
    require(latest_forecast_id in issued_by_id, "latest forecast_id has no matching issuance event")
    latest_issue = issued_by_id[latest_forecast_id]
    require(
        parse_temporal(latest_issue["data_cutoff"], "latest ledger issuance data_cutoff")
        == parse_temporal(latest["data_cutoff"], "latest.data_cutoff"),
        "latest data_cutoff does not match its issuance event",
    )
    require(latest_issue["baseline"] == latest["baseline"], "latest baseline does not match its issuance event")
    latest_model = require_object(latest["model"], "latest.model")
    if "model" in latest_issue:
        require(latest_issue["model"] == latest_model, "latest model does not match its issuance event")
    else:
        require(
            latest_issue["model_version"] == latest_model.get("version"),
            "latest model version does not match its issuance event",
        )
    latest_curve = require_object(latest["curve"], "latest.curve")
    latest_thresholds = require_object(latest["thresholds"], "latest.thresholds")
    latest_uncertainty = require_object(latest["uncertainty"], "latest.uncertainty")
    latest_sessions = require_object(latest["event_definition"], "latest.event_definition").get("horizon_sessions")
    for horizon in HORIZONS:
        specification = require_object(latest_issue["horizons"][horizon], f"latest issuance {horizon}")
        require(specification["probability"] == latest_curve[horizon], f"latest curve.{horizon} does not match its issuance event")
        require(specification["threshold_percent"] == latest_thresholds[horizon], f"latest threshold.{horizon} does not match its issuance event")
        require(specification["uncertainty"] == latest_uncertainty[horizon], f"latest uncertainty.{horizon} does not match its issuance event")
        if isinstance(latest_sessions, dict):
            require(specification["sessions"] == latest_sessions[horizon], f"latest sessions.{horizon} does not match its issuance event")
    published_forecast: Mapping[str, Any] | None = None
    if "forecast" in latest:
        published_forecast = require_object(latest["forecast"], "latest.forecast")
    top_level_path = latest.get("path_risk")
    nested_path = published_forecast.get("path_risk") if published_forecast is not None else None
    if top_level_path is not None and nested_path is not None:
        require(top_level_path == nested_path, "latest path_risk disagrees with latest.forecast.path_risk")
    latest_path = top_level_path if top_level_path is not None else nested_path
    issue_path = latest_issue.get("path_risk")
    latest_identity = latest_issue.get("identity")
    latest_identity_version = (
        latest_identity.get("version") if isinstance(latest_identity, dict) else None
    )
    if latest_identity_version == LEDGER_IDENTITY_VERSION:
        require(latest_path is not None, "latest path_risk is missing from its v3 issuance")
        require(issue_path == latest_path, "latest path_risk does not match its issuance event")
    else:
        require(latest_path is None, "latest path_risk is not sealed by a v3 issuance identity")

    if "identity" in latest_issue and published_forecast is not None:
        identity_keys = LEDGER_CONTENT_KEYS_BY_VERSION.get(str(latest_identity_version), ())
        for key in identity_keys:
            require(
                latest_issue[key] == published_forecast.get(key),
                f"latest forecast.{key} does not match its content-addressed issuance event",
            )

    terminal_by_key = {
        (str(resolution["forecast_id"]), str(resolution["horizon"])): resolution
        for resolution in terminal_resolutions
    }
    for resolution in terminal_resolutions:
        forecast_id = resolution["forecast_id"]
        require(forecast_id in issued_by_id, f"resolution references unknown forecast_id {forecast_id}")
        issue = issued_by_id[forecast_id]
        horizon = resolution["horizon"]
        specification = require_object(issue["horizons"][horizon], f"issued forecast {forecast_id}.{horizon}")
        require(
            resolution["issued_probability"] == specification["probability"],
            f"resolution probability does not match issued forecast {forecast_id}.{horizon}",
        )
        require(
            resolution["threshold_percent"] == specification["threshold_percent"],
            f"resolution threshold does not match issued forecast {forecast_id}.{horizon}",
        )
        require(
            resolution["model_version"] == issue["model_version"],
            f"resolution model version does not match issued forecast {forecast_id}",
        )
        baseline = require_object(issue["baseline"], f"issued forecast {forecast_id}.baseline")
        baseline_value = require_number(
            baseline.get("value"),
            f"issued forecast {forecast_id}.baseline.value",
            minimum=0.000001,
        )
        target_value = float(resolution["target_value"])
        expected_move = ((target_value / baseline_value) - 1.0) * 100.0
        require(
            math.isclose(float(resolution["realized_move_percent"]), expected_move, abs_tol=0.00001),
            f"resolution move was not calculated from immutable issued baseline for {forecast_id}.{horizon}",
        )
        target_date = parse_temporal(
            resolution["target_observation_date"],
            f"resolution {forecast_id}.{horizon}.target_observation_date",
        )
        baseline_date = parse_temporal(
            baseline.get("observation_date", issue["data_cutoff"]),
            f"issued forecast {forecast_id}.baseline.observation_date",
        )
        require(target_date > baseline_date, f"resolution target must follow baseline for {forecast_id}.{horizon}")
        implied = int(float(resolution["realized_move_percent"]) >= float(resolution["threshold_percent"]))
        require(resolution["outcome"] == implied, f"resolution outcome is inconsistent for {forecast_id}.{horizon}")

    for resolution in path_resolutions:
        forecast_id = str(resolution["forecast_id"])
        require(forecast_id in issued_by_id, f"path resolution references unknown forecast_id {forecast_id}")
        issue = issued_by_id[forecast_id]
        identity = issue.get("identity")
        require(
            isinstance(identity, dict) and identity.get("version") == LEDGER_IDENTITY_VERSION,
            f"path resolution references an issue without a sealed v3 path contract: {forecast_id}",
        )
        path_risk = require_object(issue.get("path_risk"), f"issued forecast {forecast_id}.path_risk")
        path_horizons = require_object(
            path_risk.get("horizons"),
            f"issued forecast {forecast_id}.path_risk.horizons",
        )
        horizon = str(resolution["horizon"])
        specification = require_object(
            path_horizons.get(horizon),
            f"issued path forecast {forecast_id}.{horizon}",
        )
        require(
            resolution["issued_probability"] == specification.get("probability"),
            f"path resolution probability does not match issued forecast {forecast_id}.{horizon}",
        )
        require(
            resolution["threshold_percent"] == specification.get("threshold_percent"),
            f"path resolution threshold does not match issued forecast {forecast_id}.{horizon}",
        )
        require(
            resolution["window_sessions"] == specification.get("sessions"),
            f"path resolution window does not match issued forecast {forecast_id}.{horizon}",
        )
        require(
            resolution["model_version"] == issue["model_version"],
            f"path resolution model version does not match issued forecast {forecast_id}",
        )
        issued_definition = require_object(
            path_risk.get("event_definition"),
            f"issued forecast {forecast_id}.path_risk.event_definition",
        )
        require(
            resolution["event_definition"] == issued_definition,
            f"path resolution event definition does not match issued forecast {forecast_id}.{horizon}",
        )

        baseline = require_object(issue["baseline"], f"issued forecast {forecast_id}.baseline")
        baseline_value = require_number(
            baseline.get("value"),
            f"issued forecast {forecast_id}.baseline.value",
            minimum=0.000001,
        )
        baseline_date = parse_temporal(
            baseline.get("observation_date", issue["data_cutoff"]),
            f"issued forecast {forecast_id}.baseline.observation_date",
        )
        window_start = parse_temporal(
            resolution["window_start_observation_date"],
            f"path resolution {forecast_id}.{horizon}.window_start_observation_date",
        )
        window_end = parse_temporal(
            resolution["window_end_observation_date"],
            f"path resolution {forecast_id}.{horizon}.window_end_observation_date",
        )
        peak_date = parse_temporal(
            resolution["peak_observation_date"],
            f"path resolution {forecast_id}.{horizon}.peak_observation_date",
        )
        require(
            baseline_date < window_start <= peak_date <= window_end,
            f"path resolution window/peak dates are inconsistent for {forecast_id}.{horizon}",
        )

        terminal = terminal_by_key.get((forecast_id, horizon))
        require(
            terminal is not None,
            f"path resolution requires a terminal resolution for the complete window {forecast_id}.{horizon}",
        )
        terminal_date = parse_temporal(
            terminal["target_observation_date"],
            f"terminal resolution {forecast_id}.{horizon}.target_observation_date",
        )
        require(
            window_end == terminal_date,
            f"path resolution window end does not match t+h for {forecast_id}.{horizon}",
        )
        peak_value = float(resolution["peak_value"])
        require(
            peak_value >= float(terminal["target_value"]),
            f"path peak must include the terminal observation for {forecast_id}.{horizon}",
        )
        expected_move = ((peak_value / baseline_value) - 1.0) * 100.0
        require(
            math.isclose(float(resolution["max_move_percent"]), expected_move, abs_tol=0.00001),
            f"path resolution move was not calculated from immutable issued baseline for {forecast_id}.{horizon}",
        )
        implied = expected_move >= float(resolution["threshold_percent"])
        require(
            resolution["outcome"] is implied,
            f"path resolution outcome is inconsistent for {forecast_id}.{horizon}",
        )
        if terminal["outcome"] == 1:
            require(
                resolution["outcome"] is True,
                f"path outcome must include a positive terminal event for {forecast_id}.{horizon}",
            )


def validate_cache_coherence(
    source_cache: Any,
    latest: Mapping[str, Any],
) -> None:
    cache = require_object(source_cache, "source_cache.json")
    require(bool(cache), "source_cache.json must not be empty")
    validate_json_numbers(cache, "source_cache.json")
    if "data_health" not in latest:
        return
    cache_sources_raw = cache.get("sources", cache)
    cache_sources = require_object(cache_sources_raw, "source_cache.json.sources")
    health = require_object(latest["data_health"], "latest.data_health")
    for source_id, source in iter_health_sources(health):
        cache_key = source.get("cache_key")
        uses_cache = (
            source.get("cached") is True
            or source.get("used_cache") is True
            or str(source.get("status", "")).casefold() in {"cached", "cached_fallback"}
        )
        if cache_key is not None:
            cache_key = require_string(cache_key, f"data-health source {source_id}.cache_key")
            require(cache_key in cache_sources, f"data-health source {source_id} references missing cache key {cache_key}")
        else:
            require(source_id in cache_sources, f"data-health source {source_id} has no matching cache entry")
        if uses_cache:
            entry = require_object(cache_sources.get(cache_key or source_id), f"source cache {cache_key or source_id}")
            require(entry.get("payload") is not None, f"cached data-health source {source_id} has no cached payload")


def validate_static_files(root: Path) -> None:
    docs = root / "docs"
    required = [
        docs / "index.html",
        docs / "methodology.html",
        docs / "app.js",
        docs / "style.css",
        root / "CHANGELOG.md",
        root / "SECURITY.md",
    ]
    for path in required:
        require(path.exists(), f"Missing required browser file: {path}")

    index_html = (docs / "index.html").read_text(encoding="utf-8")
    app_js = (docs / "app.js").read_text(encoding="utf-8")
    require("./style.css" in index_html, "index.html must load the browser stylesheet")
    require("./app.js" in index_html, "index.html must load the browser script")
    require('name="theme-color"' in index_html, "index.html must define a theme color")
    require('name="referrer"' in index_html, "index.html must define a referrer policy")
    require("Content-Security-Policy" in index_html, "index.html must define a content security policy")
    require("./methodology.html" in index_html, "index.html must link to the methodology page")
    require("./data/latest.json" in app_js, "app.js must request latest.json")
    require("./data/history.json" in app_js, "app.js must request history.json")
    forbidden = {
        r"\.innerHTML\s*=": "raw innerHTML assignment",
        r"\.outerHTML\s*=": "raw outerHTML assignment",
        r"\bdocument\.write\s*\(": "document.write",
        r"(?:^|[^A-Za-z0-9_$])eval\s*\(": "eval",
        r"\bnew\s+Function\s*\(": "dynamic Function construction",
    }
    for pattern, description in forbidden.items():
        require(re.search(pattern, app_js, flags=re.MULTILINE) is None, f"app.js must not use {description}")


def find_ledger_path(data_dir: Path) -> Path | None:
    candidates = [data_dir / "forecast_ledger.json", data_dir / "forecast-ledger.json"]
    existing = [path for path in candidates if path.exists()]
    require(len(existing) <= 1, "only one canonical forecast-ledger filename may exist")
    return existing[0] if existing else None


def validate_bundle(root: Path = ROOT) -> None:
    docs = root / "docs"
    data_dir = docs / "data"
    latest_path = data_dir / "latest.json"
    history_path = data_dir / "history.json"
    cache_path = data_dir / "source_cache.json"
    for path in (latest_path, history_path, cache_path):
        require(path.exists(), f"Missing required data file: {path}")

    latest = validate_latest(read_json(latest_path))
    validate_history(read_json(history_path), latest)
    validate_cache_coherence(read_json(cache_path), latest)

    ledger_path = find_ledger_path(data_dir)
    if "schema_version" in latest:
        require(ledger_path is not None, "versioned snapshots require a forecast ledger")
    if ledger_path is not None:
        validate_ledger(read_json(ledger_path), latest)
    validate_static_files(root)


def main() -> None:
    try:
        validate_bundle(ROOT)
    except ValidationError as exc:
        raise SystemExit(f"Browser bundle validation failed: {exc}") from exc
    print("Browser bundle validation passed.")


if __name__ == "__main__":
    main()
