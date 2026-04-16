from __future__ import annotations

from datetime import datetime, timedelta

HORIZONS = ("1w", "1m", "3m", "6m", "1y")

HORIZON_THRESHOLDS = {
    "1w": 2.0,
    "1m": 5.0,
    "3m": 10.0,
    "6m": 15.0,
    "1y": 25.0,
}

HORIZON_DAY_WINDOWS = {
    "1w": 7,
    "1m": 30,
    "3m": 90,
    "6m": 180,
    "1y": 365,
}


def horizon_due_date(observed_at: datetime, horizon: str) -> datetime:
    return observed_at + timedelta(days=HORIZON_DAY_WINDOWS[horizon])


def horizon_sort_key(horizon: str) -> int:
    try:
        return HORIZONS.index(horizon)
    except ValueError:
        return len(HORIZONS)
