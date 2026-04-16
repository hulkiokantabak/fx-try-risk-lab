import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hmac import compare_digest
from pathlib import Path
from secrets import token_urlsafe
from statistics import mean
from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models.entities import (
    AgentRoundOutput,
    AssessmentCycle,
    ChatterItem,
    CycleSpecialistActivation,
    Headline,
    HouseView,
    MacroObservation,
    MacroSeries,
    PriceObservation,
    PriceSeries,
    RealizedOutcome,
    Report,
    Source,
    SourceFetchRun,
)
from app.services.assessment_engine import (
    create_assessment_cycle,
    create_follow_up_cycle,
    load_evidence_pack,
    rebuild_assessment_cycle,
)
from app.services.cycle_delta import build_cycle_delta_summary, collect_watch_triggers
from app.services.debate_engine import run_fx_experts_rounds
from app.services.horizons import HORIZON_THRESHOLDS, HORIZONS, horizon_due_date, horizon_sort_key
from app.services.realized_outcomes import sync_realized_outcomes
from app.services.reporting import (
    generate_assessment_report,
    load_report_bytes,
    load_report_html,
)
from app.services.source_refresh import execute_queued_refreshes

settings = get_settings()
templates = Jinja2Templates(directory=str(settings.templates_dir))
router = APIRouter()

SERIES_CODE_PATTERN = re.compile(r"^[A-Za-z0-9._/-]+$")
SERIES_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _%()/+.'&-]*$")
SERIES_CATEGORY_PATTERN = re.compile(r"^[a-z0-9_]+$")
SERIES_CURRENCY_PATTERN = re.compile(r"^[A-Z]{2,10}$")
SERIES_FREQUENCY_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{1,19}$")
EVDS_TEMPLATE_PREFIX = "evds-search:"
EVDS_TEMPLATE_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _%()/+.,'&-]*$")


class PrimaryHorizon(StrEnum):
    ONE_WEEK = "1w"
    ONE_MONTH = "1m"
    THREE_MONTHS = "3m"
    SIX_MONTHS = "6m"
    ONE_YEAR = "1y"


@dataclass
class DashboardStats:
    source_count: int
    assessment_count: int
    latest_score: float | None
    report_count: int


@dataclass
class SourceHealthRow:
    source: Source
    last_status: str
    last_finished_at: datetime | None
    last_items_ingested: int
    last_error_message: str | None
    configured_series_count: int


@dataclass
class EvidenceRow:
    source_name: str
    occurred_at: datetime
    title: str
    summary: str | None
    url: str | None


@dataclass
class MacroSeriesRow:
    series: MacroSeries
    source_name: str
    observation_count: int
    latest_observation_date: datetime | None
    latest_value: float | None


@dataclass
class PriceSeriesRow:
    series: PriceSeries
    source_name: str
    observation_count: int
    latest_observation_date: datetime | None
    latest_value: float | None


@dataclass
class AssessmentHistoryRow:
    cycle: AssessmentCycle
    parent_cycle: AssessmentCycle | None
    follow_up_count: int


@dataclass
class RoundDisplay:
    name: str
    label: str
    outputs: list[AgentRoundOutput]


@dataclass
class RealizedOutcomeDisplay:
    horizon: str
    threshold_pct: float
    due_date: datetime
    predicted_score: float | None
    realized_move_pct: float | None
    outcome_known_on: datetime | None
    event_occurred: bool | None
    calibration_gap: float | None
    absolute_error: float | None
    status_label: str
    status_class: str


@dataclass
class BacktestingRow:
    cycle: AssessmentCycle
    predicted_score: float | None
    due_date: datetime
    realized_move_pct: float | None
    event_occurred: bool | None
    calibration_gap: float | None
    absolute_error: float | None
    status_label: str
    status_class: str


@dataclass
class BacktestingStats:
    resolved_primary_count: int
    pending_primary_count: int
    mean_absolute_error: float | None
    brier_score: float | None
    avg_resolved_probability: float | None
    realized_trigger_rate: float | None
    calibration_bias: float | None


@dataclass
class BacktestingHorizonStats:
    horizon: str
    resolved_count: int
    pending_count: int
    avg_predicted_score: float | None
    trigger_rate: float | None
    mean_absolute_error: float | None
    brier_score: float | None


@dataclass
class CycleTrustSummary:
    trust_label: str
    trust_class: str
    snapshot_age_label: str
    blocked_source_count: int
    readiness_label: str
    readiness_class: str
    disagreement_range: float | None
    queued_refresh_count: int
    next_action: str
    note: str


@dataclass
class CycleLineageSummary:
    parent_cycle: AssessmentCycle | None
    child_cycles: list[AssessmentCycle]
    previous_score: float | None
    current_score: float | None
    score_delta: float | None
    previous_market_regime: str | None
    current_market_regime: str | None
    previous_turkey_regime: str | None
    current_turkey_regime: str | None
    added_specialists: list[str]
    removed_specialists: list[str]


@dataclass
class CycleBriefingSummary:
    answer_note: str
    watch_triggers: list[str]
    watch_note: str
    setup_note: str
    coverage_note: str
    debate_note: str


CBRT_EVDS_GUIDANCE = [
    {
        "title": "CBRT 1 Week Repo",
        "suggested_category": "domestic_rates",
        "frequency": "policy meeting dates",
        "notes": (
            "This is now the main no-key source for domestic policy posture. The public CBRT "
            "page lists the one-week repo rate history directly."
        ),
        "template_code": None,
        "doc_url": "https://www.tcmb.gov.tr/wps/wcm/connect/EN/TCMB%2BEN/Main%2BMenu/Core%2BFunctions/Monetary%2BPolicy/Central%2BBank%2BInterest%2BRates/1%2BWeek%2BRepo",
        "doc_label": "CBRT 1 Week Repo page",
    },
    {
        "title": "CBRT International Reserves and Foreign Currency Liquidity",
        "suggested_category": "reserves",
        "frequency": "weekly",
        "notes": (
            "This is the main no-key reserve source. The public page exposes a weekly ZIP that "
            "contains the reserve workbook used for Round 0 reserve tracking."
        ),
        "template_code": None,
        "doc_url": "https://www.tcmb.gov.tr/wps/wcm/connect/EN/TCMB%2BEN/Main%2BMenu/Statistics/Balance%2Bof%2BPayments%2Band%2BRelated%2BStatistics/International%2BReserves%2Band%2BForeign%2BCurrency%2BLiquidity/",
        "doc_label": "CBRT reserve liquidity page",
    },
    {
        "title": "CBRT EVDS (Optional Advanced)",
        "suggested_category": "optional_advanced",
        "frequency": "account-based",
        "notes": (
            "EVDS stays optional in v1. Keep it only as an advanced adapter later if you decide "
            "to add an account-based CBRT feed on top of the no-key sources."
        ),
        "template_code": None,
        "doc_url": "https://evds2.tcmb.gov.tr/index.php?/evds/login/=",
        "doc_label": "EVDS login",
    },
]


def _issue_csrf_token(request: Request) -> str:
    token = token_urlsafe(32)
    request.session["csrf_token"] = token
    return token


def _require_csrf_token(request: Request, submitted_token: str) -> None:
    expected_token = request.session.get("csrf_token")
    if not expected_token or not compare_digest(expected_token, submitted_token):
        raise HTTPException(status_code=403, detail="Invalid CSRF token")
    request.session.pop("csrf_token", None)


def _set_flash_message(request: Request, message: str) -> None:
    request.session["flash_message"] = message


def _pop_flash_message(request: Request) -> str | None:
    return request.session.pop("flash_message", None)


def _recent_cycles(db: Session, limit: int = 5) -> list[AssessmentCycle]:
    return list(
        db.scalars(
            select(AssessmentCycle)
            .order_by(AssessmentCycle.assessment_timestamp.desc(), AssessmentCycle.id.desc())
            .limit(limit)
        ).all()
    )


def _assessment_history_rows(db: Session) -> list[AssessmentHistoryRow]:
    cycles = list(
        db.scalars(
            select(AssessmentCycle).order_by(
                AssessmentCycle.assessment_timestamp.desc(),
                AssessmentCycle.id.desc(),
            )
        ).all()
    )
    cycles_by_id = {cycle.id: cycle for cycle in cycles}
    follow_up_counts: dict[int, int] = {}
    for cycle in cycles:
        if cycle.parent_cycle_id is not None:
            follow_up_counts[cycle.parent_cycle_id] = (
                follow_up_counts.get(cycle.parent_cycle_id, 0) + 1
            )
    return [
        AssessmentHistoryRow(
            cycle=cycle,
            parent_cycle=cycles_by_id.get(cycle.parent_cycle_id),
            follow_up_count=follow_up_counts.get(cycle.id, 0),
        )
        for cycle in cycles
    ]


def _dashboard_stats(db: Session) -> DashboardStats:
    source_count = db.scalar(select(func.count()).select_from(Source)) or 0
    assessment_count = db.scalar(select(func.count()).select_from(AssessmentCycle)) or 0
    report_count = db.scalar(select(func.count()).select_from(Report)) or 0
    latest_score = db.scalar(
        select(HouseView.house_primary_score).order_by(HouseView.created_at.desc()).limit(1)
    )
    return DashboardStats(
        source_count=source_count,
        assessment_count=assessment_count,
        latest_score=latest_score,
        report_count=report_count,
    )


def _source_health_rows(db: Session) -> list[SourceHealthRow]:
    sources = list(
        db.scalars(select(Source).order_by(Source.trust_tier.asc(), Source.name.asc())).all()
    )
    rows: list[SourceHealthRow] = []
    for source in sources:
        if source.category == "market_prices":
            configured_series_count = db.scalar(
                select(func.count())
                .select_from(PriceSeries)
                .where(PriceSeries.source_id == source.id)
            ) or 0
        else:
            configured_series_count = db.scalar(
                select(func.count())
                .select_from(MacroSeries)
                .where(MacroSeries.source_id == source.id)
            ) or 0
        latest_run = db.scalar(
            select(SourceFetchRun)
            .where(SourceFetchRun.source_id == source.id)
            .order_by(SourceFetchRun.started_at.desc(), SourceFetchRun.id.desc())
            .limit(1)
        )
        rows.append(
            SourceHealthRow(
                source=source,
                last_status=latest_run.status if latest_run else "never-run",
                last_finished_at=latest_run.finished_at if latest_run else None,
                last_items_ingested=latest_run.items_ingested if latest_run else 0,
                last_error_message=latest_run.error_message if latest_run else None,
                configured_series_count=configured_series_count,
            )
        )
    return rows


def _snapshot_age_label(timestamp: datetime) -> str:
    delta = datetime.now(UTC).replace(tzinfo=None) - timestamp
    if delta < timedelta(0):
        delta = timedelta(0)
    if delta < timedelta(hours=1):
        minutes = max(int(delta.total_seconds() // 60), 0)
        return "just now" if minutes < 5 else f"{minutes}m old"
    if delta < timedelta(days=1):
        hours = max(int(delta.total_seconds() // 3600), 1)
        return f"{hours}h old"
    days = delta.days
    return "1 day old" if days == 1 else f"{days} days old"


def _blocked_source_count(evidence_pack: dict | None) -> int:
    if not evidence_pack:
        return 0
    return sum(
        1
        for entry in evidence_pack.get("sources", [])
        if entry.get("last_fetch", {}).get("status")
        in {"credentials-required", "config-required", "error"}
    )


def _readiness_overview(evidence_pack: dict | None) -> tuple[str, str]:
    if not evidence_pack:
        return "No readiness read", "pending"
    layers = (evidence_pack.get("expert_readiness") or {}).get("layers", [])
    statuses = [layer.get("status") for layer in layers if layer.get("status")]
    if not statuses:
        return "No readiness read", "pending"
    if all(status == "ready" for status in statuses):
        return "All lenses ready", "success"
    if any(status in {"ready", "partial"} for status in statuses):
        return "Partial coverage", "partial-success"
    return "Thin coverage", "config-required"


def _next_action_label(
    evidence_pack: dict | None,
    latest_house_view: HouseView | None,
    latest_pdf_report: Report | None,
) -> str:
    if evidence_pack and evidence_pack.get("action_queue"):
        return evidence_pack["action_queue"][0].get("title", "Review setup queue")
    if latest_house_view is None:
        return "Run FX Experts rounds"
    if latest_pdf_report is None:
        return "Generate PDF brief"
    return "Create follow-up cycle"


def _cycle_briefing_summary(
    evidence_pack: dict | None,
    latest_house_view: HouseView | None,
    round_displays: list[RoundDisplay],
    trust_summary: CycleTrustSummary,
) -> CycleBriefingSummary:
    round4_outputs = [
        output
        for round_display in round_displays
        if round_display.name == "round4"
        for output in round_display.outputs
    ]
    watch_triggers = collect_watch_triggers(round4_outputs)
    action_queue = (evidence_pack or {}).get("action_queue") or []
    top_setup_action = (
        action_queue[0].get("title", "Review the setup queue")
        if action_queue
        else "No urgent setup action is queued."
    )
    macro_summary = (evidence_pack or {}).get("macro_summary") or {}
    price_summary = (evidence_pack or {}).get("price_summary") or {}
    news_summary = (evidence_pack or {}).get("news_summary") or {}
    market_regime = (price_summary.get("market_regime") or {}).get("regime_label") or "n/a"
    macro_coverage = (
        f"{macro_summary.get('series_with_observations', 0)}/"
        f"{macro_summary.get('configured_series', 0)} macro"
    )
    market_coverage = (
        f"{price_summary.get('series_with_observations', 0)}/"
        f"{price_summary.get('configured_series', 0)} market"
    )
    headline_count = news_summary.get("headline_count_14d", 0)
    chatter_count = news_summary.get("chatter_count_14d", 0)
    coverage_note = (
        f"{macro_coverage}, {market_coverage}, "
        f"{headline_count} headlines, {chatter_count} chatter items. "
        f"Current market regime: {market_regime}."
    )

    if latest_house_view is None:
        answer_note = (
            "No house view is stored yet. Finish Rounds 1-4 to produce the desk answer, "
            "confidence, and probability curve."
        )
    else:
        answer_note = (
            f"Primary horizon {latest_house_view.primary_horizon} sits at "
            f"{latest_house_view.house_primary_score:.1f} with "
            f"{latest_house_view.house_confidence} confidence. "
            f"Trust state: {trust_summary.trust_label.lower()}."
        )

    if watch_triggers:
        watch_note = "Watch list: " + ", ".join(watch_triggers[:5]) + "."
    else:
        watch_note = "No Round 4 watch triggers are stored yet."

    if round_displays:
        total_outputs = sum(len(round_display.outputs) for round_display in round_displays)
        debate_note = (
            f"{len(round_displays)} rounds are stored with {total_outputs} total outputs. "
            "Open the full workup for the complete desk transcript."
        )
    else:
        debate_note = (
            "Debate outputs are not stored yet. Run the FX Experts rounds after "
            "Round 0 looks ready."
        )

    return CycleBriefingSummary(
        answer_note=answer_note,
        watch_triggers=watch_triggers,
        watch_note=watch_note,
        setup_note=top_setup_action,
        coverage_note=coverage_note,
        debate_note=debate_note,
    )


def _cycle_trust_summary(
    cycle: AssessmentCycle,
    evidence_pack: dict | None,
    latest_house_view: HouseView | None,
    latest_pdf_report: Report | None,
) -> CycleTrustSummary:
    snapshot_age_label = _snapshot_age_label(cycle.assessment_timestamp)
    blocked_source_count = _blocked_source_count(evidence_pack)
    readiness_label, readiness_class = _readiness_overview(evidence_pack)
    queued_refresh_count = (
        (evidence_pack or {}).get("refresh_plan", {}).get("queued_count", 0)
    )
    next_action = _next_action_label(evidence_pack, latest_house_view, latest_pdf_report)
    disagreement_range = (
        latest_house_view.disagreement_range if latest_house_view is not None else None
    )

    if evidence_pack is None:
        trust_label = "No snapshot"
        trust_class = "pending"
    elif blocked_source_count > 0:
        trust_label = "Blocked inputs"
        trust_class = "config-required"
    elif queued_refresh_count > 0:
        trust_label = "Refresh pending"
        trust_class = "pending"
    elif latest_house_view is None:
        trust_label = "Evidence ready"
        trust_class = "success" if readiness_class == "success" else readiness_class
    elif latest_house_view.stress_flag:
        trust_label = "Stress on"
        trust_class = "triggered"
    elif disagreement_range is not None and disagreement_range >= 20:
        trust_label = "Split desk"
        trust_class = "partial-success"
    elif readiness_class != "success":
        trust_label = "Use caution"
        trust_class = "partial-success"
    else:
        trust_label = "Desk ready"
        trust_class = "success"

    note_bits = [f"Snapshot is {snapshot_age_label}."]
    if blocked_source_count:
        note_bits.append(f"{blocked_source_count} sources are blocked or incomplete.")
    elif queued_refresh_count:
        note_bits.append(f"{queued_refresh_count} queued refreshes are still pending.")
    else:
        note_bits.append("No source blockers are currently visible.")
    note_bits.append(f"Next best action: {next_action}.")
    return CycleTrustSummary(
        trust_label=trust_label,
        trust_class=trust_class,
        snapshot_age_label=snapshot_age_label,
        blocked_source_count=blocked_source_count,
        readiness_label=readiness_label,
        readiness_class=readiness_class,
        disagreement_range=disagreement_range,
        queued_refresh_count=queued_refresh_count,
        next_action=next_action,
        note=" ".join(note_bits),
    )


def _cycle_lineage_summary(
    db: Session,
    cycle: AssessmentCycle,
    evidence_pack: dict | None,
    activations: list[CycleSpecialistActivation],
    latest_house_view: HouseView | None,
) -> CycleLineageSummary:
    parent_cycle = (
        db.get(AssessmentCycle, cycle.parent_cycle_id)
        if cycle.parent_cycle_id is not None
        else None
    )
    child_cycles = list(
        db.scalars(
            select(AssessmentCycle)
            .where(AssessmentCycle.parent_cycle_id == cycle.id)
            .order_by(
                AssessmentCycle.assessment_timestamp.desc(),
                AssessmentCycle.id.desc(),
            )
            .limit(4)
        ).all()
    )
    previous_score = None
    score_delta = None
    previous_market_regime = None
    previous_turkey_regime = None
    added_specialists: list[str] = []
    removed_specialists: list[str] = []
    if parent_cycle is not None:
        previous_house_view = _latest_house_view_for_cycle(db, parent_cycle.id)
        if previous_house_view is not None:
            previous_score = previous_house_view.house_primary_score
        if latest_house_view is not None and previous_score is not None:
            score_delta = round(latest_house_view.house_primary_score - previous_score, 1)
        previous_pack = load_evidence_pack(parent_cycle) or {}
        previous_market_regime = (
            ((previous_pack.get("price_summary") or {}).get("market_regime") or {}).get(
                "regime_label"
            )
        )
        previous_turkey_regime = (
            (
                (previous_pack.get("macro_summary") or {}).get("turkey_policy_reserves")
                or {}
            ).get("regime_label")
        )
        previous_activation_names = {
            activation.specialist_name
            for activation in db.scalars(
                select(CycleSpecialistActivation).where(
                    CycleSpecialistActivation.cycle_id == parent_cycle.id
                )
            ).all()
        }
        current_activation_names = {activation.specialist_name for activation in activations}
        added_specialists = sorted(current_activation_names - previous_activation_names)
        removed_specialists = sorted(previous_activation_names - current_activation_names)

    current_market_regime = (
        (((evidence_pack or {}).get("price_summary") or {}).get("market_regime") or {}).get(
            "regime_label"
        )
    )
    current_turkey_regime = (
        (
            (((evidence_pack or {}).get("macro_summary") or {}).get("turkey_policy_reserves"))
            or {}
        ).get("regime_label")
    )
    return CycleLineageSummary(
        parent_cycle=parent_cycle,
        child_cycles=child_cycles,
        previous_score=previous_score,
        current_score=(
            latest_house_view.house_primary_score if latest_house_view is not None else None
        ),
        score_delta=score_delta,
        previous_market_regime=previous_market_regime,
        current_market_regime=current_market_regime,
        previous_turkey_regime=previous_turkey_regime,
        current_turkey_regime=current_turkey_regime,
        added_specialists=added_specialists,
        removed_specialists=removed_specialists,
    )


def _recent_headlines(db: Session, limit: int = 8) -> list[EvidenceRow]:
    rows = db.execute(
        select(Headline, Source.name)
        .join(Source, Headline.source_id == Source.id, isouter=True)
        .order_by(Headline.published_at.desc(), Headline.id.desc())
        .limit(limit)
    ).all()
    return [
        EvidenceRow(
            source_name=source_name or "Unknown source",
            occurred_at=headline.published_at,
            title=headline.title,
            summary=headline.summary,
            url=headline.url,
        )
        for headline, source_name in rows
    ]


def _recent_chatter(db: Session, limit: int = 8) -> list[EvidenceRow]:
    rows = db.execute(
        select(ChatterItem, Source.name)
        .join(Source, ChatterItem.source_id == Source.id, isouter=True)
        .order_by(ChatterItem.posted_at.desc(), ChatterItem.id.desc())
        .limit(limit)
    ).all()
    return [
        EvidenceRow(
            source_name=source_name or "Unknown source",
            occurred_at=item.posted_at,
            title=item.content,
            summary=item.author,
            url=item.url,
        )
        for item, source_name in rows
    ]


def _macro_snapshot_rows(db: Session, limit: int = 10) -> list[MacroSeriesRow]:
    rows = [
        row
        for row in _macro_series_rows(db)
        if row.observation_count > 0 and row.latest_observation_date is not None
    ]
    rows.sort(
        key=lambda row: (
            row.latest_observation_date or datetime.min,
            row.series.name.casefold(),
        ),
        reverse=True,
    )
    return rows[:limit]


def _price_snapshot_rows(db: Session, limit: int = 12) -> list[PriceSeriesRow]:
    rows = [
        row
        for row in _price_series_rows(db)
        if row.observation_count > 0 and row.latest_observation_date is not None
    ]
    rows.sort(
        key=lambda row: (
            row.latest_observation_date or datetime.min,
            row.series.name.casefold(),
        ),
        reverse=True,
    )
    return rows[:limit]


def _macro_series_rows(db: Session) -> list[MacroSeriesRow]:
    series_list = list(
        db.scalars(
            select(MacroSeries).order_by(MacroSeries.category.asc(), MacroSeries.name.asc())
        ).all()
    )
    rows: list[MacroSeriesRow] = []
    for series in series_list:
        source_name = db.scalar(select(Source.name).where(Source.id == series.source_id).limit(1))
        observation_count = db.scalar(
            select(func.count())
            .select_from(MacroObservation)
            .where(MacroObservation.series_id == series.id)
        ) or 0
        latest_observation = db.scalar(
            select(MacroObservation)
            .where(MacroObservation.series_id == series.id)
            .order_by(MacroObservation.observation_date.desc(), MacroObservation.id.desc())
            .limit(1)
        )
        rows.append(
            MacroSeriesRow(
                series=series,
                source_name=source_name or "Unknown source",
                observation_count=observation_count,
                latest_observation_date=(
                    latest_observation.observation_date if latest_observation else None
                ),
                latest_value=latest_observation.value if latest_observation else None,
            )
        )
    return rows


def _official_sources(db: Session) -> list[Source]:
    return list(
        db.scalars(
            select(Source)
            .where(Source.category == "official_macro")
            .order_by(Source.name.asc(), Source.id.asc())
        ).all()
    )


def _market_price_sources(db: Session) -> list[Source]:
    return list(
        db.scalars(
            select(Source)
            .where(Source.category == "market_prices")
            .order_by(Source.name.asc(), Source.id.asc())
        ).all()
    )


def _price_series_rows(db: Session) -> list[PriceSeriesRow]:
    series_list = list(
        db.scalars(
            select(PriceSeries).order_by(PriceSeries.name.asc(), PriceSeries.id.asc())
        ).all()
    )
    rows: list[PriceSeriesRow] = []
    for series in series_list:
        source_name = db.scalar(select(Source.name).where(Source.id == series.source_id).limit(1))
        observation_count = db.scalar(
            select(func.count())
            .select_from(PriceObservation)
            .where(PriceObservation.series_id == series.id)
        ) or 0
        latest_observation = db.scalar(
            select(PriceObservation)
            .where(PriceObservation.series_id == series.id)
            .order_by(PriceObservation.observed_at.desc(), PriceObservation.id.desc())
            .limit(1)
        )
        rows.append(
            PriceSeriesRow(
                series=series,
                source_name=source_name or "Unknown source",
                observation_count=observation_count,
                latest_observation_date=(
                    latest_observation.observed_at if latest_observation else None
                ),
                latest_value=latest_observation.close_value if latest_observation else None,
            )
        )
    return rows


def _validate_macro_series_form(
    *,
    code: str,
    name: str,
    category: str,
    frequency: str,
) -> tuple[str, str, str, str]:
    normalized_code = code.strip()
    normalized_name = name.strip()
    normalized_category = category.strip().casefold()
    normalized_frequency = frequency.strip().casefold()

    if normalized_code.startswith(EVDS_TEMPLATE_PREFIX):
        _validate_evds_template_code(normalized_code)
    else:
        if not SERIES_CODE_PATTERN.fullmatch(normalized_code):
            raise HTTPException(
                status_code=400,
                detail=(
                    "Series code may only contain letters, digits, dots, slashes, underscores, "
                    "and hyphens."
                ),
            )
        if (
            ".." in normalized_code
            or normalized_code.startswith(("/", "."))
            or normalized_code.endswith("/")
            or "//" in normalized_code
        ):
            raise HTTPException(
                status_code=400,
                detail="Series code cannot contain path-traversal-like segments.",
            )
    if not SERIES_LABEL_PATTERN.fullmatch(normalized_name):
        raise HTTPException(
            status_code=400,
            detail="Series name contains unsupported characters.",
        )
    if not SERIES_CATEGORY_PATTERN.fullmatch(normalized_category):
        raise HTTPException(
            status_code=400,
            detail="Category must be lowercase snake_case.",
        )
    if not SERIES_FREQUENCY_PATTERN.fullmatch(normalized_frequency):
        raise HTTPException(
            status_code=400,
            detail="Frequency must be a short lowercase token such as daily or monthly.",
        )
    return (
        normalized_code,
        normalized_name,
        normalized_category,
        normalized_frequency,
    )


def _validate_evds_template_code(code: str) -> None:
    raw_body = code[len(EVDS_TEMPLATE_PREFIX) :].strip()
    if not raw_body:
        raise HTTPException(
            status_code=400,
            detail="EVDS template codes must include an official data-group title.",
        )
    if ".." in raw_body or "//" in raw_body or raw_body.startswith(("/", ".")):
        raise HTTPException(
            status_code=400,
            detail="EVDS template codes cannot contain path-traversal-like segments.",
        )

    segments = [segment.strip() for segment in raw_body.split("|", 1)]
    if any(not segment for segment in segments):
        raise HTTPException(
            status_code=400,
            detail="EVDS template codes must keep both the title and optional hint non-empty.",
        )
    for segment in segments:
        if not EVDS_TEMPLATE_SEGMENT_PATTERN.fullmatch(segment):
            raise HTTPException(
                status_code=400,
                detail=(
                    "EVDS template titles may only contain letters, digits, spaces, and "
                    "light punctuation."
                ),
            )


def _validate_price_series_form(
    *,
    symbol: str,
    name: str,
    frequency: str,
    base_currency: str | None,
    quote_currency: str | None,
) -> tuple[str, str, str, str | None, str | None]:
    normalized_symbol = symbol.strip()
    normalized_name = name.strip()
    normalized_frequency = frequency.strip().casefold()
    normalized_base = base_currency.strip().upper() if base_currency else None
    normalized_quote = quote_currency.strip().upper() if quote_currency else None

    if not SERIES_CODE_PATTERN.fullmatch(normalized_symbol):
        raise HTTPException(
            status_code=400,
            detail=(
                "Price symbol may only contain letters, digits, dots, slashes, underscores, "
                "and hyphens."
            ),
        )
    if (
        ".." in normalized_symbol
        or normalized_symbol.startswith(("/", "."))
        or normalized_symbol.endswith("/")
        or "//" in normalized_symbol
    ):
        raise HTTPException(
            status_code=400,
            detail="Price symbol cannot contain path-traversal-like segments.",
        )
    if not SERIES_LABEL_PATTERN.fullmatch(normalized_name):
        raise HTTPException(
            status_code=400,
            detail="Series name contains unsupported characters.",
        )
    if not SERIES_FREQUENCY_PATTERN.fullmatch(normalized_frequency):
        raise HTTPException(
            status_code=400,
            detail="Frequency must be a short lowercase token such as daily or weekly.",
        )
    if normalized_base and not SERIES_CURRENCY_PATTERN.fullmatch(normalized_base):
        raise HTTPException(
            status_code=400,
            detail="Base currency must be an uppercase token such as EUR or USD.",
        )
    if normalized_quote and not SERIES_CURRENCY_PATTERN.fullmatch(normalized_quote):
        raise HTTPException(
            status_code=400,
            detail="Quote currency must be an uppercase token such as TRY or USD.",
        )
    if bool(normalized_base) != bool(normalized_quote):
        raise HTTPException(
            status_code=400,
            detail="Provide both base and quote currency for FX pairs, or leave both blank.",
        )

    return (
        normalized_symbol,
        normalized_name,
        normalized_frequency,
        normalized_base,
        normalized_quote,
    )


def _round_displays(db: Session, cycle_id: int) -> list[RoundDisplay]:
    outputs = list(
        db.scalars(
            select(AgentRoundOutput)
            .where(AgentRoundOutput.cycle_id == cycle_id)
            .order_by(
                AgentRoundOutput.round_name.asc(),
                AgentRoundOutput.created_at.asc(),
                AgentRoundOutput.id.asc(),
            )
        ).all()
    )
    grouped: dict[str, list[AgentRoundOutput]] = {}
    for output in outputs:
        grouped.setdefault(output.round_name, []).append(output)

    labels = {
        "round1": "Round 1 - Topic Framing",
        "round2": "Round 2 - Initial Thesis",
        "round3": "Round 3 - Challenge And Revision",
        "round4": "Round 4 - Final Verdict",
    }
    return [
        RoundDisplay(
            name=round_name,
            label=labels.get(round_name, round_name.title()),
            outputs=grouped[round_name],
        )
        for round_name in ("round1", "round2", "round3", "round4")
        if grouped.get(round_name)
    ]


def _realized_outcome_displays(
    db: Session,
    cycle: AssessmentCycle,
    latest_house_view: HouseView | None,
) -> list[RealizedOutcomeDisplay]:
    outcomes = list(
        db.scalars(
            select(RealizedOutcome)
            .where(RealizedOutcome.cycle_id == cycle.id)
            .order_by(RealizedOutcome.id.asc())
        ).all()
    )
    outcomes.sort(key=lambda item: horizon_sort_key(item.horizon))
    displays: list[RealizedOutcomeDisplay] = []
    for outcome in outcomes:
        predicted_score = None
        if latest_house_view is not None:
            predicted_score = latest_house_view.risk_curve.get(outcome.horizon)
        actual_event_score = None
        if outcome.event_occurred is not None:
            actual_event_score = 100.0 if outcome.event_occurred else 0.0

        calibration_gap = None
        absolute_error = None
        if predicted_score is not None and actual_event_score is not None:
            calibration_gap = round(predicted_score - actual_event_score, 1)
            absolute_error = round(abs(calibration_gap), 1)

        status_label = "Pending"
        status_class = "pending"
        if outcome.event_occurred is True:
            status_label = "Triggered"
            status_class = "triggered"
        elif outcome.event_occurred is False:
            status_label = "Cleared"
            status_class = "cleared"

        displays.append(
            RealizedOutcomeDisplay(
                horizon=outcome.horizon,
                threshold_pct=outcome.threshold_pct,
                due_date=horizon_due_date(cycle.assessment_timestamp, outcome.horizon),
                predicted_score=predicted_score,
                realized_move_pct=outcome.realized_move_pct,
                outcome_known_on=outcome.outcome_known_on,
                event_occurred=outcome.event_occurred,
                calibration_gap=calibration_gap,
                absolute_error=absolute_error,
                status_label=status_label,
                status_class=status_class,
            )
        )
    return displays


def _latest_house_view_for_cycle(db: Session, cycle_id: int) -> HouseView | None:
    return db.scalar(
        select(HouseView)
        .where(HouseView.cycle_id == cycle_id)
        .order_by(HouseView.created_at.desc(), HouseView.id.desc())
        .limit(1)
    )


def _latest_report_for_cycle(
    db: Session,
    cycle_id: int,
    *,
    report_type: str,
) -> Report | None:
    return db.scalar(
        select(Report)
        .where(
            Report.cycle_id == cycle_id,
            Report.report_type == report_type,
        )
        .order_by(Report.generated_at.desc(), Report.id.desc())
        .limit(1)
    )


def _backtesting_rows(
    db: Session,
) -> tuple[list[BacktestingRow], BacktestingStats, list[BacktestingHorizonStats]]:
    cycles = list(
        db.scalars(
            select(AssessmentCycle).order_by(
                AssessmentCycle.assessment_timestamp.desc(),
                AssessmentCycle.id.desc(),
            )
        ).all()
    )
    rows: list[BacktestingRow] = []
    absolute_errors: list[float] = []
    brier_scores: list[float] = []
    resolved_predicted_scores: list[float] = []
    resolved_event_scores: list[float] = []
    horizon_metrics = {
        horizon: {
            "resolved_count": 0,
            "pending_count": 0,
            "predicted_scores": [],
            "event_scores": [],
            "absolute_errors": [],
            "brier_scores": [],
        }
        for horizon in HORIZONS
    }
    for cycle in cycles:
        latest_house_view = _latest_house_view_for_cycle(db, cycle.id)
        primary_outcome = db.scalar(
            select(RealizedOutcome)
            .where(
                RealizedOutcome.cycle_id == cycle.id,
                RealizedOutcome.horizon == cycle.primary_horizon,
            )
            .limit(1)
        )
        if primary_outcome is None:
            primary_outcome = RealizedOutcome(
                cycle_id=cycle.id,
                horizon=cycle.primary_horizon,
                threshold_pct=HORIZON_THRESHOLDS[cycle.primary_horizon],
                realized_move_pct=None,
                outcome_known_on=None,
                event_occurred=None,
            )

        predicted_score = None
        if latest_house_view is not None:
            predicted_score = latest_house_view.risk_curve.get(cycle.primary_horizon)
        actual_event_score = None
        if primary_outcome.event_occurred is not None:
            actual_event_score = 100.0 if primary_outcome.event_occurred else 0.0

        horizon_metric = horizon_metrics[cycle.primary_horizon]
        if primary_outcome.event_occurred is None:
            horizon_metric["pending_count"] += 1
        else:
            horizon_metric["resolved_count"] += 1
            horizon_metric["event_scores"].append(actual_event_score)
            resolved_event_scores.append(actual_event_score)
            if predicted_score is not None:
                horizon_metric["predicted_scores"].append(predicted_score)
                resolved_predicted_scores.append(predicted_score)

        calibration_gap = None
        absolute_error = None
        if predicted_score is not None and actual_event_score is not None:
            calibration_gap = round(predicted_score - actual_event_score, 1)
            absolute_error = round(abs(calibration_gap), 1)
            absolute_errors.append(absolute_error)
            horizon_metric["absolute_errors"].append(absolute_error)
            brier_score = ((predicted_score / 100) - (actual_event_score / 100)) ** 2
            brier_scores.append(brier_score)
            horizon_metric["brier_scores"].append(brier_score)

        status_label = "Pending"
        status_class = "pending"
        if primary_outcome.event_occurred is True:
            status_label = "Triggered"
            status_class = "triggered"
        elif primary_outcome.event_occurred is False:
            status_label = "Cleared"
            status_class = "cleared"

        rows.append(
            BacktestingRow(
                cycle=cycle,
                predicted_score=predicted_score,
                due_date=horizon_due_date(cycle.assessment_timestamp, cycle.primary_horizon),
                realized_move_pct=primary_outcome.realized_move_pct,
                event_occurred=primary_outcome.event_occurred,
                calibration_gap=calibration_gap,
                absolute_error=absolute_error,
                status_label=status_label,
                status_class=status_class,
            )
        )

    avg_resolved_probability = (
        round(mean(resolved_predicted_scores), 1) if resolved_predicted_scores else None
    )
    realized_trigger_rate = round(mean(resolved_event_scores), 1) if resolved_event_scores else None
    calibration_bias = None
    if avg_resolved_probability is not None and realized_trigger_rate is not None:
        calibration_bias = round(avg_resolved_probability - realized_trigger_rate, 1)

    stats = BacktestingStats(
        resolved_primary_count=sum(1 for row in rows if row.event_occurred is not None),
        pending_primary_count=sum(1 for row in rows if row.event_occurred is None),
        mean_absolute_error=round(mean(absolute_errors), 2) if absolute_errors else None,
        brier_score=round(mean(brier_scores), 4) if brier_scores else None,
        avg_resolved_probability=avg_resolved_probability,
        realized_trigger_rate=realized_trigger_rate,
        calibration_bias=calibration_bias,
    )
    horizon_stats: list[BacktestingHorizonStats] = []
    for horizon in HORIZONS:
        metrics = horizon_metrics[horizon]
        horizon_stats.append(
            BacktestingHorizonStats(
                horizon=horizon,
                resolved_count=metrics["resolved_count"],
                pending_count=metrics["pending_count"],
                avg_predicted_score=(
                    round(mean(metrics["predicted_scores"]), 1)
                    if metrics["predicted_scores"]
                    else None
                ),
                trigger_rate=(
                    round(mean(metrics["event_scores"]), 1)
                    if metrics["event_scores"]
                    else None
                ),
                mean_absolute_error=(
                    round(mean(metrics["absolute_errors"]), 2)
                    if metrics["absolute_errors"]
                    else None
                ),
                brier_score=(
                    round(mean(metrics["brier_scores"]), 4)
                    if metrics["brier_scores"]
                    else None
                ),
            )
        )
    return rows, stats, horizon_stats


@router.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    from app.db import SessionLocal

    with SessionLocal() as db:
        stats = _dashboard_stats(db)
        recent_cycles = _recent_cycles(db)
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "app_name": settings.app_name,
            "page_title": "Dashboard",
            "stats": stats,
            "primary_horizon": "1m",
            "recent_cycles": recent_cycles,
        },
    )


@router.get("/assessments/new", response_class=HTMLResponse)
def new_assessment(request: Request) -> HTMLResponse:
    csrf_token = _issue_csrf_token(request)
    return templates.TemplateResponse(
        request,
        "new_assessment.html",
        {
            "app_name": settings.app_name,
            "page_title": "New Assessment",
            "horizons": [horizon.value for horizon in PrimaryHorizon],
            "csrf_token": csrf_token,
        },
    )


@router.post("/assessments")
def create_assessment(
    request: Request,
    primary_horizon: Annotated[PrimaryHorizon, Form(...)],
    csrf_token: Annotated[str, Form(...)],
    custom_context: Annotated[str | None, Form(max_length=4000)] = None,
    refresh_official_data: Annotated[bool, Form()] = False,
    include_news_chatter: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    from app.db import SessionLocal

    _require_csrf_token(request, csrf_token)

    with SessionLocal() as db:
        cycle = create_assessment_cycle(
            db,
            settings,
            primary_horizon=primary_horizon.value,
            custom_context=custom_context,
            refresh_official_data=refresh_official_data,
            include_news_chatter=include_news_chatter,
        )

    return RedirectResponse(
        url=f"/assessments/{cycle.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/assessments/{cycle_id}/follow-up")
def create_follow_up_assessment(
    request: Request,
    cycle_id: int,
    csrf_token: Annotated[str, Form(...)],
) -> RedirectResponse:
    from app.db import SessionLocal

    _require_csrf_token(request, csrf_token)

    with SessionLocal() as db:
        try:
            cycle = create_follow_up_cycle(db, settings, source_cycle_id=cycle_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    _set_flash_message(
        request,
        (
            f"Created follow-up cycle {cycle.id} from cycle {cycle_id}. "
            "The original cycle stayed frozen."
        ),
    )
    return RedirectResponse(
        url=f"/assessments/{cycle.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/assessments", response_class=HTMLResponse)
def assessment_history(request: Request) -> HTMLResponse:
    from app.db import SessionLocal

    with SessionLocal() as db:
        history_rows = _assessment_history_rows(db)
    return templates.TemplateResponse(
        request,
        "assessments.html",
        {
            "app_name": settings.app_name,
            "page_title": "Assessment History",
            "history_rows": history_rows,
        },
    )


@router.get("/sources", response_class=HTMLResponse)
def source_health(request: Request) -> HTMLResponse:
    from app.db import SessionLocal

    with SessionLocal() as db:
        rows = _source_health_rows(db)
    csrf_token = _issue_csrf_token(request)

    return templates.TemplateResponse(
        request,
        "sources.html",
        {
            "app_name": settings.app_name,
            "page_title": "Source Health",
            "rows": rows,
            "csrf_token": csrf_token,
            "flash_message": _pop_flash_message(request),
        },
    )


@router.post("/sources/refresh")
def refresh_sources(
    request: Request,
    csrf_token: Annotated[str, Form(...)],
) -> RedirectResponse:
    from app.db import SessionLocal

    _require_csrf_token(request, csrf_token)

    with SessionLocal() as db:
        summary = execute_queued_refreshes(db, settings)
    _set_flash_message(request, summary["message"])
    return RedirectResponse(url="/sources", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/assessments/{cycle_id}/refresh-sources")
def refresh_assessment_sources(
    request: Request,
    cycle_id: int,
    csrf_token: Annotated[str, Form(...)],
) -> RedirectResponse:
    from app.db import SessionLocal

    _require_csrf_token(request, csrf_token)

    with SessionLocal() as db:
        cycle = db.get(AssessmentCycle, cycle_id)
        if cycle is None:
            raise HTTPException(status_code=404, detail="Assessment cycle not found")
        summary = execute_queued_refreshes(db, settings)
    _set_flash_message(request, summary["message"])
    return RedirectResponse(
        url=f"/assessments/{cycle_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/assessments/{cycle_id}", response_class=HTMLResponse)
def assessment_detail(request: Request, cycle_id: int) -> HTMLResponse:
    from app.db import SessionLocal

    with SessionLocal() as db:
        cycle = db.get(AssessmentCycle, cycle_id)
        if cycle is None:
            raise HTTPException(status_code=404, detail="Assessment cycle not found")
        activations = list(
            db.scalars(
                select(CycleSpecialistActivation)
                .where(CycleSpecialistActivation.cycle_id == cycle.id)
                .order_by(
                    CycleSpecialistActivation.activated_at.asc(),
                    CycleSpecialistActivation.id.asc(),
                )
            ).all()
        )
        evidence_pack = load_evidence_pack(cycle)
        round_displays = _round_displays(db, cycle.id)
        round4_outputs = [
            output
            for round_display in round_displays
            if round_display.name == "round4"
            for output in round_display.outputs
        ]
        latest_house_view = _latest_house_view_for_cycle(db, cycle.id)
        realized_outcomes = _realized_outcome_displays(db, cycle, latest_house_view)
        latest_pdf_report = _latest_report_for_cycle(
            db,
            cycle.id,
            report_type="pdf_assessment",
        )
        latest_html_report = _latest_report_for_cycle(
            db,
            cycle.id,
            report_type="html_assessment",
        )
        trust_summary = _cycle_trust_summary(
            cycle,
            evidence_pack,
            latest_house_view,
            latest_pdf_report,
        )
        briefing_summary = _cycle_briefing_summary(
            evidence_pack,
            latest_house_view,
            round_displays,
            trust_summary,
        )
        lineage_summary = _cycle_lineage_summary(
            db,
            cycle,
            evidence_pack,
            activations,
            latest_house_view,
        )
        delta_summary = build_cycle_delta_summary(
            db,
            cycle,
            evidence_pack=evidence_pack,
            activations=activations,
            latest_house_view=latest_house_view,
            round4_outputs=round4_outputs,
        )

    return templates.TemplateResponse(
        request,
        "assessment_detail.html",
        {
            "app_name": settings.app_name,
            "page_title": cycle.label,
            "cycle": cycle,
            "activations": activations,
            "evidence_pack": evidence_pack,
            "round_displays": round_displays,
            "latest_house_view": latest_house_view,
            "realized_outcomes": realized_outcomes,
            "latest_pdf_report": latest_pdf_report,
            "latest_html_report": latest_html_report,
            "briefing_summary": briefing_summary,
            "trust_summary": trust_summary,
            "lineage_summary": lineage_summary,
            "delta_summary": delta_summary,
            "csrf_token": _issue_csrf_token(request),
            "flash_message": _pop_flash_message(request),
        },
    )


@router.post("/assessments/{cycle_id}/rebuild")
def rebuild_assessment(
    request: Request,
    cycle_id: int,
    csrf_token: Annotated[str, Form(...)],
) -> RedirectResponse:
    from app.db import SessionLocal

    _require_csrf_token(request, csrf_token)

    with SessionLocal() as db:
        try:
            cycle = rebuild_assessment_cycle(db, settings, cycle_id=cycle_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    _set_flash_message(request, f"Rebuilt Round 0 evidence pack for {cycle.label}.")
    return RedirectResponse(
        url=f"/assessments/{cycle.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/assessments/{cycle_id}/run-rounds")
def run_assessment_rounds(
    request: Request,
    cycle_id: int,
    csrf_token: Annotated[str, Form(...)],
) -> RedirectResponse:
    from app.db import SessionLocal

    _require_csrf_token(request, csrf_token)

    with SessionLocal() as db:
        try:
            cycle = run_fx_experts_rounds(db, settings, cycle_id=cycle_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    _set_flash_message(request, f"FX Experts completed rounds 1-4 for {cycle.label}.")
    return RedirectResponse(
        url=f"/assessments/{cycle.id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.post("/assessments/{cycle_id}/generate-report")
def generate_report(
    request: Request,
    cycle_id: int,
    csrf_token: Annotated[str, Form(...)],
) -> RedirectResponse:
    from app.db import SessionLocal

    _require_csrf_token(request, csrf_token)

    with SessionLocal() as db:
        try:
            generate_assessment_report(db, settings, cycle_id=cycle_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    _set_flash_message(request, f"Generated PDF and HTML report artifacts for cycle {cycle_id}.")
    return RedirectResponse(
        url=f"/assessments/{cycle_id}",
        status_code=status.HTTP_303_SEE_OTHER,
    )


@router.get("/backtesting", response_class=HTMLResponse)
def backtesting_dashboard(request: Request) -> HTMLResponse:
    from app.db import SessionLocal

    with SessionLocal() as db:
        rows, stats, horizon_stats = _backtesting_rows(db)
    return templates.TemplateResponse(
        request,
        "backtesting.html",
        {
            "app_name": settings.app_name,
            "page_title": "Backtesting",
            "rows": rows,
            "stats": stats,
            "horizon_stats": horizon_stats,
            "csrf_token": _issue_csrf_token(request),
            "flash_message": _pop_flash_message(request),
        },
    )


@router.post("/backtesting/refresh")
def refresh_backtesting(
    request: Request,
    csrf_token: Annotated[str, Form(...)],
) -> RedirectResponse:
    from app.db import SessionLocal

    _require_csrf_token(request, csrf_token)

    with SessionLocal() as db:
        summary = sync_realized_outcomes(db)
        db.commit()
    _set_flash_message(
        request,
        (
            "Recomputed realized outcomes for "
            f"{summary['cycle_count']} cycles with {summary['resolved_count']} resolved horizons."
        ),
    )
    return RedirectResponse(url="/backtesting", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/reports/{report_id}")
def report_detail(request: Request, report_id: int) -> Response:
    from app.db import SessionLocal

    with SessionLocal() as db:
        report = db.get(Report, report_id)
        if report is None:
            raise HTTPException(status_code=404, detail="Report not found")
        if report.report_type == "pdf_assessment":
            try:
                payload = load_report_bytes(report, settings.reports_dir)
            except ValueError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            filename = Path(report.file_path or f"report-{report.id}.pdf").name
            return Response(
                content=payload,
                media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="{filename}"'},
            )
        try:
            html = load_report_html(report, settings.reports_dir)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
    return HTMLResponse(content=html)


@router.get("/evidence", response_class=HTMLResponse)
def evidence_browser(request: Request) -> HTMLResponse:
    from app.db import SessionLocal

    with SessionLocal() as db:
        macro_rows = _macro_snapshot_rows(db)
        price_rows = _price_snapshot_rows(db)
        headlines = _recent_headlines(db)
        chatter = _recent_chatter(db)

    return templates.TemplateResponse(
        request,
        "evidence.html",
        {
            "app_name": settings.app_name,
            "page_title": "Evidence Browser",
            "macro_rows": macro_rows,
            "price_rows": price_rows,
            "headlines": headlines,
            "chatter": chatter,
        },
    )


@router.get("/macro-series", response_class=HTMLResponse)
def macro_series_catalog(request: Request) -> HTMLResponse:
    from app.db import SessionLocal

    with SessionLocal() as db:
        rows = _macro_series_rows(db)
        sources = _official_sources(db)
    csrf_token = _issue_csrf_token(request)

    return templates.TemplateResponse(
        request,
        "macro_series.html",
        {
            "app_name": settings.app_name,
            "page_title": "Macro Series",
            "rows": rows,
            "sources": sources,
            "cbrt_guidance": CBRT_EVDS_GUIDANCE,
            "csrf_token": csrf_token,
            "flash_message": _pop_flash_message(request),
        },
    )


@router.get("/price-series", response_class=HTMLResponse)
def price_series_catalog(request: Request) -> HTMLResponse:
    from app.db import SessionLocal

    with SessionLocal() as db:
        rows = _price_series_rows(db)
        sources = _market_price_sources(db)
    csrf_token = _issue_csrf_token(request)

    return templates.TemplateResponse(
        request,
        "price_series.html",
        {
            "app_name": settings.app_name,
            "page_title": "Price Series",
            "rows": rows,
            "sources": sources,
            "csrf_token": csrf_token,
            "flash_message": _pop_flash_message(request),
        },
    )


@router.post("/macro-series")
def create_macro_series(
    request: Request,
    source_id: Annotated[int, Form(...)],
    code: Annotated[str, Form(min_length=2, max_length=220)],
    name: Annotated[str, Form(min_length=2, max_length=160)],
    category: Annotated[str, Form(min_length=2, max_length=80)],
    frequency: Annotated[str, Form(min_length=2, max_length=20)],
    csrf_token: Annotated[str, Form(...)],
    unit: Annotated[str | None, Form(max_length=50)] = None,
) -> RedirectResponse:
    from app.db import SessionLocal

    _require_csrf_token(request, csrf_token)
    normalized_code, normalized_name, normalized_category, normalized_frequency = (
        _validate_macro_series_form(
            code=code,
            name=name,
            category=category,
            frequency=frequency,
        )
    )

    with SessionLocal() as db:
        source = db.get(Source, source_id)
        if source is None or source.category != "official_macro":
            raise HTTPException(status_code=404, detail="Official source not found")
        existing = db.scalar(
            select(MacroSeries.id).where(MacroSeries.code == normalized_code).limit(1)
        )
        if existing is not None:
            _set_flash_message(request, f"Series {normalized_code} already exists.")
            return RedirectResponse(url="/macro-series", status_code=status.HTTP_303_SEE_OTHER)

        db.add(
            MacroSeries(
                source_id=source.id,
                code=normalized_code,
                name=normalized_name,
                category=normalized_category,
                frequency=normalized_frequency,
                unit=(unit or "").strip() or None,
            )
        )
        db.commit()

    _set_flash_message(request, f"Added macro series {normalized_code}.")
    return RedirectResponse(url="/macro-series", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/price-series")
def create_price_series(
    request: Request,
    source_id: Annotated[int, Form(...)],
    symbol: Annotated[str, Form(min_length=1, max_length=50)],
    name: Annotated[str, Form(min_length=2, max_length=120)],
    frequency: Annotated[str, Form(min_length=2, max_length=20)],
    csrf_token: Annotated[str, Form(...)],
    base_currency: Annotated[str | None, Form(max_length=10)] = None,
    quote_currency: Annotated[str | None, Form(max_length=10)] = None,
) -> RedirectResponse:
    from app.db import SessionLocal

    _require_csrf_token(request, csrf_token)
    (
        normalized_symbol,
        normalized_name,
        normalized_frequency,
        normalized_base,
        normalized_quote,
    ) = _validate_price_series_form(
        symbol=symbol,
        name=name,
        frequency=frequency,
        base_currency=base_currency,
        quote_currency=quote_currency,
    )

    with SessionLocal() as db:
        source = db.get(Source, source_id)
        if source is None or source.category != "market_prices":
            raise HTTPException(status_code=404, detail="Market-price source not found")
        existing = db.scalar(
            select(PriceSeries.id).where(PriceSeries.symbol == normalized_symbol).limit(1)
        )
        if existing is not None:
            _set_flash_message(request, f"Price series {normalized_symbol} already exists.")
            return RedirectResponse(url="/price-series", status_code=status.HTTP_303_SEE_OTHER)

        db.add(
            PriceSeries(
                source_id=source.id,
                symbol=normalized_symbol,
                name=normalized_name,
                base_currency=normalized_base,
                quote_currency=normalized_quote,
                frequency=normalized_frequency,
            )
        )
        db.commit()

    _set_flash_message(request, f"Added price series {normalized_symbol}.")
    return RedirectResponse(url="/price-series", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/healthz")
def healthcheck() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "app": settings.app_name,
        }
    )


@router.get("/readyz")
def readiness_check() -> JSONResponse:
    from app.db import SessionLocal

    storage_ok = all(path.exists() for path in settings.storage_dirs)
    db_ok = True
    try:
        with SessionLocal() as db:
            db.execute(select(1))
    except Exception:
        db_ok = False

    status_code = 200 if storage_ok and db_ok else 503
    return JSONResponse(
        {
            "status": "ready" if status_code == 200 else "not-ready",
            "storage_ok": storage_ok,
            "database_ok": db_ok,
            "warnings": [],
        },
        status_code=status_code,
    )
