from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utc_now_naive,
        onupdate=_utc_now_naive,
        nullable=False,
    )


class Source(TimestampMixin, Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    trust_tier: Mapped[int] = mapped_column(Integer, nullable=False)
    collection_method: Mapped[str] = mapped_column(String(50), nullable=False)
    endpoint: Mapped[str | None] = mapped_column(String(500))
    requires_credentials: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    freshness_expectation: Mapped[str | None] = mapped_column(String(100))
    parser_adapter: Mapped[str | None] = mapped_column(String(120))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    fetch_runs: Mapped[list[SourceFetchRun]] = relationship(back_populates="source")


class SourceFetchRun(TimestampMixin, Base):
    __tablename__ = "source_fetch_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    items_ingested: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)

    source: Mapped[Source] = relationship(back_populates="fetch_runs")


class PriceSeries(TimestampMixin, Base):
    __tablename__ = "price_series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    quote_currency: Mapped[str | None] = mapped_column(String(10))
    base_currency: Mapped[str | None] = mapped_column(String(10))
    frequency: Mapped[str] = mapped_column(String(20), default="daily", nullable=False)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))

    observations: Mapped[list[PriceObservation]] = relationship(back_populates="series")


class PriceObservation(Base):
    __tablename__ = "price_observations"
    __table_args__ = (UniqueConstraint("series_id", "observed_at", name="uq_price_obs"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("price_series.id"), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    open_value: Mapped[float | None] = mapped_column(Float)
    high_value: Mapped[float | None] = mapped_column(Float)
    low_value: Mapped[float | None] = mapped_column(Float)
    close_value: Mapped[float | None] = mapped_column(Float)
    volume: Mapped[float | None] = mapped_column(Float)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive, nullable=False)

    series: Mapped[PriceSeries] = relationship(back_populates="observations")


class MacroSeries(TimestampMixin, Base):
    __tablename__ = "macro_series"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(220), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    unit: Mapped[str | None] = mapped_column(String(50))
    frequency: Mapped[str] = mapped_column(String(20), default="monthly", nullable=False)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))

    observations: Mapped[list[MacroObservation]] = relationship(back_populates="series")


class MacroObservation(Base):
    __tablename__ = "macro_observations"
    __table_args__ = (UniqueConstraint("series_id", "observation_date", name="uq_macro_obs"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    series_id: Mapped[int] = mapped_column(ForeignKey("macro_series.id"), nullable=False)
    observation_date: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    release_date: Mapped[datetime | None] = mapped_column(DateTime)
    value: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive, nullable=False)

    series: Mapped[MacroSeries] = relationship(back_populates="observations")


class PolicyEvent(TimestampMixin, Base):
    __tablename__ = "policy_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))
    occurred_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    title: Mapped[str] = mapped_column(String(220), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[dict | None] = mapped_column(JSON)


class Headline(TimestampMixin, Base):
    __tablename__ = "headlines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))
    published_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    url: Mapped[str | None] = mapped_column(String(1000))
    summary: Mapped[str | None] = mapped_column(Text)
    sentiment_hint: Mapped[str | None] = mapped_column(String(50))
    tags: Mapped[list | None] = mapped_column(JSON)


class ChatterItem(TimestampMixin, Base):
    __tablename__ = "chatter_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(ForeignKey("sources.id"))
    posted_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    author: Mapped[str | None] = mapped_column(String(120))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str | None] = mapped_column(String(1000))
    trust_score: Mapped[float | None] = mapped_column(Float)
    tags: Mapped[list | None] = mapped_column(JSON)


class AssessmentCycle(TimestampMixin, Base):
    __tablename__ = "assessment_cycles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(160), nullable=False)
    primary_horizon: Mapped[str] = mapped_column(String(10), nullable=False, default="1m")
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="draft")
    parent_cycle_id: Mapped[int | None] = mapped_column(ForeignKey("assessment_cycles.id"))
    assessment_timestamp: Mapped[datetime] = mapped_column(DateTime, default=_utc_now_naive)
    evidence_pack_path: Mapped[str | None] = mapped_column(String(500))
    user_prompt: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)

    parent_cycle: Mapped[AssessmentCycle | None] = relationship(
        back_populates="follow_up_cycles",
        remote_side="AssessmentCycle.id",
    )
    follow_up_cycles: Mapped[list[AssessmentCycle]] = relationship(
        back_populates="parent_cycle"
    )
    activations: Mapped[list[CycleSpecialistActivation]] = relationship(back_populates="cycle")
    round_outputs: Mapped[list[AgentRoundOutput]] = relationship(back_populates="cycle")
    house_views: Mapped[list[HouseView]] = relationship(back_populates="cycle")
    reports: Mapped[list[Report]] = relationship(back_populates="cycle")
    realized_outcomes: Mapped[list[RealizedOutcome]] = relationship(back_populates="cycle")


class CycleSpecialistActivation(Base):
    __tablename__ = "cycle_specialist_activations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("assessment_cycles.id"), nullable=False)
    specialist_name: Mapped[str] = mapped_column(String(80), nullable=False)
    trigger_topic: Mapped[str] = mapped_column(String(120), nullable=False)
    materiality_reason: Mapped[str | None] = mapped_column(Text)
    activated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utc_now_naive,
        nullable=False,
    )

    cycle: Mapped[AssessmentCycle] = relationship(back_populates="activations")


class AgentRoundOutput(TimestampMixin, Base):
    __tablename__ = "agent_round_outputs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("assessment_cycles.id"), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(80), nullable=False)
    agent_role: Mapped[str] = mapped_column(String(120), nullable=False)
    round_name: Mapped[str] = mapped_column(String(20), nullable=False)
    stance: Mapped[str | None] = mapped_column(String(30))
    primary_horizon: Mapped[str | None] = mapped_column(String(10))
    primary_risk_score: Mapped[float | None] = mapped_column(Float)
    confidence: Mapped[str | None] = mapped_column(String(20))
    risk_curve: Mapped[dict | None] = mapped_column(JSON)
    top_drivers: Mapped[list | None] = mapped_column(JSON)
    counterevidence: Mapped[str | None] = mapped_column(Text)
    watch_triggers: Mapped[list | None] = mapped_column(JSON)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    cycle: Mapped[AssessmentCycle] = relationship(back_populates="round_outputs")


class HouseView(TimestampMixin, Base):
    __tablename__ = "house_views"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("assessment_cycles.id"), nullable=False)
    primary_horizon: Mapped[str] = mapped_column(String(10), nullable=False)
    house_primary_score: Mapped[float] = mapped_column(Float, nullable=False)
    house_confidence: Mapped[str] = mapped_column(String(20), nullable=False)
    disagreement_range: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    stress_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    minority_risk_note: Mapped[str | None] = mapped_column(Text)
    risk_curve: Mapped[dict] = mapped_column(JSON, nullable=False)

    cycle: Mapped[AssessmentCycle] = relationship(back_populates="house_views")


class Report(TimestampMixin, Base):
    __tablename__ = "reports"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("assessment_cycles.id"), nullable=False)
    report_type: Mapped[str] = mapped_column(String(30), nullable=False, default="html")
    title: Mapped[str] = mapped_column(String(220), nullable=False)
    file_path: Mapped[str | None] = mapped_column(String(500))
    generated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=_utc_now_naive,
        nullable=False,
    )

    cycle: Mapped[AssessmentCycle] = relationship(back_populates="reports")


class RealizedOutcome(TimestampMixin, Base):
    __tablename__ = "realized_outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cycle_id: Mapped[int] = mapped_column(ForeignKey("assessment_cycles.id"), nullable=False)
    horizon: Mapped[str] = mapped_column(String(10), nullable=False)
    threshold_pct: Mapped[float] = mapped_column(Float, nullable=False)
    realized_move_pct: Mapped[float | None] = mapped_column(Float)
    outcome_known_on: Mapped[datetime | None] = mapped_column(DateTime)
    event_occurred: Mapped[bool | None] = mapped_column(Boolean)

    cycle: Mapped[AssessmentCycle] = relationship(back_populates="realized_outcomes")
