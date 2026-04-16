from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.entities import (
    AssessmentCycle,
    PriceObservation,
    PriceSeries,
    RealizedOutcome,
)
from app.services.horizons import HORIZON_THRESHOLDS, HORIZONS, horizon_due_date


@dataclass(frozen=True)
class MarketPoint:
    observed_at: datetime
    close_value: float
    source_name: str


def sync_realized_outcomes(
    session: Session,
    *,
    cycle_id: int | None = None,
) -> dict:
    cycles_query = select(AssessmentCycle).order_by(
        AssessmentCycle.assessment_timestamp.asc(),
        AssessmentCycle.id.asc(),
    )
    if cycle_id is not None:
        cycles_query = cycles_query.where(AssessmentCycle.id == cycle_id)

    cycles = list(session.scalars(cycles_query).all())
    if not cycles:
        return {"cycle_count": 0, "resolved_count": 0, "updated_rows": 0}

    history = _usdtry_history(session)
    updated_rows = 0
    resolved_count = 0
    for cycle in cycles:
        session.execute(delete(RealizedOutcome).where(RealizedOutcome.cycle_id == cycle.id))
        rows = _build_cycle_outcomes(cycle, history)
        session.add_all(rows)
        updated_rows += len(rows)
        resolved_count += sum(1 for row in rows if row.event_occurred is not None)

    session.flush()
    return {
        "cycle_count": len(cycles),
        "resolved_count": resolved_count,
        "updated_rows": updated_rows,
    }


def _build_cycle_outcomes(
    cycle: AssessmentCycle,
    history: list[MarketPoint],
) -> list[RealizedOutcome]:
    if not history:
        return [
            RealizedOutcome(
                cycle_id=cycle.id,
                horizon=horizon,
                threshold_pct=HORIZON_THRESHOLDS[horizon],
                realized_move_pct=None,
                outcome_known_on=None,
                event_occurred=None,
            )
            for horizon in HORIZONS
        ]

    baseline = _latest_known_point(history, cycle.assessment_timestamp)
    rows: list[RealizedOutcome] = []
    for horizon in HORIZONS:
        threshold_pct = HORIZON_THRESHOLDS[horizon]
        due_date = horizon_due_date(cycle.assessment_timestamp, horizon)
        outcome_point = _first_point_on_or_after(history, due_date)

        if baseline is None or outcome_point is None:
            realized_move_pct = None
            outcome_known_on = None
            event_occurred = None
        else:
            realized_move_pct = round(
                ((outcome_point.close_value - baseline.close_value) / baseline.close_value) * 100,
                3,
            )
            outcome_known_on = outcome_point.observed_at
            event_occurred = realized_move_pct >= threshold_pct

        rows.append(
            RealizedOutcome(
                cycle_id=cycle.id,
                horizon=horizon,
                threshold_pct=threshold_pct,
                realized_move_pct=realized_move_pct,
                outcome_known_on=outcome_known_on,
                event_occurred=event_occurred,
            )
        )
    return rows


def _usdtry_history(session: Session) -> list[MarketPoint]:
    points_by_date: dict[datetime, MarketPoint] = {}

    direct_series = list(
        session.scalars(
            select(PriceSeries)
            .where(
                PriceSeries.base_currency == "USD",
                PriceSeries.quote_currency == "TRY",
            )
            .order_by(PriceSeries.id.asc())
        ).all()
    )
    for series in direct_series:
        observations = list(
            session.scalars(
                select(PriceObservation)
                .where(PriceObservation.series_id == series.id)
                .order_by(PriceObservation.observed_at.asc(), PriceObservation.id.asc())
            ).all()
        )
        for observation in observations:
            if observation.close_value is None:
                continue
            points_by_date.setdefault(
                observation.observed_at,
                MarketPoint(
                    observed_at=observation.observed_at,
                    close_value=observation.close_value,
                    source_name=series.name,
                ),
            )

    derived_series = _derived_usdtry_history(session)
    for point in derived_series:
        points_by_date.setdefault(point.observed_at, point)

    return sorted(points_by_date.values(), key=lambda item: item.observed_at)


def _derived_usdtry_history(session: Session) -> list[MarketPoint]:
    eur_try_series = session.scalar(
        select(PriceSeries)
        .where(PriceSeries.symbol == "D.TRY.EUR.SP00.A")
        .limit(1)
    )
    eur_usd_series = session.scalar(
        select(PriceSeries)
        .where(PriceSeries.symbol == "D.USD.EUR.SP00.A")
        .limit(1)
    )
    if eur_try_series is None or eur_usd_series is None:
        return []

    eur_try_points = {
        observation.observed_at: observation.close_value
        for observation in session.scalars(
            select(PriceObservation)
            .where(PriceObservation.series_id == eur_try_series.id)
            .order_by(PriceObservation.observed_at.asc(), PriceObservation.id.asc())
        ).all()
        if observation.close_value not in (None, 0)
    }
    eur_usd_points = {
        observation.observed_at: observation.close_value
        for observation in session.scalars(
            select(PriceObservation)
            .where(PriceObservation.series_id == eur_usd_series.id)
            .order_by(PriceObservation.observed_at.asc(), PriceObservation.id.asc())
        ).all()
        if observation.close_value not in (None, 0)
    }

    shared_dates = sorted(set(eur_try_points) & set(eur_usd_points))
    return [
        MarketPoint(
            observed_at=observed_at,
            close_value=round(eur_try_points[observed_at] / eur_usd_points[observed_at], 6),
            source_name="USD/TRY Derived From ECB EUR Crosses",
        )
        for observed_at in shared_dates
    ]


def _latest_known_point(history: list[MarketPoint], target: datetime) -> MarketPoint | None:
    observed_at = [point.observed_at for point in history]
    index = bisect_right(observed_at, target)
    if index == 0:
        return None
    return history[index - 1]


def _first_point_on_or_after(history: list[MarketPoint], target: datetime) -> MarketPoint | None:
    observed_at = [point.observed_at for point in history]
    index = bisect_left(observed_at, target)
    if index >= len(history):
        return None
    return history[index]
