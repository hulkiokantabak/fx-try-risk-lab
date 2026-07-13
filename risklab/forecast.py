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


MODEL_VERSION = "empirical-regime-v2.1.0"
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
    all_metrics: dict[str, dict] = {}
    all_drivers: dict[str, list[dict]] = {}

    for horizon, sessions in HORIZON_SESSIONS.items():
        threshold = float(thresholds[horizon])
        labelled = _labelled_rows(points, features, sessions, threshold)
        current_training = [row for row in labelled if row.target_index <= current.index]
        if len(current_training) < MINIMUM_TRAINING:
            raise ValueError(
                f"{horizon} forecast has only {len(current_training)} leakage-safe training examples"
            )

        backtest = _walk_forward_backtest(labelled, by_index, sessions)
        raw, conditional_count, regime = _conditional_probability(current, current_training)
        calibrated, calibration_count = _calibrate(raw, backtest, as_of_index=current.index + 1)
        metrics = _metrics(backtest)
        calibration_status = (
            "calibrated"
            if metrics.get("forecast_count", 0) >= 50
            and (metrics.get("brier_skill_vs_climatology") or 0.0) > 0.0
            and (metrics.get("calibration_error") or 1.0) <= 0.10
            else "experimental"
        )
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
        target_move = ((points[-1].value * (1.0 + threshold / 100.0)))

        horizons[horizon] = {
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
            "event": {
                "baseline_value": round(float(points[-1].value), 6),
                "threshold_value": round(target_move, 6),
                "operator": ">=",
                "formula": f"USDTRY[t+{sessions}] / USDTRY[t] - 1 >= {threshold / 100.0:.6f}",
            },
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
        all_metrics[horizon] = metrics
        all_drivers[horizon] = drivers

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
            "status": "experimental",
            "is_calibrated": is_calibrated,
            "output_type": "calibrated_probability" if is_calibrated else "experimental_probability",
            "probability_scale": "0-100 percent",
            "method": (
                "Expanding-window historical event rate conditioned on as-of 5-session momentum, "
                "20-session momentum, realized volatility and acceleration; Bayesian shrinkage to "
                "the as-of climatology; rolling reliability recalibration."
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
            "pair": "USD/TRY",
            "direction": "TRY depreciation / USD/TRY increase",
            "measurement": "ECB reference-rate close to close over an exact count of trading observations",
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
        "signed_drivers": all_drivers,
    }


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
