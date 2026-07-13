"""Leakage-controlled, interpretable USD/TRY empirical probability model.

The model deliberately uses only the derived ECB USD/TRY history. Other public
feeds remain contextual evidence: their release schedules and revisions make it
unsafe to silently splice today's values into a historical training matrix.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from datetime import datetime
from statistics import mean, median
from typing import Sequence


MODEL_VERSION = "empirical-regime-v2.2.0"
HORIZON_SESSIONS = {"1w": 5, "1m": 22, "3m": 66, "6m": 132, "1y": 264}
FEATURE_WINDOW = 20
MINIMUM_HISTORY = 750
MINIMUM_TRAINING = 400
PRIOR_STRENGTH = 36.0
CALIBRATION_PRIOR_STRENGTH = 30.0
BOOTSTRAP_REPLICATIONS = 399


@dataclass(frozen=True)
class FeatureRow:
    index: int
    observed_at: datetime
    momentum_5d: float
    momentum_20d: float
    realized_volatility_20d: float
    acceleration: float


@dataclass(frozen=True)
class LabelledRow:
    features: FeatureRow
    target_index: int
    outcome: int


@dataclass(frozen=True)
class Prediction:
    forecast_index: int
    target_index: int
    raw_probability: float
    probability: float
    climatology_probability: float
    outcome: int
    conditional_count: int
    calibration_count: int
    unconstrained_probability: float | None = None


@dataclass(frozen=True)
class UncertaintyInterval:
    lower: float
    upper: float
    effective_sample_size: int
    block_length_sessions: int
    calibration_block_length_forecasts: int
    calibration_evidence_count: int


def build_empirical_forecast(points: Sequence[object], thresholds: dict[str, float]) -> dict:
    if len(points) < MINIMUM_HISTORY:
        raise ValueError(
            f"empirical forecast requires at least {MINIMUM_HISTORY} USD/TRY observations; received {len(points)}"
        )
    features = _feature_rows(points)
    by_index = {row.index: row for row in features}
    current = features[-1]
    horizons: dict[str, dict] = {}
    touch_horizons: dict[str, dict] = {}
    all_metrics: dict[str, dict] = {}
    all_touch_metrics: dict[str, dict] = {}
    all_drivers: dict[str, list[dict]] = {}
    all_touch_drivers: dict[str, list[dict]] = {}

    for horizon, sessions in HORIZON_SESSIONS.items():
        threshold = float(thresholds[horizon])
        terminal, terminal_backtest, terminal_probability = _estimate_horizon(
            points,
            features,
            by_index,
            current,
            horizon=horizon,
            sessions=sessions,
            threshold=threshold,
            contract="exact_terminal",
        )
        touch, _touch_backtest, _touch_probability = _estimate_horizon(
            points,
            features,
            by_index,
            current,
            horizon=horizon,
            sessions=sessions,
            threshold=threshold,
            contract="any_time_breach",
            coherence_floor_probability=terminal_probability,
            coherence_floor_predictions=terminal_backtest,
        )
        horizons[horizon] = terminal
        touch_horizons[horizon] = touch
        all_metrics[horizon] = terminal["calibration"]
        all_touch_metrics[horizon] = touch["calibration"]
        all_drivers[horizon] = terminal["signed_drivers"]
        all_touch_drivers[horizon] = touch["signed_drivers"]

    cutoff_date = points[-1].observed_at.strftime("%Y-%m-%d")
    cutoff = f"{cutoff_date}T00:00:00Z"
    baseline = round(float(points[-1].value), 6)
    is_calibrated = all(
        specification["sample"]["calibration_examples"] >= 20
        and specification["calibration_status"] == "calibrated"
        for specification in horizons.values()
    )
    return {
        "model": {
            "name": "Empirical regime-conditioned USD/TRY model",
            "version": MODEL_VERSION,
            "status": "calibrated" if is_calibrated else "experimental",
            "is_calibrated": is_calibrated,
            "output_type": "calibrated_probability" if is_calibrated else "experimental_probability",
            "probability_scale": "0-100 percent",
            "primary_contract": "exact_terminal",
            "secondary_contract": "any_time_breach",
            "method": (
                "Expanding-window historical event rate conditioned on as-of 5-session momentum, "
                "20-session momentum, realized volatility and acceleration; Bayesian shrinkage to "
                "the as-of climatology; rolling reliability recalibration. Exact-terminal and "
                "any-time-breach contracts are estimated separately from their own labels."
            ),
            "predictors": [
                "USD/TRY 5-session percentage change",
                "USD/TRY 20-session percentage change",
                "USD/TRY 20-session annualized realized volatility",
                "5-session acceleration relative to the 20-session trend",
            ],
            "excluded_context": (
                "Contemporaneous macro, reserve, volatility and news inputs are contextual only and do not alter "
                "the probability until point-in-time histories with release timestamps are available."
            ),
            "training_protocol": (
                "For every historical forecast, training labels are admitted only after their complete target "
                "window ends. Calibration uses only earlier forecasts whose outcomes were observable at that time."
            ),
            "limitations": [
                "The reference rate is derived from ECB EUR/TRY and EUR/USD observations, not an executable quote.",
                "Long-horizon backtest outcomes overlap even though evaluation forecasts are sampled no more often than monthly.",
                "Moving-block intervals retain observed target-window dependence but describe sampling uncertainty, not structural breaks.",
                "Backtest metrics are research evidence, not a guarantee of future performance.",
                "Any-time-breach estimates use observed ECB daily reference-rate observations only; intraday breaches are not observed.",
            ],
        },
        "data_cutoff": cutoff,
        "baseline": {
            "pair": "USD/TRY",
            "value": baseline,
            "spot": baseline,
            "observation_date": cutoff_date,
            "observed_at": cutoff,
            "source": "ECB reference rates, USD/TRY derived as EUR/TRY divided by EUR/USD",
            "price_type": "daily reference rate",
        },
        "target": {
            "variable": "future derived ECB USD/TRY daily reference rate",
            "direction": "TRY depreciation / USD/TRY increase",
            "horizon_unit": "ECB trading observations",
        },
        "event_definition": {
            "event_type": "exact_terminal",
            "primary": True,
            "pair": "USD/TRY",
            "direction": "TRY depreciation / USD/TRY increase",
            "measurement": "ECB reference-rate change over an exact count of aligned observations",
            "statement": (
                "For each horizon, the event occurs when the future USD/TRY reference rate is greater than or "
                "equal to the baseline multiplied by one plus that horizon's threshold."
            ),
            "thresholds_percent": thresholds,
            "thresholds": thresholds,
            "horizon_sessions": HORIZON_SESSIONS,
            "baseline_rule": "latest common-date ECB EUR/TRY and EUR/USD observation available at publication",
            "target_rule": "observation exactly h ECB trading observations after baseline",
        },
        "horizons": horizons,
        "backtest": {
            "protocol": "strict expanding-window, target-window-purged training and as-of reliability calibration",
            "metrics": all_metrics,
            "metric_definitions": {
                "brier_score": "mean squared probability error; lower is better",
                "log_loss": "mean negative Bernoulli log likelihood; lower is better",
                "brier_skill_vs_climatology": (
                    "1 - model Brier / target-purged as-of climatology Brier; each benchmark probability "
                    "is stored when its forecast is formed; positive is better"
                ),
                "calibration_error": "weighted absolute forecast-vs-outcome gap across probability bins; lower is better",
            },
        },
        "path_risk": {
            "contract": "any_time_breach",
            "primary": False,
            "event_definition": {
                "event_type": "any_time_breach",
                "pair": "USD/TRY",
                "direction": "TRY depreciation / USD/TRY increase",
                "measurement": (
                    "maximum derived ECB USD/TRY reference rate across observations t+1 through t+h, inclusive"
                ),
                "statement": (
                    "For each horizon, the any-time-breach event occurs when at least one derived ECB USD/TRY "
                    "daily reference-rate observation from t+1 through t+h, inclusive, is greater than or "
                    "equal to the baseline multiplied by one plus that horizon's threshold. The outcome is "
                    "zero only after observation t+h is available."
                ),
                "relationship_to_terminal": (
                    "This is a separate path-dependent contract. It can resolve to 1 even when the primary "
                    "exact-terminal contract resolves to 0; the exact-terminal forecast remains primary."
                ),
                "thresholds_percent": thresholds,
                "horizon_sessions": HORIZON_SESSIONS,
                "baseline_rule": "latest common-date ECB EUR/TRY and EUR/USD observation available at publication",
                "window_start_rule": "first derived ECB USD/TRY observation after baseline (t+1)",
                "target_rule": "all derived ECB USD/TRY observations t+1 through t+h, inclusive",
                "resolution_rule": (
                    "Resolve to 1 if any in-window observation touches or exceeds the threshold; otherwise "
                    "resolve to 0 only when the complete h-observation window has ended."
                ),
                "observation_limit": (
                    "Observed ECB daily reference-rate observations only; intraday threshold breaches are outside the contract."
                ),
            },
            "coherence_constraint": {
                "rule": "P(any-time breach by h) = max(unconstrained path estimate, P(exact-terminal breach at h))",
                "rationale": (
                    "The exact-terminal event is a subset of the inclusive any-time-breach event, so its "
                    "probability is a mathematical lower bound on path risk."
                ),
                "backtest_application": (
                    "Applied to matched same-forecast-index walk-forward probabilities before touch metrics "
                    "and calibration-status gates are computed."
                ),
            },
            "horizons": touch_horizons,
            "backtest": {
                "protocol": (
                    "strict expanding-window, complete-touch-window-purged training and as-of reliability calibration"
                ),
                "metrics": all_touch_metrics,
                "metric_definitions": {
                    "brier_score": "mean squared probability error; lower is better",
                    "log_loss": "mean negative Bernoulli log likelihood; lower is better",
                    "brier_skill_vs_climatology": (
                        "1 - touch-model Brier / target-purged touch-climatology Brier; each benchmark "
                        "probability is stored when its forecast is formed; positive is better"
                    ),
                    "calibration_error": (
                        "weighted absolute forecast-vs-outcome gap across probability bins; lower is better"
                    ),
                },
            },
            "signed_drivers": all_touch_drivers,
        },
        "signed_drivers": all_drivers,
    }


def _estimate_horizon(
    points: Sequence[object],
    features: Sequence[FeatureRow],
    by_index: dict[int, FeatureRow],
    current: FeatureRow,
    *,
    horizon: str,
    sessions: int,
    threshold: float,
    contract: str,
    coherence_floor_probability: float | None = None,
    coherence_floor_predictions: Sequence[Prediction] | None = None,
) -> tuple[dict, list[Prediction], float]:
    """Fit one event contract without sharing outcomes across contracts."""

    if contract == "exact_terminal":
        labelled = _labelled_rows(points, features, sessions, threshold)
    elif contract == "any_time_breach":
        labelled = _touch_labelled_rows(points, features, sessions, threshold)
    else:
        raise ValueError(f"unsupported forecast contract: {contract}")

    # Publication is represented by as_of_index=current.index + 1, so the
    # equivalent strict purge admits target_index <= current.index. Historical
    # forecasts retain target_index < forecast_index in _walk_forward_backtest;
    # neither path admits a partial target window.
    current_training = [row for row in labelled if row.target_index <= current.index]
    if len(current_training) < MINIMUM_TRAINING:
        raise ValueError(
            f"{horizon} {contract} forecast has only {len(current_training)} leakage-safe training examples"
        )

    unconstrained_backtest = _walk_forward_backtest(labelled, by_index, sessions)
    raw, conditional_count, regime = _conditional_probability(current, current_training)
    unconstrained_calibrated, calibration_count = _calibrate(
        raw,
        unconstrained_backtest,
        as_of_index=current.index + 1,
    )
    calibrated = unconstrained_calibrated
    backtest = unconstrained_backtest
    coherence_adjustments = 0
    coherence_max_adjustment = 0.0
    if contract == "any_time_breach":
        if coherence_floor_probability is None or coherence_floor_predictions is None:
            raise ValueError("any_time_breach estimation requires matched exact-terminal coherence floors")
        backtest, coherence_adjustments, coherence_max_adjustment = _coherent_touch_predictions(
            unconstrained_backtest,
            coherence_floor_predictions,
        )
        calibrated = max(unconstrained_calibrated, coherence_floor_probability)
    challenger = _climatology_probability(current_training)
    metrics = _metrics(backtest)
    if contract == "any_time_breach":
        metrics["probability_series"] = "coherence_constrained_any_time_breach"
        metrics["coherence_adjusted_forecast_count"] = coherence_adjustments
        metrics["coherence_max_adjustment_percentage_points"] = round(
            coherence_max_adjustment * 100.0,
            4,
        )
        metrics["unconstrained_metrics"] = _metrics(unconstrained_backtest)
    calibration_status = _calibration_status(metrics)
    interval = _uncertainty_interval(
        calibrated,
        raw_probability=raw,
        current=current,
        training=current_training,
        earlier_predictions=backtest,
        sessions=sessions,
        as_of_index=current.index + 1,
    )
    drivers = _signed_drivers(current, current_training)
    baseline = float(points[-1].value)
    threshold_value = baseline * (1.0 + threshold / 100.0)
    if contract == "exact_terminal":
        event = {
            "event_type": contract,
            "primary": True,
            "baseline_value": round(baseline, 6),
            "threshold_value": round(threshold_value, 6),
            "operator": ">=",
            "observation_offset": sessions,
            "formula": f"USDTRY[t+{sessions}] / USDTRY[t] - 1 >= {threshold / 100.0:.6f}",
            "contract_text": (
                f"Resolve to 1 only if the derived ECB USD/TRY observation exactly at t+{sessions} "
                f"is at least {threshold:.6f}% above USD/TRY at t; otherwise resolve to 0."
            ),
        }
    else:
        event = {
            "event_type": contract,
            "primary": False,
            "baseline_value": round(baseline, 6),
            "threshold_value": round(threshold_value, 6),
            "operator": ">=",
            "window_start_offset": 1,
            "window_end_offset": sessions,
            "window_inclusive": True,
            "formula": (
                f"max(USDTRY[t+1], ..., USDTRY[t+{sessions}]) / USDTRY[t] - 1 "
                f">= {threshold / 100.0:.6f}"
            ),
            "contract_text": (
                f"Resolve to 1 if any derived ECB USD/TRY observation from t+1 through t+{sessions}, "
                f"inclusive, touches or exceeds {threshold:.6f}% above USD/TRY at t; otherwise resolve "
                f"to 0 only after observation t+{sessions} is available."
            ),
        }

    benchmark = {
        "name": "Current target-purged climatology",
        "role": "challenger",
        "probability": round(challenger * 100.0, 1),
        "delta_model_minus_benchmark_percentage_points": round(
            (calibrated - challenger) * 100.0,
            1,
        ),
        "training_examples": len(current_training),
        "method": "Laplace-smoothed event rate from labels whose complete target window has resolved",
        "purge_rule": "target_index <= current forecast index; no partial target windows admitted",
    }
    result = {
        "horizon": horizon,
        "sessions": sessions,
        "threshold_percent": threshold,
        "probability": round(calibrated * 100.0, 1),
        "raw_probability": round(raw * 100.0, 1),
        "uncertainty": {
            "level": 90,
            "lower": round(interval.lower * 100.0, 1),
            "upper": round(interval.upper * 100.0, 1),
            "lower_probability": round(interval.lower * 100.0, 1),
            "upper_probability": round(interval.upper * 100.0, 1),
            "method": (
                f"{BOOTSTRAP_REPLICATIONS}-replicate deterministic circular moving-block bootstrap "
                "of strictly resolved as-of regime and reliability-calibration evidence; "
                "90% percentile interval"
            ),
            "effective_sample_size": interval.effective_sample_size,
            "block_length_sessions": interval.block_length_sessions,
            "calibration_block_length_forecasts": interval.calibration_block_length_forecasts,
            "calibration_evidence_count": interval.calibration_evidence_count,
            "limitations": (
                "Sampling uncertainty conditional on the fixed feature and regime definitions; "
                "it does not quantify structural breaks, source error or tail-model risk."
            ),
        },
        "event": event,
        "sample": {
            "training_examples": len(current_training),
            "conditional_examples": conditional_count,
            "calibration_examples": calibration_count,
            "regime": regime,
        },
        "calibration": metrics,
        "calibration_status": calibration_status,
        "signed_drivers": drivers,
    }
    if contract == "exact_terminal":
        result["challenger"] = {
            **benchmark,
            "delta_model_minus_challenger_percentage_points": benchmark[
                "delta_model_minus_benchmark_percentage_points"
            ],
        }
    else:
        result["benchmark"] = benchmark
        adjustment = calibrated - unconstrained_calibrated
        result["unconstrained_probability"] = round(unconstrained_calibrated * 100.0, 1)
        result["coherence_constraint"] = {
            "rule": "touch probability is floored by the matched exact-terminal probability",
            "terminal_floor_probability": round(float(coherence_floor_probability) * 100.0, 1),
            "adjustment_percentage_points": round(adjustment * 100.0, 1),
            "binding": adjustment > 0.0,
            "backtest_adjusted_forecast_count": coherence_adjustments,
            "backtest_max_adjustment_percentage_points": round(
                coherence_max_adjustment * 100.0,
                4,
            ),
            "metrics_probability_series": "coherence_constrained_any_time_breach",
        }
    return result, backtest, calibrated


def _calibration_status(metrics: dict) -> str:
    forecast_count = metrics.get("forecast_count")
    brier_skill = metrics.get("brier_skill_vs_climatology")
    calibration_error = metrics.get("calibration_error")
    passes = (
        isinstance(forecast_count, (int, float))
        and math.isfinite(float(forecast_count))
        and forecast_count >= 50
        and isinstance(brier_skill, (int, float))
        and math.isfinite(float(brier_skill))
        and brier_skill > 0.0
        and isinstance(calibration_error, (int, float))
        and math.isfinite(float(calibration_error))
        and calibration_error <= 0.10
    )
    return (
        "calibrated"
        if passes
        else "experimental"
    )


def _feature_rows(points: Sequence[object]) -> list[FeatureRow]:
    rows = []
    values = [float(point.value) for point in points]
    for index in range(FEATURE_WINDOW, len(points)):
        r5 = _percent_return(values[index - 5], values[index])
        r20 = _percent_return(values[index - 20], values[index])
        daily_log_returns = [
            math.log(values[position] / values[position - 1])
            for position in range(index - 19, index + 1)
            if values[position - 1] > 0 and values[position] > 0
        ]
        volatility = _sample_std(daily_log_returns) * math.sqrt(252.0) * 100.0
        rows.append(
            FeatureRow(
                index=index,
                observed_at=points[index].observed_at,
                momentum_5d=r5,
                momentum_20d=r20,
                realized_volatility_20d=volatility,
                acceleration=r5 - r20 * 0.25,
            )
        )
    return rows


def _labelled_rows(
    points: Sequence[object],
    features: Sequence[FeatureRow],
    sessions: int,
    threshold: float,
) -> list[LabelledRow]:
    result = []
    for row in features:
        target_index = row.index + sessions
        if target_index >= len(points):
            continue
        move = _percent_return(float(points[row.index].value), float(points[target_index].value))
        result.append(LabelledRow(row, target_index, int(move >= threshold)))
    return result


def _touch_labelled_rows(
    points: Sequence[object],
    features: Sequence[FeatureRow],
    sessions: int,
    threshold: float,
) -> list[LabelledRow]:
    """Label an inclusive t+1..t+h daily-reference-rate threshold breach.

    ``target_index`` is always the end of the full observation window, including
    when a breach occurs earlier. This intentionally delays admission of every
    positive and negative label until the same complete window has resolved.
    """

    result = []
    for row in features:
        target_index = row.index + sessions
        if target_index >= len(points):
            continue
        baseline = float(points[row.index].value)
        threshold_value = baseline * (1.0 + threshold / 100.0)
        touched = any(
            float(points[position].value) >= threshold_value
            for position in range(row.index + 1, target_index + 1)
        )
        result.append(LabelledRow(row, target_index, int(touched)))
    return result


def _walk_forward_backtest(
    labelled: Sequence[LabelledRow],
    by_index: dict[int, FeatureRow],
    sessions: int,
) -> list[Prediction]:
    if not labelled:
        return []
    first_index = max(labelled[0].features.index + MINIMUM_TRAINING + sessions, FEATURE_WINDOW + 500)
    last_index = labelled[-1].features.index
    evaluation_step = max(5, min(22, sessions))
    predictions: list[Prediction] = []
    labelled_by_index = {row.features.index: row for row in labelled}
    for forecast_index in range(first_index, last_index + 1, evaluation_step):
        current = by_index.get(forecast_index)
        actual = labelled_by_index.get(forecast_index)
        if current is None or actual is None:
            continue
        training = [row for row in labelled if row.target_index < forecast_index]
        if len(training) < MINIMUM_TRAINING:
            continue
        raw, conditional_count, _regime = _conditional_probability(current, training)
        calibrated, calibration_count = _calibrate(raw, predictions, as_of_index=forecast_index)
        climatology = _climatology_probability(training)
        predictions.append(
            Prediction(
                forecast_index=forecast_index,
                target_index=actual.target_index,
                raw_probability=raw,
                probability=calibrated,
                climatology_probability=climatology,
                outcome=actual.outcome,
                conditional_count=conditional_count,
                calibration_count=calibration_count,
            )
        )
    return predictions


def _coherent_touch_predictions(
    touch_predictions: Sequence[Prediction],
    terminal_predictions: Sequence[Prediction],
) -> tuple[list[Prediction], int, float]:
    """Project matched touch forecasts onto their terminal-event lower bound.

    Outcomes and raw estimates remain those of the touch contract. Only the
    published/evaluated touch probability is projected, and the original value
    is retained on each prediction for reproducibility.
    """

    terminal_by_index = {
        prediction.forecast_index: prediction for prediction in terminal_predictions
    }
    touch_indexes = {prediction.forecast_index for prediction in touch_predictions}
    if touch_indexes != set(terminal_by_index):
        raise ValueError("touch and terminal backtests must have identical forecast indexes")

    coherent: list[Prediction] = []
    adjusted_count = 0
    maximum_adjustment = 0.0
    for touch in touch_predictions:
        terminal = terminal_by_index[touch.forecast_index]
        if touch.target_index != terminal.target_index:
            raise ValueError("matched touch and terminal forecasts must share a target-window end")
        if touch.outcome < terminal.outcome:
            raise ValueError("touch outcome cannot be lower than its exact-terminal subset outcome")
        unconstrained = (
            touch.unconstrained_probability
            if touch.unconstrained_probability is not None
            else touch.probability
        )
        constrained = max(unconstrained, terminal.probability)
        adjustment = constrained - unconstrained
        if adjustment > 0.0:
            adjusted_count += 1
            maximum_adjustment = max(maximum_adjustment, adjustment)
        coherent.append(
            Prediction(
                forecast_index=touch.forecast_index,
                target_index=touch.target_index,
                raw_probability=touch.raw_probability,
                probability=constrained,
                climatology_probability=touch.climatology_probability,
                outcome=touch.outcome,
                conditional_count=touch.conditional_count,
                calibration_count=touch.calibration_count,
                unconstrained_probability=unconstrained,
            )
        )
    return coherent, adjusted_count, maximum_adjustment


def _conditional_probability(
    current: FeatureRow,
    training: Sequence[LabelledRow],
) -> tuple[float, int, str]:
    base = _climatology_probability(training)
    conditional, regime_detail = _conditional_rows(current, training)
    probability = (
        sum(row.outcome for row in conditional) + PRIOR_STRENGTH * base
    ) / (len(conditional) + PRIOR_STRENGTH)
    return _clip_probability(probability), len(conditional), regime_detail


def _climatology_probability(training: Sequence[LabelledRow]) -> float:
    """Laplace-smoothed event rate from labels resolved at forecast formation."""

    return (sum(row.outcome for row in training) + 1.0) / (len(training) + 2.0)


def _conditional_rows(
    current: FeatureRow,
    training: Sequence[LabelledRow],
) -> tuple[list[LabelledRow], str]:
    momentum_cuts = _tertiles([row.features.momentum_20d for row in training])
    volatility_cuts = _tertiles([row.features.realized_volatility_20d for row in training])
    current_momentum = _bin(current.momentum_20d, momentum_cuts)
    current_volatility = _bin(current.realized_volatility_20d, volatility_cuts)
    current_acceleration = current.acceleration >= 0

    conditional = [
        row
        for row in training
        if _bin(row.features.momentum_20d, momentum_cuts) == current_momentum
        and _bin(row.features.realized_volatility_20d, volatility_cuts) == current_volatility
        and (row.features.acceleration >= 0) == current_acceleration
    ]
    regime_detail = "momentum tertile + volatility tertile + acceleration sign"
    if len(conditional) < 30:
        conditional = [
            row
            for row in training
            if _bin(row.features.momentum_20d, momentum_cuts) == current_momentum
            and _bin(row.features.realized_volatility_20d, volatility_cuts) == current_volatility
        ]
        regime_detail = "momentum tertile + volatility tertile (acceleration relaxed)"
    if len(conditional) < 30:
        conditional = [
            row
            for row in training
            if _bin(row.features.momentum_20d, momentum_cuts) == current_momentum
        ]
        regime_detail = "momentum tertile (volatility and acceleration relaxed)"
    return conditional, regime_detail


def _calibrate(
    raw_probability: float,
    earlier_predictions: Sequence[Prediction],
    *,
    as_of_index: int,
) -> tuple[float, int]:
    resolved = _calibration_evidence(raw_probability, earlier_predictions, as_of_index=as_of_index)
    if len(resolved) < 20:
        return raw_probability, len(resolved)
    calibrated = (
        sum(prediction.outcome for prediction in resolved)
        + CALIBRATION_PRIOR_STRENGTH * raw_probability
    ) / (len(resolved) + CALIBRATION_PRIOR_STRENGTH)
    return _clip_probability(calibrated), len(resolved)


def _calibration_evidence(
    raw_probability: float,
    earlier_predictions: Sequence[Prediction],
    *,
    as_of_index: int,
) -> list[Prediction]:
    return [
        prediction
        for prediction in earlier_predictions
        if prediction.target_index < as_of_index
        and abs(prediction.raw_probability - raw_probability) <= 0.10
    ]


def _metrics(predictions: Sequence[Prediction]) -> dict:
    if not predictions:
        return {
            "status": "insufficient_backtest_sample",
            "forecast_count": 0,
            "event_count": 0,
            "brier_score": None,
            "log_loss": None,
            "climatology_brier_score": None,
            "brier_skill_vs_climatology": None,
            "calibration_error": None,
        }
    brier = mean((prediction.probability - prediction.outcome) ** 2 for prediction in predictions)
    log_loss = mean(
        -(
            prediction.outcome * math.log(_clip_probability(prediction.probability))
            + (1 - prediction.outcome) * math.log(_clip_probability(1.0 - prediction.probability))
        )
        for prediction in predictions
    )
    climatology_errors = [
        (prediction.climatology_probability - prediction.outcome) ** 2
        for prediction in predictions
    ]
    climatology_brier = mean(climatology_errors)
    skill = 1.0 - brier / climatology_brier if climatology_brier > 0 else None
    return {
        "status": "available",
        "forecast_count": len(predictions),
        "event_count": sum(prediction.outcome for prediction in predictions),
        "event_rate": round(mean(prediction.outcome for prediction in predictions), 4),
        "brier_score": round(brier, 4),
        "log_loss": round(log_loss, 4),
        "climatology_brier_score": round(climatology_brier, 4),
        "climatology_protocol": (
            "Laplace-smoothed event rate from labels with target_index before forecast_index; "
            "the benchmark probability is stored at forecast formation"
        ),
        "brier_skill_vs_climatology": round(skill, 4) if skill is not None else None,
        "calibration_error": round(_expected_calibration_error(predictions), 4),
        "first_forecast_date_index": predictions[0].forecast_index,
        "last_forecast_date_index": predictions[-1].forecast_index,
        "evaluation_cadence_sessions": max(5, min(22, predictions[0].target_index - predictions[0].forecast_index)),
    }


def _expected_calibration_error(predictions: Sequence[Prediction]) -> float:
    total = len(predictions)
    error = 0.0
    for lower in (0.0, 0.2, 0.4, 0.6, 0.8):
        upper = lower + 0.2
        group = [
            prediction
            for prediction in predictions
            if lower <= prediction.probability < upper or (upper == 1.0 and prediction.probability == 1.0)
        ]
        if not group:
            continue
        error += (len(group) / total) * abs(
            mean(prediction.probability for prediction in group)
            - mean(prediction.outcome for prediction in group)
        )
    return error


def _uncertainty_interval(
    probability: float,
    *,
    raw_probability: float,
    current: FeatureRow,
    training: Sequence[LabelledRow],
    earlier_predictions: Sequence[Prediction],
    sessions: int,
    as_of_index: int,
) -> UncertaintyInterval:
    """Return a dependence-aware percentile interval for the fitted probability.

    Historical labels overlap for multi-session horizons, so treating every row
    as an independent Bernoulli draw materially understates uncertainty.  The
    bootstrap resamples circular blocks of horizon length from the strictly
    resolved training rows.  It then applies the same climatology shrinkage and,
    when available, the same local reliability-calibration formula as the point
    estimator.  Regime membership and the calibration neighbourhood are held
    fixed; the published limitation makes that conditional scope explicit.
    """

    conditional, _regime = _conditional_rows(current, training)
    conditional_indexes = {row.features.index for row in conditional}
    calibration_evidence = _calibration_evidence(
        raw_probability,
        earlier_predictions,
        as_of_index=as_of_index,
    )
    evaluation_step = max(5, min(22, sessions))
    calibration_block_length = max(1, math.ceil(sessions / evaluation_step))
    rng = random.Random(
        0xF17A5EED
        + sessions * 1009
        + len(training) * 9176
        + len(calibration_evidence) * 37
    )
    replicates: list[float] = []
    for _ in range(BOOTSTRAP_REPLICATIONS):
        total_events, conditional_events, conditional_count = _moving_block_training_counts(
            training,
            conditional_indexes,
            block_length=sessions,
            rng=rng,
        )
        base = (total_events + 1.0) / (len(training) + 2.0)
        bootstrapped_raw = (
            conditional_events + PRIOR_STRENGTH * base
        ) / (conditional_count + PRIOR_STRENGTH)
        if len(calibration_evidence) >= 20:
            calibration_events = _moving_block_event_sum(
                calibration_evidence,
                block_length=calibration_block_length,
                rng=rng,
            )
            estimate = (
                calibration_events + CALIBRATION_PRIOR_STRENGTH * bootstrapped_raw
            ) / (len(calibration_evidence) + CALIBRATION_PRIOR_STRENGTH)
        else:
            estimate = bootstrapped_raw
        replicates.append(_clip_probability(estimate))

    lower = min(probability, _percentile(replicates, 0.05))
    upper = max(probability, _percentile(replicates, 0.95))
    conditional_effective = _non_overlapping_label_count(conditional, sessions)
    if len(calibration_evidence) >= 20:
        calibration_effective = _non_overlapping_prediction_count(calibration_evidence)
        effective = min(conditional_effective, calibration_effective)
    else:
        effective = conditional_effective
    return UncertaintyInterval(
        lower=max(0.0, lower),
        upper=min(1.0, upper),
        effective_sample_size=max(1, effective),
        block_length_sessions=sessions,
        calibration_block_length_forecasts=calibration_block_length,
        calibration_evidence_count=len(calibration_evidence),
    )


def _moving_block_training_counts(
    training: Sequence[LabelledRow],
    conditional_indexes: set[int],
    *,
    block_length: int,
    rng: random.Random,
) -> tuple[int, int, int]:
    total_events = 0
    conditional_events = 0
    conditional_count = 0
    sampled = 0
    while sampled < len(training):
        start = rng.randrange(len(training))
        take = min(block_length, len(training) - sampled)
        for offset in range(take):
            row = training[(start + offset) % len(training)]
            total_events += row.outcome
            if row.features.index in conditional_indexes:
                conditional_events += row.outcome
                conditional_count += 1
        sampled += take
    return total_events, conditional_events, conditional_count


def _moving_block_event_sum(
    predictions: Sequence[Prediction],
    *,
    block_length: int,
    rng: random.Random,
) -> int:
    events = 0
    sampled = 0
    while sampled < len(predictions):
        start = rng.randrange(len(predictions))
        take = min(block_length, len(predictions) - sampled)
        events += sum(
            predictions[(start + offset) % len(predictions)].outcome
            for offset in range(take)
        )
        sampled += take
    return events


def _non_overlapping_label_count(rows: Sequence[LabelledRow], sessions: int) -> int:
    count = 0
    next_index = -1
    for row in rows:
        if row.features.index >= next_index:
            count += 1
            next_index = row.features.index + sessions
    return count


def _non_overlapping_prediction_count(predictions: Sequence[Prediction]) -> int:
    count = 0
    next_index = -1
    for prediction in predictions:
        if prediction.forecast_index >= next_index:
            count += 1
            next_index = prediction.target_index
    return count


def _percentile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1.0 - weight) + ordered[upper] * weight


def _signed_drivers(current: FeatureRow, training: Sequence[LabelledRow]) -> list[dict]:
    specifications = [
        ("momentum_5d", "5-session USD/TRY momentum", "%", current.momentum_5d),
        ("momentum_20d", "20-session USD/TRY momentum", "%", current.momentum_20d),
        ("realized_volatility_20d", "20-session realized volatility", "% annualized", current.realized_volatility_20d),
        ("acceleration", "Short-trend acceleration", "percentage points", current.acceleration),
    ]
    base = mean(row.outcome for row in training)
    drivers = []
    for attribute, label, unit, value in specifications:
        values = [float(getattr(row.features, attribute)) for row in training]
        split = median(values)
        selected = [
            row.outcome
            for row in training
            if (float(getattr(row.features, attribute)) >= split) == (value >= split)
        ]
        selected_rate = (sum(selected) + PRIOR_STRENGTH * base) / (len(selected) + PRIOR_STRENGTH)
        effect = (selected_rate - base) * 100.0
        drivers.append(
            {
                "id": attribute,
                "label": label,
                "value": round(value, 3),
                "unit": unit,
                "historical_median": round(split, 3),
                "direction": "pressure" if effect > 0.25 else "relief" if effect < -0.25 else "neutral",
                "estimated_effect_percentage_points": round(effect, 1),
                "sample_count": len(selected),
                "source": "ECB-derived USD/TRY history",
                "interpretation": "marginal historical association; effects are not additive or causal",
            }
        )
    return sorted(drivers, key=lambda item: abs(item["estimated_effect_percentage_points"]), reverse=True)


def _tertiles(values: Sequence[float]) -> tuple[float, float]:
    ordered = sorted(values)
    return ordered[len(ordered) // 3], ordered[(2 * len(ordered)) // 3]


def _bin(value: float, cuts: tuple[float, float]) -> int:
    if value < cuts[0]:
        return 0
    if value < cuts[1]:
        return 1
    return 2


def _percent_return(start: float, end: float) -> float:
    return ((end / start) - 1.0) * 100.0


def _sample_std(values: Sequence[float]) -> float:
    if len(values) < 2:
        return 0.0
    average = mean(values)
    return math.sqrt(sum((value - average) ** 2 for value in values) / (len(values) - 1))


def _clip_probability(value: float) -> float:
    return max(0.005, min(0.995, value))
