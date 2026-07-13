"""Semantic validation and provenance helpers for public data sources."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Callable, Iterable


class DataQualityError(ValueError):
    """Raised when a response is syntactically readable but not usable data."""


def validate_series(
    points: Iterable[object],
    *,
    minimum_count: int,
    positive: bool = False,
    plausible_range: tuple[float, float] | None = None,
) -> None:
    points = list(points)
    if len(points) < minimum_count:
        raise DataQualityError(f"expected at least {minimum_count} observations; received {len(points)}")
    dates = []
    for point in points:
        observed_at = getattr(point, "observed_at", None)
        value = getattr(point, "value", None)
        if not isinstance(observed_at, datetime):
            raise DataQualityError("observation date is missing or invalid")
        if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise DataQualityError("observation value is missing or non-finite")
        if positive and value <= 0:
            raise DataQualityError("series contains a non-positive observation")
        if plausible_range is not None and not plausible_range[0] <= value <= plausible_range[1]:
            raise DataQualityError(
                f"observation {value} is outside plausible range {plausible_range[0]}..{plausible_range[1]}"
            )
        dates.append(observed_at)
    if dates != sorted(dates) or len(dates) != len(set(dates)):
        raise DataQualityError("observation dates must be unique and increasing")


def validate_series_map(
    series_map: object,
    *,
    required_keys: Iterable[str],
    minimum_count: int,
) -> None:
    if not isinstance(series_map, dict):
        raise DataQualityError("expected a map of named series")
    for key in required_keys:
        if key not in series_map:
            raise DataQualityError(f"required series {key!r} is missing")
        validate_series(series_map[key], minimum_count=minimum_count, positive=True)


def validate_feed(entries: Iterable[object], *, minimum_count: int = 1) -> None:
    entries = list(entries)
    if len(entries) < minimum_count:
        raise DataQualityError(f"expected at least {minimum_count} feed entries; received {len(entries)}")
    for entry in entries:
        if not str(getattr(entry, "title", "")).strip():
            raise DataQualityError("feed entry has no title")
        if not isinstance(getattr(entry, "published_at", None), datetime):
            raise DataQualityError("feed entry has no valid publication date")


def latest_observation(value: object) -> datetime | None:
    if isinstance(value, list):
        dates = [getattr(item, "observed_at", None) for item in value]
        dates.extend(getattr(item, "published_at", None) for item in value)
        valid = [item for item in dates if isinstance(item, datetime)]
        return max(valid) if valid else None
    if isinstance(value, dict):
        valid = [latest_observation(item) for item in value.values()]
        return max((item for item in valid if item is not None), default=None)
    return None


def item_count(value: object) -> int:
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        return sum(item_count(item) for item in value.values())
    return 0


def checksum(payload: object) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def iso_utc(value: datetime | None = None) -> str:
    value = value or datetime.now(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def age_days(observed_at: datetime | None, *, now: datetime | None = None) -> float | None:
    if observed_at is None:
        return None
    now = now or datetime.now(UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    return max(0.0, (now - observed_at.astimezone(UTC)).total_seconds() / 86_400.0)


def ensure_fresh(value: object, *, maximum_age_days: int, allow_no_observation: bool = False) -> None:
    latest = latest_observation(value)
    if latest is None:
        if allow_no_observation:
            return
        raise DataQualityError("source has no dated observation")
    age = age_days(latest)
    if age is not None and age > maximum_age_days:
        raise DataQualityError(
            f"latest observation is {age:.1f} days old; freshness gate is {maximum_age_days} days"
        )


Validator = Callable[[object], None]
