from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
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
    Source,
    SourceFetchRun,
)
from app.services.horizons import HORIZON_THRESHOLDS
from app.services.realized_outcomes import sync_realized_outcomes
from app.services.report_store import purge_cycle_reports

MACRO_CATEGORY_PRIORITY = {
    "fx": 0,
    "turkey_inflation": 1,
    "domestic_rates": 2,
    "reserves": 3,
    "external_balance": 4,
    "global_dollar": 5,
    "global_rates": 6,
    "growth": 7,
}

PRICE_CATEGORY_PRIORITY = {
    "derived_fx": 0,
    "peer_fx": 1,
    "fx": 2,
    "volatility": 3,
}

VOLATILITY_SYMBOLS = {"VIX", "VIX9D", "VVIX", "VXEEM", "OVX", "GVZ"}
VOLATILITY_DISPLAY_PRIORITY = {
    "VIX": 0,
    "VIX9D": 1,
    "VVIX": 2,
    "VXEEM": 3,
    "OVX": 4,
    "GVZ": 5,
}

READINESS_CATEGORY_LABELS = {
    "domestic_rates": "domestic policy rates",
    "external_balance": "current-account financing",
    "fx": "USD/TRY spot",
    "global_dollar": "broad dollar",
    "global_rates": "global rates",
    "growth": "growth",
    "peer_fx": "peer FX",
    "reserves": "reserves",
    "turkey_inflation": "Turkey inflation",
    "volatility": "volatility",
}

READINESS_LAYERS = (
    ("External Macro", ("global_dollar", "global_rates", "growth")),
    ("Turkey Macro", ("domestic_rates", "turkey_inflation", "reserves", "external_balance")),
    ("Spot Tape", ("fx", "peer_fx")),
    ("Volatility", ("volatility",)),
)

READINESS_AGENT_REQUIREMENTS = {
    "Atlas": ("global_rates", "global_dollar", "growth", "external_balance", "peer_fx"),
    "Bosphorus": (
        "domestic_rates",
        "turkey_inflation",
        "fx",
        "external_balance",
        "growth",
        "peer_fx",
        "reserves",
    ),
    "Flow": ("domestic_rates", "fx", "global_dollar", "peer_fx", "volatility"),
    "Vega": ("domestic_rates", "volatility", "global_dollar", "fx", "peer_fx"),
}

ACTION_PRIORITY_RANK = {"high": 0, "medium": 1, "low": 2}


def _utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


ON_CALL_SPECIALISTS = {
    "Ankara": {
        "role": "Political / Policy Risk Analyst",
        "keywords": {
            "election": "Election timing can abruptly change policy expectations and TRY pricing.",
            "cabinet": "Cabinet change can signal a domestic policy regime shift.",
            "cbrt": "CBRT mentions can be material for domestic policy credibility.",
            "central bank": (
                "Central-bank framing can change intervention and rates expectations."
            ),
            "sanctions": (
                "Sanctions linked to Turkey can impair funding access and policy flexibility."
            ),
            "capital control": "Capital-control risk is a direct TRY convertibility concern.",
            "macroprudential": "Macroprudential changes can distort near-term FX behavior.",
            "banking restriction": (
                "Banking restrictions can materially alter FX access and pricing."
            ),
        },
    },
    "Ledger": {
        "role": "Sovereign Credit / Reserves Analyst",
        "keywords": {
            "reserve": "Reserve adequacy is central to TRY durability and intervention capacity.",
            "intervention": "Intervention mentions point to balance-sheet defense questions.",
            "cds": "CDS stress can signal sovereign funding deterioration.",
            "eurobond": "Eurobond pricing is a direct sovereign funding signal.",
            "rollover": "Rollover risk can drive external financing fragility.",
            "external debt": "External debt pressure can tighten FX funding conditions.",
            "funding": "Funding stress is material for reserve sustainability.",
            "current account": "Current-account financing strain matters for TRY resilience.",
        },
    },
    "Strait": {
        "role": "Global Political Risk Analyst",
        "keywords": {
            "war": "War risk can quickly reprice global and regional FX risk.",
            "geopolitic": "Geopolitical stress can spill into risk appetite and energy pricing.",
            "middle east": (
                "Middle East escalation is directly relevant to Turkey's external balance."
            ),
            "russia": "Russia-related escalation can affect regional trade and sanctions channels.",
            "nato": "NATO tension can become a geopolitical risk transmission channel.",
            "shipping": "Shipping disruption can hit trade and energy costs.",
            "border": "Border instability can change risk sentiment and fiscal burdens.",
            "conflict": "Conflict mentions may imply global political repricing risk.",
        },
    },
    "Meridian": {
        "role": "Global Cycle / Liquidity Strategist",
        "keywords": {
            "fed": "Fed repricing can materially alter dollar pressure on TRY.",
            "ecb": "ECB and European growth signals matter for external conditions.",
            "recession": "Global recession risk can reshape EM flows and carry demand.",
            "liquidity": "Dollar-liquidity conditions are core to EM FX stress.",
            "dxy": "Dollar index moves are a direct global pressure gauge.",
            "yield": "Rates and yields drive the external carry regime.",
            "commodity": "Commodity-cycle turns feed into inflation and external balances.",
            "global growth": "Global growth shocks can spill into funding and exports.",
            "vix": "Volatility spikes can propagate into EM FX risk-off moves.",
        },
    },
}


@dataclass(frozen=True)
class SpecialistActivationMatch:
    specialist_name: str
    specialist_role: str
    trigger_topic: str
    materiality_reason: str
    matched_terms: tuple[str, ...]


def create_assessment_cycle(
    session: Session,
    settings: Settings,
    *,
    primary_horizon: str,
    custom_context: str | None,
    refresh_official_data: bool,
    include_news_chatter: bool,
    label: str | None = None,
) -> AssessmentCycle:
    cycle = AssessmentCycle(
        label=label or f"TRY Risk Cycle {datetime.now():%Y-%m-%d %H:%M}",
        primary_horizon=primary_horizon,
        status="draft",
        user_prompt=(custom_context or "").strip() or None,
        summary="Building Round 0 evidence pack.",
    )
    session.add(cycle)
    session.commit()
    session.refresh(cycle)

    activations = detect_specialist_activations(cycle.user_prompt)
    _sync_specialist_activations(session, cycle.id, activations)

    queue_source_refreshes(
        session,
        refresh_official_data=refresh_official_data,
        include_news_chatter=include_news_chatter,
    )
    session.flush()

    evidence_pack = build_evidence_pack(
        session,
        cycle=cycle,
        refresh_official_data=refresh_official_data,
        include_news_chatter=include_news_chatter,
        activations=activations,
    )
    _write_cycle_artifacts(
        session,
        settings,
        cycle=cycle,
        evidence_pack=evidence_pack,
        refresh_official_data=refresh_official_data,
        include_news_chatter=include_news_chatter,
        activation_names=[activation.specialist_name for activation in activations],
    )
    sync_realized_outcomes(session, cycle_id=cycle.id)
    session.commit()
    session.refresh(cycle)
    return cycle


def create_follow_up_cycle(
    session: Session,
    settings: Settings,
    *,
    source_cycle_id: int,
) -> AssessmentCycle:
    source_cycle = session.get(AssessmentCycle, source_cycle_id)
    if source_cycle is None:
        raise ValueError(f"Assessment cycle {source_cycle_id} was not found.")

    request_flags = _request_flags_from_cycle(source_cycle)
    cycle = create_assessment_cycle(
        session,
        settings,
        primary_horizon=source_cycle.primary_horizon,
        custom_context=source_cycle.user_prompt,
        refresh_official_data=request_flags["refresh_official_data"],
        include_news_chatter=request_flags["include_news_chatter"],
        label=(
            f"TRY Risk Cycle {datetime.now():%Y-%m-%d %H:%M} "
            f"| Follow-up to #{source_cycle.id}"
        ),
    )
    cycle.parent_cycle_id = source_cycle.id
    cycle.summary = (
        f"Follow-up to cycle {source_cycle.id}. "
        f"{cycle.summary or 'Round 0 evidence pack created.'}"
    )
    session.commit()
    session.refresh(cycle)
    return cycle


def rebuild_assessment_cycle(
    session: Session,
    settings: Settings,
    *,
    cycle_id: int,
) -> AssessmentCycle:
    cycle = session.get(AssessmentCycle, cycle_id)
    if cycle is None:
        raise ValueError(f"Assessment cycle {cycle_id} was not found.")

    request_flags = _request_flags_from_cycle(cycle)
    activations = detect_specialist_activations(cycle.user_prompt)
    _sync_specialist_activations(session, cycle.id, activations)
    _invalidate_debate_outputs(session, cycle.id)
    purge_cycle_reports(session, cycle_id=cycle.id, reports_dir=settings.reports_dir)
    cycle.assessment_timestamp = _utc_now_naive()
    cycle.status = "draft"
    session.flush()

    evidence_pack = build_evidence_pack(
        session,
        cycle=cycle,
        refresh_official_data=request_flags["refresh_official_data"],
        include_news_chatter=request_flags["include_news_chatter"],
        activations=activations,
    )
    _write_cycle_artifacts(
        session,
        settings,
        cycle=cycle,
        evidence_pack=evidence_pack,
        refresh_official_data=request_flags["refresh_official_data"],
        include_news_chatter=request_flags["include_news_chatter"],
        activation_names=[activation.specialist_name for activation in activations],
    )
    sync_realized_outcomes(session, cycle_id=cycle.id)
    session.commit()
    session.refresh(cycle)
    return cycle


def detect_specialist_activations(custom_context: str | None) -> list[SpecialistActivationMatch]:
    if not custom_context:
        return []

    normalized = custom_context.casefold()
    matches: list[SpecialistActivationMatch] = []
    for specialist_name, specialist_definition in ON_CALL_SPECIALISTS.items():
        matched_terms = [
            term
            for term in specialist_definition["keywords"]
            if term in normalized
        ]
        if not matched_terms:
            continue
        first_term = matched_terms[0]
        matches.append(
            SpecialistActivationMatch(
                specialist_name=specialist_name,
                specialist_role=specialist_definition["role"],
                trigger_topic=first_term,
                materiality_reason=specialist_definition["keywords"][first_term],
                matched_terms=tuple(matched_terms[:5]),
            )
        )
    return matches


def build_evidence_pack(
    session: Session,
    *,
    cycle: AssessmentCycle,
    refresh_official_data: bool,
    include_news_chatter: bool,
    activations: list[SpecialistActivationMatch],
) -> dict:
    sources = list(
        session.scalars(
            select(Source)
            .where(Source.enabled.is_(True))
            .order_by(Source.trust_tier, Source.name)
        ).all()
    )
    source_entries = [_source_entry(session, source) for source in sources]
    category_counts: dict[str, int] = {}
    for source in sources:
        category_counts[source.category] = category_counts.get(source.category, 0) + 1

    refresh_plan = _queued_refresh_sources(session)
    macro_summary = _macro_summary(session, cycle.assessment_timestamp)
    price_summary = _price_summary(session, cycle.assessment_timestamp)
    news_summary = _news_summary(session, cycle.assessment_timestamp)
    data_completeness = {
        "configured_macro_series": macro_summary["configured_series"],
        "observed_macro_series": macro_summary["series_with_observations"],
        "macro_coverage_ratio": _coverage_ratio(
            macro_summary["series_with_observations"],
            macro_summary["configured_series"],
        ),
        "configured_price_series": price_summary["configured_series"],
        "observed_price_series": price_summary["series_with_observations"],
        "price_coverage_ratio": _coverage_ratio(
            price_summary["series_with_observations"],
            price_summary["configured_series"],
        ),
        "recent_headlines": news_summary["headline_count_14d"],
        "recent_chatter": news_summary["chatter_count_14d"],
        "sources_with_blockers": sum(
            1
            for entry in source_entries
            if entry["last_fetch"]["status"] in {"credentials-required", "config-required", "error"}
        ),
    }
    expert_readiness = _expert_readiness_summary(
        macro_summary=macro_summary,
        price_summary=price_summary,
        news_summary=news_summary,
    )
    action_queue = _action_queue_summary(
        refresh_official_data=refresh_official_data,
        include_news_chatter=include_news_chatter,
        macro_summary=macro_summary,
        price_summary=price_summary,
        news_summary=news_summary,
        refresh_plan=refresh_plan,
        source_entries=source_entries,
        expert_readiness=expert_readiness,
    )
    coverage_notes = _coverage_notes(
        refresh_official_data=refresh_official_data,
        include_news_chatter=include_news_chatter,
        activations=activations,
        macro_summary=macro_summary,
        price_summary=price_summary,
        news_summary=news_summary,
        refresh_plan=refresh_plan,
        source_entries=source_entries,
    )

    return {
        "schema_version": 4,
        "generated_at": _utc_timestamp(),
        "cycle_id": cycle.id,
        "cycle_label": cycle.label,
        "assessment_timestamp": cycle.assessment_timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "primary_horizon": cycle.primary_horizon,
        "risk_thresholds_pct": HORIZON_THRESHOLDS,
        "user_prompt": cycle.user_prompt,
        "request_flags": {
            "refresh_official_data": refresh_official_data,
            "include_news_chatter": include_news_chatter,
        },
        "refresh_plan": {
            "queued_sources": refresh_plan,
            "queued_count": len(refresh_plan),
        },
        "source_summary": {
            "enabled_sources": len(sources),
            "categories": category_counts,
        },
        "data_completeness": data_completeness,
        "macro_summary": macro_summary,
        "price_summary": price_summary,
        "news_summary": news_summary,
        "expert_readiness": expert_readiness,
        "action_queue": action_queue,
        "sources": source_entries,
        "activated_specialists": [
            {
                "specialist_name": activation.specialist_name,
                "specialist_role": activation.specialist_role,
                "trigger_topic": activation.trigger_topic,
                "materiality_reason": activation.materiality_reason,
                "matched_terms": list(activation.matched_terms),
            }
            for activation in activations
        ],
        "coverage_notes": coverage_notes,
    }


def _expert_readiness_summary(
    *,
    macro_summary: dict,
    price_summary: dict,
    news_summary: dict,
) -> dict:
    available = _available_evidence_categories(macro_summary, price_summary)
    layers: list[dict] = []
    for layer_name, required_categories in READINESS_LAYERS:
        present = [
            READINESS_CATEGORY_LABELS[category]
            for category in required_categories
            if category in available
        ]
        missing = [
            READINESS_CATEGORY_LABELS[category]
            for category in required_categories
            if category not in available
        ]
        ratio = len(present) / len(required_categories) if required_categories else 0.0
        layers.append(
            {
                "layer_name": layer_name,
                "status": _readiness_status(ratio),
                "present": present,
                "missing": missing,
                "coverage_ratio": round(ratio, 3),
            }
        )

    agents: list[dict] = []
    for agent_name, required_categories in READINESS_AGENT_REQUIREMENTS.items():
        present = [
            READINESS_CATEGORY_LABELS[category]
            for category in required_categories
            if category in available
        ]
        missing = [
            READINESS_CATEGORY_LABELS[category]
            for category in required_categories
            if category not in available
        ]
        ratio = len(present) / len(required_categories) if required_categories else 0.0
        agents.append(
            {
                "agent_name": agent_name,
                "status": _readiness_status(ratio),
                "coverage_ratio": round(ratio, 3),
                "present_count": len(present),
                "required_count": len(required_categories),
                "missing": missing,
            }
        )

    return {
        "available_categories": sorted(available),
        "layers": layers,
        "agents": agents,
        "headline_count_14d": news_summary.get("headline_count_14d", 0),
        "chatter_count_14d": news_summary.get("chatter_count_14d", 0),
    }


def _action_queue_summary(
    *,
    refresh_official_data: bool,
    include_news_chatter: bool,
    macro_summary: dict,
    price_summary: dict,
    news_summary: dict,
    refresh_plan: list[dict],
    source_entries: list[dict],
    expert_readiness: dict,
) -> list[dict]:
    actions: list[dict] = []
    seen_titles: set[str] = set()
    layers_by_name = {
        item["layer_name"]: item for item in expert_readiness.get("layers", [])
    }

    def add_action(
        *,
        priority: str,
        title: str,
        detail: str,
        target_area: str,
    ) -> None:
        if title in seen_titles:
            return
        seen_titles.add(title)
        actions.append(
            {
                "priority": priority,
                "title": title,
                "detail": detail,
                "target_area": target_area,
                "priority_rank": ACTION_PRIORITY_RANK.get(priority, 99),
            }
        )

    if refresh_plan:
        queued_names = [entry["source_name"] for entry in refresh_plan[:4]]
        suffix = ""
        if len(refresh_plan) > 4:
            suffix = f", and {len(refresh_plan) - 4} more"
        add_action(
            priority="high",
            title="Run queued source refreshes",
            detail=(
                f"{len(refresh_plan)} source refreshes are still queued "
                f"({', '.join(queued_names)}{suffix}). Refresh the evidence snapshot "
                "before trusting the next debate round."
            ),
            target_area="Sources",
        )

    credential_blockers = [
        entry["name"]
        for entry in source_entries
        if entry["last_fetch"]["status"] == "credentials-required"
    ]
    if credential_blockers:
        add_action(
            priority="high",
            title="Provide blocked source credentials",
            detail=(
                "These sources cannot refresh until credentials are supplied: "
                + ", ".join(credential_blockers)
                + "."
            ),
            target_area="Sources",
        )

    config_blockers = [
        entry["name"]
        for entry in source_entries
        if entry["last_fetch"]["status"] == "config-required"
    ]
    if config_blockers:
        add_action(
            priority="medium",
            title="Complete source configuration",
            detail=(
                "These sources still need local configuration before they can ingest: "
                + ", ".join(config_blockers)
                + "."
            ),
            target_area="Sources",
        )

    error_sources = [
        entry["name"]
        for entry in source_entries
        if entry["last_fetch"]["status"] == "error"
    ]
    if error_sources:
        add_action(
            priority="high",
            title="Review source fetch failures",
            detail=(
                "The latest refresh failed for: "
                + ", ".join(error_sources)
                + ". Check the source log and rerun the affected providers."
            ),
            target_area="Sources",
        )

    turkey_layer = layers_by_name.get("Turkey Macro")
    if turkey_layer and turkey_layer["status"] != "ready":
        missing = ", ".join(turkey_layer["missing"]) or "Turkey macro coverage"
        add_action(
            priority="high",
            title="Complete Turkey macro coverage",
            detail=(
                f"Missing or thin signals: {missing}. Refresh the public CBRT policy-rate and "
                "reserve sources, or add confirmed domestic_rates and reserves series, so "
                "Bosphorus and Ledger stop working off thin domestic evidence."
            ),
            target_area="Macro Series",
        )

    external_layer = layers_by_name.get("External Macro")
    if external_layer and external_layer["status"] != "ready":
        missing = ", ".join(external_layer["missing"]) or "External macro coverage"
        add_action(
            priority="medium",
            title="Strengthen external macro coverage",
            detail=(
                f"Still missing: {missing}. Fill those global series so Atlas has a cleaner "
                "dollar, rates, and external-balance read."
            ),
            target_area="Macro Series",
        )

    spot_layer = layers_by_name.get("Spot Tape")
    if spot_layer and spot_layer["status"] != "ready":
        missing = ", ".join(spot_layer["missing"]) or "Spot tape coverage"
        add_action(
            priority="medium",
            title="Strengthen spot tape coverage",
            detail=(
                f"Still missing: {missing}. Make sure USD/TRY and the peer FX basket are "
                "refreshing so Flow can judge whether TRY is moving idiosyncratically or "
                "with peers."
            ),
            target_area="Price Series",
        )

    volatility_layer = layers_by_name.get("Volatility")
    if volatility_layer and volatility_layer["status"] != "ready":
        missing = ", ".join(volatility_layer["missing"]) or "Volatility coverage"
        add_action(
            priority="medium",
            title="Strengthen volatility coverage",
            detail=(
                f"Still missing: {missing}. Add or refresh public vol proxies so Vega can "
                "separate TRY-specific fear from a broader cross-asset volatility shock."
            ),
            target_area="Price Series",
        )

    if include_news_chatter:
        if (
            news_summary.get("headline_count_14d", 0) == 0
            and news_summary.get("chatter_count_14d", 0) == 0
        ):
            add_action(
                priority="medium",
                title="Refresh news and chatter evidence",
                detail=(
                    "No recent headlines or chatter landed inside the 14-day window. "
                    "Refresh those sources before leaning on event-risk commentary."
                ),
                target_area="Sources",
            )
    elif not refresh_official_data:
        add_action(
            priority="low",
            title="Enable news and chatter on event-heavy cycles",
            detail=(
                "This cycle skipped headlines and chatter. Turn that scan on when policy "
                "headlines or geopolitical risk are a meaningful part of the thesis."
            ),
            target_area="New Assessment",
        )

    if not actions and (
        macro_summary.get("series_with_observations", 0) == 0
        or price_summary.get("series_with_observations", 0) == 0
    ):
        add_action(
            priority="medium",
            title="Load missing local history",
            detail=(
                "Some local macro or market history is still empty. Refresh the affected "
                "sources before treating the evidence pack as complete."
            ),
            target_area="Sources",
        )

    actions.sort(key=lambda item: item["priority_rank"])
    return [
        {
            "priority": item["priority"],
            "title": item["title"],
            "detail": item["detail"],
            "target_area": item["target_area"],
        }
        for item in actions[:6]
    ]


def _available_evidence_categories(macro_summary: dict, price_summary: dict) -> set[str]:
    observed_categories = macro_summary.get("observed_categories")
    if observed_categories:
        available = set(observed_categories)
    else:
        available = {
            item["category"]
            for item in macro_summary.get("key_observations", [])
            if item.get("category")
        }

    for pair in price_summary.get("derived_pairs", []):
        category = pair.get("category")
        if category == "derived_fx":
            available.add("fx")
        elif category == "peer_fx":
            available.add("peer_fx")

    for item in price_summary.get("series", []):
        category = item.get("category")
        if category == "fx":
            available.add("fx")
        elif category == "volatility":
            available.add("volatility")

    market_regime = price_summary.get("market_regime") or {}
    if market_regime.get("peer_count", 0) > 0:
        available.add("peer_fx")
    if market_regime.get("vix_close") is not None:
        available.add("volatility")
    return available


def _readiness_status(ratio: float) -> str:
    if ratio >= 0.8:
        return "ready"
    if ratio >= 0.4:
        return "partial"
    return "thin"


def load_evidence_pack(cycle: AssessmentCycle) -> dict | None:
    if not cycle.evidence_pack_path:
        return None

    evidence_path = Path(cycle.evidence_pack_path)
    if not evidence_path.exists():
        return None
    return json.loads(evidence_path.read_text(encoding="utf-8"))


def summarize_cycle(
    *,
    refresh_official_data: bool,
    include_news_chatter: bool,
    activation_names: list[str],
    enabled_sources: int,
    macro_series_with_observations: int,
    macro_series_total: int,
    price_series_with_observations: int,
    price_series_total: int,
    recent_headlines: int,
) -> str:
    summary_bits = [f"Round 0 with {enabled_sources} enabled sources."]
    summary_bits.append(
        f"Macro coverage {macro_series_with_observations}/{macro_series_total} series."
    )
    summary_bits.append(
        f"Price coverage {price_series_with_observations}/{price_series_total} series."
    )
    if include_news_chatter:
        summary_bits.append(f"Recent headlines: {recent_headlines}.")
    if refresh_official_data:
        summary_bits.append("Official refresh requested.")
    if activation_names:
        summary_bits.append("On-call specialists: " + ", ".join(activation_names) + ".")
    else:
        summary_bits.append("No on-call specialists auto-activated.")
    return " ".join(summary_bits)


def queue_source_refreshes(
    session: Session,
    *,
    refresh_official_data: bool,
    include_news_chatter: bool,
) -> list[dict]:
    category_filters: set[str] = set()
    if refresh_official_data:
        category_filters.update({"official_macro", "market_prices"})
    if include_news_chatter:
        category_filters.update({"news", "social"})
    if not category_filters:
        return []

    sources = list(
        session.scalars(
            select(Source)
            .where(Source.enabled.is_(True), Source.category.in_(sorted(category_filters)))
            .order_by(Source.trust_tier, Source.name)
        ).all()
    )
    if not sources:
        return []

    queued_at = _utc_now_naive()
    session.add_all(
        SourceFetchRun(
            source_id=source.id,
            started_at=queued_at,
            status="queued",
            items_ingested=0,
        )
        for source in sources
    )

    return [
        {
            "source_name": source.name,
            "source_slug": source.slug,
            "category": source.category,
            "status": "queued",
        }
        for source in sources
    ]


def write_evidence_pack(settings: Settings, cycle_id: int, evidence_pack: dict) -> Path:
    evidence_path = settings.evidence_dir / f"cycle-{cycle_id:05d}.json"
    evidence_path.write_text(
        json.dumps(evidence_pack, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return evidence_path


def _request_flags_from_cycle(cycle: AssessmentCycle) -> dict[str, bool]:
    existing_pack = load_evidence_pack(cycle) or {}
    request_flags = existing_pack.get("request_flags", {})
    return {
        "refresh_official_data": bool(request_flags.get("refresh_official_data", False)),
        "include_news_chatter": bool(request_flags.get("include_news_chatter", False)),
    }


def _write_cycle_artifacts(
    session: Session,
    settings: Settings,
    *,
    cycle: AssessmentCycle,
    evidence_pack: dict,
    refresh_official_data: bool,
    include_news_chatter: bool,
    activation_names: list[str],
) -> None:
    evidence_pack_path = write_evidence_pack(settings, cycle.id, evidence_pack)
    cycle.evidence_pack_path = str(evidence_pack_path)
    cycle.summary = summarize_cycle(
        refresh_official_data=refresh_official_data,
        include_news_chatter=include_news_chatter,
        activation_names=activation_names,
        enabled_sources=evidence_pack["source_summary"]["enabled_sources"],
        macro_series_with_observations=evidence_pack["macro_summary"]["series_with_observations"],
        macro_series_total=evidence_pack["macro_summary"]["configured_series"],
        price_series_with_observations=evidence_pack["price_summary"]["series_with_observations"],
        price_series_total=evidence_pack["price_summary"]["configured_series"],
        recent_headlines=evidence_pack["news_summary"]["headline_count_14d"],
    )
    session.commit()
    session.refresh(cycle)


def _sync_specialist_activations(
    session: Session,
    cycle_id: int,
    activations: list[SpecialistActivationMatch],
) -> None:
    existing = list(
        session.scalars(
            select(CycleSpecialistActivation).where(
                CycleSpecialistActivation.cycle_id == cycle_id
            )
        ).all()
    )
    for activation in existing:
        session.delete(activation)
    if activations:
        session.add_all(
            CycleSpecialistActivation(
                cycle_id=cycle_id,
                specialist_name=activation.specialist_name,
                trigger_topic=activation.trigger_topic,
                materiality_reason=activation.materiality_reason,
            )
            for activation in activations
        )


def _invalidate_debate_outputs(session: Session, cycle_id: int) -> None:
    session.execute(delete(AgentRoundOutput).where(AgentRoundOutput.cycle_id == cycle_id))
    session.execute(delete(HouseView).where(HouseView.cycle_id == cycle_id))


def _queued_refresh_sources(session: Session) -> list[dict]:
    queued_rows = session.execute(
        select(
            Source.name,
            Source.slug,
            Source.category,
            func.count(SourceFetchRun.id),
        )
        .join(SourceFetchRun, SourceFetchRun.source_id == Source.id)
        .where(SourceFetchRun.status == "queued")
        .group_by(Source.id, Source.name, Source.slug, Source.category)
        .order_by(Source.trust_tier.asc(), Source.name.asc())
    ).all()
    return [
        {
            "source_name": source_name,
            "source_slug": source_slug,
            "category": category,
            "status": "queued",
            "queued_runs": queued_runs,
        }
        for source_name, source_slug, category, queued_runs in queued_rows
    ]


def _macro_summary(session: Session, assessment_timestamp: datetime | None) -> dict:
    anchor = assessment_timestamp or _utc_now_naive()
    source_names = {
        source_id: source_name
        for source_id, source_name in session.execute(select(Source.id, Source.name)).all()
    }
    source_slugs = {
        source_id: source_slug
        for source_id, source_slug in session.execute(select(Source.id, Source.slug)).all()
    }
    series_list = list(
        session.scalars(
            select(MacroSeries).order_by(MacroSeries.category.asc(), MacroSeries.name.asc())
        ).all()
    )
    observations: list[dict] = []
    missing_series: list[dict] = []
    latest_date: datetime | None = None

    for series in series_list:
        latest_points_query = (
            select(MacroObservation)
            .where(
                MacroObservation.series_id == series.id,
                MacroObservation.observation_date <= anchor,
            )
            .order_by(
                MacroObservation.observation_date.desc(),
                MacroObservation.id.desc(),
            )
            .limit(2)
        )
        source_slug = source_slugs.get(series.source_id)
        if source_slug == "imf-data":
            latest_points_query = latest_points_query.where(
                MacroObservation.fetched_at <= anchor
            )
        else:
            latest_points_query = latest_points_query.where(
                or_(
                    MacroObservation.release_date.is_(None),
                    MacroObservation.release_date <= anchor,
                )
            )
        latest_points = list(
            session.scalars(latest_points_query).all()
        )
        if not latest_points:
            missing_series.append(
                {
                    "code": series.code,
                    "name": series.name,
                    "source_name": source_names.get(series.source_id, "Unknown source"),
                    "category": series.category,
                }
            )
            continue

        latest = latest_points[0]
        previous = latest_points[1] if len(latest_points) > 1 else None
        latest_date = (
            latest.observation_date
            if latest_date is None or latest.observation_date > latest_date
            else latest_date
        )
        observations.append(
            {
                "code": series.code,
                "name": series.name,
                "source_name": source_names.get(series.source_id, "Unknown source"),
                "category": series.category,
                "unit": series.unit,
                "frequency": series.frequency,
                "observation_date": latest.observation_date.strftime("%Y-%m-%d"),
                "value": latest.value,
                "previous_value": previous.value if previous else None,
                "delta": (
                    latest.value - previous.value
                    if previous and latest.value is not None and previous.value is not None
                    else None
                ),
                "trend": _trend_label(
                    latest.value,
                    previous.value if previous else None,
                ),
            }
        )

    observations.sort(
        key=lambda item: (
            MACRO_CATEGORY_PRIORITY.get(item["category"], 99),
            item["name"],
        )
    )
    missing_series.sort(
        key=lambda item: (
            MACRO_CATEGORY_PRIORITY.get(item["category"], 99),
            item["name"],
        )
    )

    return {
        "configured_series": len(series_list),
        "series_with_observations": len(observations),
        "as_of": anchor.strftime("%Y-%m-%d %H:%M:%S"),
        "latest_observation_date": (
            latest_date.strftime("%Y-%m-%d") if latest_date else None
        ),
        "observed_categories": sorted(
            {
                item["category"]
                for item in observations
                if item.get("category")
            }
        ),
        "key_observations": observations[:8],
        "rates_regime": _rates_regime_summary(observations),
        "turkey_policy_reserves": _turkey_policy_reserves_summary(observations),
        "missing_series": missing_series[:6],
    }


def _rates_regime_summary(observations: list[dict]) -> dict:
    by_code = {item["code"]: item for item in observations}
    fedfunds = by_code.get("FEDFUNDS")
    dgs2 = by_code.get("DGS2")
    dgs10 = by_code.get("DGS10")
    dollar = by_code.get("DTWEXBGS")
    turkey_cpi = by_code.get("PCPIPCH/TUR")

    score = (
        _macro_trend_score(dollar, weight=1.2)
        + _macro_trend_score(dgs2, weight=1.0)
        + _macro_trend_score(dgs10, weight=0.8)
        + _macro_trend_score(fedfunds, weight=0.6)
    )

    if score >= 1.8:
        regime_label = "Dollar and rates headwind"
        external_signal = "external carry backdrop is tightening"
    elif score <= -1.8:
        regime_label = "Dollar and rates relief"
        external_signal = "external carry backdrop is easing"
    else:
        regime_label = "Mixed rates backdrop"
        external_signal = "dollar and rates are mixed"

    carry_signal = "domestic carry signal is incomplete"
    if turkey_cpi:
        if turkey_cpi.get("trend") == "down":
            carry_signal = "domestic disinflation is improving carry optics"
        elif turkey_cpi.get("trend") == "up":
            carry_signal = "domestic inflation is eroding carry optics"
        else:
            carry_signal = "domestic inflation is broadly steady"

    summary_parts: list[str] = []
    if dollar and dollar.get("value") is not None:
        summary_parts.append(
            "Broad dollar index at "
            f"{dollar['value']:.3f} ({_signed_delta(dollar.get('delta'))})"
        )
    if dgs2 and dgs2.get("value") is not None:
        summary_parts.append(
            f"US 2Y at {dgs2['value']:.3f}% ({_signed_delta(dgs2.get('delta'))} pts)"
        )
    if dgs10 and dgs10.get("value") is not None:
        summary_parts.append(
            f"US 10Y at {dgs10['value']:.3f}% ({_signed_delta(dgs10.get('delta'))} pts)"
        )
    if fedfunds and fedfunds.get("value") is not None:
        summary_parts.append(
            "Fed funds at "
            f"{fedfunds['value']:.3f}% ({_signed_delta(fedfunds.get('delta'))} pts)"
        )
    summary_parts.append(carry_signal)

    return {
        "ready": bool(dollar or dgs2 or dgs10 or fedfunds or turkey_cpi),
        "regime_label": regime_label,
        "external_signal": external_signal,
        "carry_signal": carry_signal,
        "summary": ". ".join(summary_parts) + ("." if summary_parts else ""),
        "score": round(score, 3),
        "broad_dollar_value": dollar.get("value") if dollar else None,
        "broad_dollar_delta": dollar.get("delta") if dollar else None,
        "us2y_value": dgs2.get("value") if dgs2 else None,
        "us2y_delta": dgs2.get("delta") if dgs2 else None,
        "us10y_value": dgs10.get("value") if dgs10 else None,
        "us10y_delta": dgs10.get("delta") if dgs10 else None,
        "fedfunds_value": fedfunds.get("value") if fedfunds else None,
        "fedfunds_delta": fedfunds.get("delta") if fedfunds else None,
        "turkey_cpi_value": turkey_cpi.get("value") if turkey_cpi else None,
        "turkey_cpi_delta": turkey_cpi.get("delta") if turkey_cpi else None,
    }


def _macro_trend_score(observation: dict | None, *, weight: float) -> float:
    if not observation:
        return 0.0
    trend = observation.get("trend")
    if trend == "up":
        return weight
    if trend == "down":
        return -weight
    return 0.0


def _turkey_policy_reserves_summary(observations: list[dict]) -> dict:
    domestic_rates = [
        item for item in observations if item.get("category") == "domestic_rates"
    ]
    reserves = [item for item in observations if item.get("category") == "reserves"]
    inflation = next(
        (item for item in observations if item.get("category") == "turkey_inflation"),
        None,
    )

    score = (
        _risk_reversing_macro_score(domestic_rates[:2], weight=1.0)
        + _risk_reversing_macro_score(reserves[:2], weight=1.2)
        + _macro_trend_score(inflation, weight=0.8)
    )

    if score >= 1.5:
        regime_label = "Domestic fragility rising"
        relative_signal = "policy and reserve signals are adding TRY risk"
    elif score <= -1.5:
        regime_label = "Domestic policy support improving"
        relative_signal = "policy and reserve signals are helping TRY resilience"
    else:
        regime_label = "Mixed domestic policy backdrop"
        relative_signal = "policy and reserve signals are mixed"

    policy_signal = _domestic_policy_signal(domestic_rates, inflation)
    reserve_signal = _reserve_signal(reserves)

    primary_domestic_rate = domestic_rates[0] if domestic_rates else None
    primary_reserve = reserves[0] if reserves else None

    summary_parts: list[str] = []
    if primary_domestic_rate and primary_domestic_rate.get("value") is not None:
        summary_parts.append(
            f"{primary_domestic_rate['name']} at "
            f"{primary_domestic_rate['value']:.3f} "
            f"{primary_domestic_rate.get('unit') or ''}".strip()
            + f" ({_signed_delta(primary_domestic_rate.get('delta'))})"
        )
    if primary_reserve and primary_reserve.get("value") is not None:
        reserve_unit = primary_reserve.get("unit") or ""
        summary_parts.append(
            f"{primary_reserve['name']} at {primary_reserve['value']:.3f} "
            f"{reserve_unit}".strip()
            + f" ({_signed_delta(primary_reserve.get('delta'))})"
        )
    summary_parts.append(policy_signal)
    summary_parts.append(reserve_signal)

    return {
        "ready": bool(domestic_rates or reserves or inflation),
        "regime_label": regime_label,
        "relative_signal": relative_signal,
        "policy_signal": policy_signal,
        "reserve_signal": reserve_signal,
        "summary": ". ".join(summary_parts) + ("." if summary_parts else ""),
        "score": round(score, 3),
        "primary_domestic_rate_name": (
            primary_domestic_rate.get("name") if primary_domestic_rate else None
        ),
        "primary_domestic_rate_value": (
            primary_domestic_rate.get("value") if primary_domestic_rate else None
        ),
        "primary_domestic_rate_unit": (
            primary_domestic_rate.get("unit") if primary_domestic_rate else None
        ),
        "primary_domestic_rate_delta": (
            primary_domestic_rate.get("delta") if primary_domestic_rate else None
        ),
        "primary_reserve_name": primary_reserve.get("name") if primary_reserve else None,
        "primary_reserve_value": primary_reserve.get("value") if primary_reserve else None,
        "primary_reserve_unit": primary_reserve.get("unit") if primary_reserve else None,
        "primary_reserve_delta": primary_reserve.get("delta") if primary_reserve else None,
    }


def _risk_reversing_macro_score(observations: list[dict], *, weight: float) -> float:
    directional_reads: list[float] = []
    for observation in observations:
        trend = observation.get("trend")
        if trend == "up":
            directional_reads.append(-1.0)
        elif trend == "down":
            directional_reads.append(1.0)
    if not directional_reads:
        return 0.0
    return (sum(directional_reads) / len(directional_reads)) * weight


def _domestic_policy_signal(domestic_rates: list[dict], inflation: dict | None) -> str:
    rate_trend = domestic_rates[0].get("trend") if domestic_rates else None
    inflation_trend = inflation.get("trend") if inflation else None
    if rate_trend == "up" and inflation_trend == "down":
        return "domestic policy stance is tightening into disinflation"
    if rate_trend == "down" and inflation_trend == "up":
        return "domestic policy stance is easing against inflation"
    if rate_trend == "up":
        return "domestic policy stance is tightening"
    if rate_trend == "down":
        return "domestic policy stance is easing"
    if inflation_trend == "down":
        return "disinflation is improving domestic carry optics"
    if inflation_trend == "up":
        return "inflation is eroding domestic carry optics"
    return "domestic policy stance is still incomplete"


def _reserve_signal(reserves: list[dict]) -> str:
    if not reserves:
        return "reserve signal is still incomplete"
    trend_set = {
        reserve.get("trend")
        for reserve in reserves
        if reserve.get("trend") in {"up", "down"}
    }
    if trend_set == {"up"}:
        return "reserve buffer is rebuilding"
    if trend_set == {"down"}:
        return "reserve buffer is thinning"
    if trend_set == {"up", "down"}:
        return "reserve signals are split across the balance sheet"
    return "reserve buffer is broadly steady"


def _price_summary(session: Session, assessment_timestamp: datetime | None) -> dict:
    anchor = assessment_timestamp or _utc_now_naive()
    source_names = {
        source_id: source_name
        for source_id, source_name in session.execute(select(Source.id, Source.name)).all()
    }
    series_list = list(
        session.scalars(
            select(PriceSeries).order_by(PriceSeries.name.asc(), PriceSeries.id.asc())
        ).all()
    )
    series_snapshots: list[dict] = []
    missing_series: list[dict] = []
    observations_by_symbol: dict[str, list[PriceObservation]] = {}

    for series in series_list:
        latest_points = list(
            session.scalars(
                select(PriceObservation)
                .where(
                    PriceObservation.series_id == series.id,
                    PriceObservation.observed_at <= anchor,
                )
                .order_by(
                    PriceObservation.observed_at.desc(),
                    PriceObservation.id.desc(),
                )
                .limit(6)
            ).all()
        )
        observations_by_symbol[series.symbol] = latest_points
        if not latest_points:
            missing_series.append(
                {
                    "symbol": series.symbol,
                    "name": series.name,
                    "source_name": source_names.get(series.source_id, "Unknown source"),
                    "category": _price_category(series),
                }
            )
            continue

        latest = latest_points[0]
        previous = latest_points[1] if len(latest_points) > 1 else None
        series_snapshots.append(
            {
                "symbol": series.symbol,
                "name": series.name,
                "source_name": source_names.get(series.source_id, "Unknown source"),
                "category": _price_category(series),
                "observed_at": latest.observed_at.strftime("%Y-%m-%d"),
                "close_value": latest.close_value,
                "previous_close": previous.close_value if previous else None,
                "change_pct": _percent_change(
                    latest.close_value,
                    previous.close_value if previous else None,
                ),
                "trend": _trend_label(
                    latest.close_value,
                    previous.close_value if previous else None,
                ),
                "base_currency": series.base_currency,
                "quote_currency": series.quote_currency,
            }
        )

    derived_pairs = _derived_pair_summary(series_list, observations_by_symbol)
    derived_pairs.sort(key=_price_snapshot_sort_key)
    market_regime = _market_regime_summary(series_snapshots, derived_pairs)

    series_snapshots.sort(key=_price_snapshot_sort_key)
    missing_series.sort(key=_price_snapshot_sort_key)

    observed_series_count = len(series_snapshots)
    return {
        "configured_series": len(series_list),
        "series_with_observations": observed_series_count,
        "as_of": anchor.strftime("%Y-%m-%d %H:%M:%S"),
        "series": series_snapshots[:12],
        "derived_pairs": derived_pairs[:6],
        "market_regime": market_regime,
        "missing_series": missing_series[:8],
    }


def _news_summary(session: Session, assessment_timestamp: datetime | None) -> dict:
    anchor = assessment_timestamp or _utc_now_naive()
    since = anchor - timedelta(days=14)
    headline_count = session.scalar(
        select(func.count()).select_from(Headline).where(
            Headline.published_at >= since,
            Headline.published_at <= anchor,
        )
    ) or 0
    chatter_count = session.scalar(
        select(func.count()).select_from(ChatterItem).where(
            ChatterItem.posted_at >= since,
            ChatterItem.posted_at <= anchor,
        )
    ) or 0

    recent_headlines = session.execute(
        select(Headline, Source.name)
        .join(Source, Headline.source_id == Source.id, isouter=True)
        .where(Headline.published_at >= since, Headline.published_at <= anchor)
        .order_by(Headline.published_at.desc(), Headline.id.desc())
        .limit(5)
    ).all()
    recent_chatter = session.execute(
        select(ChatterItem, Source.name)
        .join(Source, ChatterItem.source_id == Source.id, isouter=True)
        .where(ChatterItem.posted_at >= since, ChatterItem.posted_at <= anchor)
        .order_by(ChatterItem.posted_at.desc(), ChatterItem.id.desc())
        .limit(5)
    ).all()

    return {
        "as_of": anchor.strftime("%Y-%m-%d %H:%M:%S"),
        "headline_count_14d": headline_count,
        "chatter_count_14d": chatter_count,
        "recent_headlines": [
            {
                "title": headline.title,
                "source_name": source_name or "Unknown source",
                "published_at": headline.published_at.strftime("%Y-%m-%d %H:%M"),
                "url": headline.url,
            }
            for headline, source_name in recent_headlines
        ],
        "recent_chatter": [
            {
                "content": item.content,
                "source_name": source_name or "Unknown source",
                "posted_at": item.posted_at.strftime("%Y-%m-%d %H:%M"),
                "author": item.author,
                "url": item.url,
            }
            for item, source_name in recent_chatter
        ],
    }


def _coverage_notes(
    *,
    refresh_official_data: bool,
    include_news_chatter: bool,
    activations: list[SpecialistActivationMatch],
    macro_summary: dict,
    price_summary: dict,
    news_summary: dict,
    refresh_plan: list[dict],
    source_entries: list[dict],
) -> list[str]:
    notes: list[str] = []
    if refresh_official_data:
        notes.append("Official-source refresh requested for this cycle.")
    else:
        notes.append(
            "Official-source refresh deferred; existing local official data drives Round 0."
        )
    if include_news_chatter:
        notes.append(
            "News and chatter scan requested, but social evidence stays below official and "
            "market-implied signals."
        )
    else:
        notes.append("News and chatter scan skipped for this cycle.")
    if macro_summary["series_with_observations"] == 0:
        notes.append("No official macro observations are stored yet.")
    else:
        notes.append(
            f"Official macro coverage is {macro_summary['series_with_observations']}/"
            f"{macro_summary['configured_series']} series."
        )
        rates_regime = macro_summary.get("rates_regime") or {}
        if rates_regime.get("ready"):
            notes.append(
                f"Rates backdrop: {rates_regime['regime_label']}. {rates_regime['summary']}"
            )
        turkey_policy = macro_summary.get("turkey_policy_reserves") or {}
        if turkey_policy.get("ready"):
            notes.append(
                "Turkey policy/reserves: "
                f"{turkey_policy['regime_label']}. {turkey_policy['summary']}"
            )
        else:
            notes.append(
                "Turkey policy/reserve layer is incomplete; refresh the public CBRT policy-rate "
                "and reserve sources or add stronger domestic_rates and reserves coverage."
            )
    if price_summary["series_with_observations"] == 0:
        notes.append("No market price observations are stored yet.")
    else:
        notes.append(
            f"Market-price coverage is {price_summary['series_with_observations']}/"
            f"{price_summary['configured_series']} series."
        )
        market_regime = price_summary.get("market_regime") or {}
        if market_regime.get("ready"):
            notes.append(
                f"Market regime: {market_regime['regime_label']}. {market_regime['summary']}"
            )
    if include_news_chatter and news_summary["headline_count_14d"] == 0:
        notes.append("No recent headlines were found inside the 14-day evidence window.")
    if refresh_plan:
        notes.append(
            "Queued refreshes are still pending for: "
            + ", ".join(entry["source_name"] for entry in refresh_plan)
            + "."
        )
    blocked_sources = [
        entry["name"]
        for entry in source_entries
        if entry["last_fetch"]["status"] in {"credentials-required", "config-required", "error"}
    ]
    if blocked_sources:
        notes.append(
            "Some sources are still blocked or incomplete: "
            + ", ".join(blocked_sources)
            + "."
        )
    if activations:
        notes.append(
            "Direct specialist mentions detected: "
            + ", ".join(activation.specialist_name for activation in activations)
            + "."
        )
    else:
        notes.append("No direct specialist mentions detected in the user context.")
    return notes


def _coverage_ratio(observed: int, configured: int) -> float:
    if configured == 0:
        return 0.0
    return round(observed / configured, 3)


def _percent_change(current: float | None, previous: float | None) -> float | None:
    if current is None or previous in (None, 0):
        return None
    return round(((current - previous) / previous) * 100, 3)


def _trend_label(current: float | None, previous: float | None) -> str:
    if current is None or previous is None:
        return "n/a"
    if current > previous:
        return "up"
    if current < previous:
        return "down"
    return "flat"


def _price_category(series: PriceSeries) -> str:
    if series.symbol in VOLATILITY_SYMBOLS:
        return "volatility"
    return "fx"


def _price_snapshot_sort_key(item: dict) -> tuple[int, int, str]:
    return (
        PRICE_CATEGORY_PRIORITY.get(item.get("category"), 99),
        VOLATILITY_DISPLAY_PRIORITY.get(item.get("symbol"), 99),
        item.get("name") or "",
    )


def _derived_pair_summary(
    series_list: list[PriceSeries],
    observations_by_symbol: dict[str, list[PriceObservation]],
) -> list[dict]:
    eur_usd = observations_by_symbol.get("D.USD.EUR.SP00.A", [])
    if len(eur_usd) < 2:
        return []

    latest_usd = eur_usd[0]
    previous_usd = eur_usd[1]
    if latest_usd.close_value in (None, 0):
        return []

    derived_pairs: list[dict] = []
    for series in series_list:
        if series.symbol == "D.USD.EUR.SP00.A":
            continue
        if series.base_currency != "EUR" or not series.quote_currency:
            continue

        series_points = observations_by_symbol.get(series.symbol, [])
        if len(series_points) < 2:
            continue

        latest_quote = series_points[0]
        previous_quote = series_points[1]
        if latest_quote.close_value in (None, 0):
            continue

        previous_close = None
        if (
            previous_quote.close_value not in (None, 0)
            and previous_usd.close_value not in (None, 0)
        ):
            previous_close = previous_quote.close_value / previous_usd.close_value

        close_value = latest_quote.close_value / latest_usd.close_value
        quote_currency = series.quote_currency
        derived_pairs.append(
            {
                "symbol": f"USD{quote_currency}_DERIVED",
                "name": f"USD/{quote_currency} Derived From ECB EUR Crosses",
                "source_name": "ECB EXR",
                "category": "derived_fx" if quote_currency == "TRY" else "peer_fx",
                "observed_at": latest_quote.observed_at.strftime("%Y-%m-%d"),
                "close_value": round(close_value, 6),
                "previous_close": round(previous_close, 6) if previous_close is not None else None,
                "change_pct": _percent_change(close_value, previous_close),
                "trend": _trend_label(close_value, previous_close),
                "base_currency": "USD",
                "quote_currency": quote_currency,
            }
        )
    return derived_pairs


def _market_regime_summary(series_snapshots: list[dict], derived_pairs: list[dict]) -> dict:
    usd_try = next(
        (item for item in derived_pairs if item.get("symbol") == "USDTRY_DERIVED"),
        None,
    )
    peer_pairs = [
        item
        for item in derived_pairs
        if item.get("category") == "peer_fx" and item.get("change_pct") is not None
    ]
    volatility_snapshot = _volatility_snapshot(series_snapshots)

    try_change_pct = usd_try.get("change_pct") if usd_try else None
    peer_average_change_pct = (
        round(sum(item["change_pct"] for item in peer_pairs) / len(peer_pairs), 3)
        if peer_pairs
        else None
    )
    try_vs_peer_gap_pct = (
        round(try_change_pct - peer_average_change_pct, 3)
        if try_change_pct is not None and peer_average_change_pct is not None
        else None
    )
    peer_breadth_up = sum(1 for item in peer_pairs if item["change_pct"] > 0)
    peer_breadth_down = sum(1 for item in peer_pairs if item["change_pct"] < 0)

    regime_label = "No market regime read yet"
    relative_signal = "insufficient data"
    if try_vs_peer_gap_pct is not None and try_change_pct is not None:
        if try_vs_peer_gap_pct >= 0.75 and try_change_pct > 0:
            regime_label = "TRY-specific stress"
            relative_signal = "TRY is weakening faster than peers"
        elif try_vs_peer_gap_pct <= -0.75 and try_change_pct < 0:
            regime_label = "TRY outperforming peers"
            relative_signal = "TRY is stronger than the peer basket"
        elif peer_average_change_pct is not None and peer_average_change_pct >= 0.25:
            regime_label = "Broad EM/CEE pressure"
            relative_signal = "peer FX is weakening alongside TRY"
        elif peer_average_change_pct is not None and peer_average_change_pct <= -0.25:
            regime_label = "Broad EM/CEE relief"
            relative_signal = "peer FX is strengthening alongside TRY"
        else:
            regime_label = "Mixed cross-market regime"
            relative_signal = "TRY and peers are not sending a one-way signal"
    elif try_change_pct is not None:
        regime_label = "TRY move without peer confirmation"
        relative_signal = "USD/TRY moved but the peer basket is incomplete"

    summary_parts: list[str] = []
    if try_change_pct is not None:
        summary_parts.append(f"USD/TRY moved {_signed_pct(try_change_pct)}")
    if peer_average_change_pct is not None:
        summary_parts.append(
            f"peer basket average moved {_signed_pct(peer_average_change_pct)}"
        )
    if try_vs_peer_gap_pct is not None:
        summary_parts.append(f"TRY vs peers gap {_signed_pct(try_vs_peer_gap_pct)} pts")
    if peer_pairs:
        summary_parts.append(
            f"breadth {peer_breadth_up} up / {peer_breadth_down} down across peers"
        )
    summary_parts.extend(volatility_snapshot["summary_parts"])

    return {
        "ready": bool(summary_parts),
        "regime_label": regime_label,
        "relative_signal": relative_signal,
        "summary": ". ".join(summary_parts) + ("." if summary_parts else ""),
        "try_change_pct": try_change_pct,
        "peer_average_change_pct": peer_average_change_pct,
        "try_vs_peer_gap_pct": try_vs_peer_gap_pct,
        "peer_breadth_up": peer_breadth_up,
        "peer_breadth_down": peer_breadth_down,
        "peer_count": len(peer_pairs),
        "peer_symbols": [item["quote_currency"] for item in peer_pairs[:5]],
        "vix_close": volatility_snapshot["vix_close"],
        "vix_change_pct": volatility_snapshot["vix_change_pct"],
        "vix9d_close": volatility_snapshot["vix9d_close"],
        "vix9d_change_pct": volatility_snapshot["vix9d_change_pct"],
        "vvix_close": volatility_snapshot["vvix_close"],
        "vvix_change_pct": volatility_snapshot["vvix_change_pct"],
        "vxeem_close": volatility_snapshot["vxeem_close"],
        "vxeem_change_pct": volatility_snapshot["vxeem_change_pct"],
        "ovx_close": volatility_snapshot["ovx_close"],
        "ovx_change_pct": volatility_snapshot["ovx_change_pct"],
        "gvz_close": volatility_snapshot["gvz_close"],
        "gvz_change_pct": volatility_snapshot["gvz_change_pct"],
        "volatility_signal": volatility_snapshot["signal"],
        "volatility_regime_label": volatility_snapshot["regime_label"],
        "volatility_score": volatility_snapshot["score"],
        "front_end_signal": volatility_snapshot["front_end_signal"],
        "tail_hedging_signal": volatility_snapshot["tail_signal"],
        "em_vol_signal": volatility_snapshot["em_signal"],
        "oil_vol_signal": volatility_snapshot["oil_signal"],
        "gold_vol_signal": volatility_snapshot["gold_signal"],
        "commodity_vol_signal": volatility_snapshot["commodity_signal"],
    }


def _volatility_snapshot(series_snapshots: list[dict]) -> dict:
    by_symbol = {
        item["symbol"]: item
        for item in series_snapshots
        if item.get("symbol") in VOLATILITY_SYMBOLS
    }

    vix = by_symbol.get("VIX")
    vix9d = by_symbol.get("VIX9D")
    vvix = by_symbol.get("VVIX")
    vxeem = by_symbol.get("VXEEM")
    ovx = by_symbol.get("OVX")
    gvz = by_symbol.get("GVZ")

    score = (
        _volatility_component_score(vix, threshold=2.0, weight=1.0)
        + _volatility_component_score(vix9d, threshold=2.0, weight=1.2)
        + _volatility_component_score(vvix, threshold=2.0, weight=0.9)
        + _volatility_component_score(vxeem, threshold=1.5, weight=1.1)
        + _volatility_component_score(ovx, threshold=3.0, weight=0.7)
        + _volatility_component_score(gvz, threshold=2.5, weight=0.5)
    )

    if score >= 3.0:
        regime_label = "Cross-asset volatility stress"
        signal = "cross-asset volatility stress rising"
    elif score >= 1.2:
        regime_label = "Volatility leaning firmer"
        signal = "volatility is leaning firmer"
    elif score <= -3.0:
        regime_label = "Cross-asset volatility relief"
        signal = "cross-asset volatility stress is easing"
    elif score <= -1.2:
        regime_label = "Volatility easing"
        signal = "volatility is easing"
    else:
        regime_label = "Mixed volatility backdrop"
        signal = "volatility is mixed to steady"

    front_end_signal = _front_end_vol_signal(vix, vix9d)
    tail_signal = _single_vol_signal(
        vvix,
        rising_label="tail-hedging demand is rising",
        easing_label="tail-hedging demand is easing",
        steady_label="tail-hedging demand is steady",
        threshold=2.0,
    )
    em_signal = _single_vol_signal(
        vxeem,
        rising_label="EM volatility pressure is rising",
        easing_label="EM volatility pressure is easing",
        steady_label="EM volatility pressure is steady",
        threshold=1.5,
    )
    oil_signal = _single_vol_signal(
        ovx,
        rising_label="oil volatility pressure is rising",
        easing_label="oil volatility pressure is easing",
        steady_label="oil volatility pressure is steady",
        threshold=3.0,
    )
    gold_signal = _single_vol_signal(
        gvz,
        rising_label="gold volatility pressure is rising",
        easing_label="gold volatility pressure is easing",
        steady_label="gold volatility pressure is steady",
        threshold=2.5,
    )
    commodity_signal = _commodity_vol_signal(ovx, gvz)

    summary_parts: list[str] = []
    if vix and vix.get("close_value") is not None:
        summary_parts.append(
            f"VIX at {vix['close_value']:.2f} ({_signed_pct(vix.get('change_pct'))}) with {signal}"
        )
    if vix9d and vix9d.get("close_value") is not None:
        vix9d_change = _signed_pct(vix9d.get("change_pct"))
        summary_parts.append(
            f"VIX9D at {vix9d['close_value']:.2f} ({vix9d_change}) "
            f"with {front_end_signal}"
        )
    if vvix and vvix.get("close_value") is not None:
        vvix_change = _signed_pct(vvix.get("change_pct"))
        summary_parts.append(
            f"VVIX at {vvix['close_value']:.2f} ({vvix_change}) "
            f"where {tail_signal}"
        )
    if vxeem and vxeem.get("close_value") is not None:
        vxeem_change = _signed_pct(vxeem.get("change_pct"))
        summary_parts.append(
            f"VXEEM at {vxeem['close_value']:.2f} ({vxeem_change}) "
            f"and {em_signal}"
        )
    if ovx and ovx.get("close_value") is not None:
        ovx_change = _signed_pct(ovx.get("change_pct"))
        summary_parts.append(
            f"OVX at {ovx['close_value']:.2f} ({ovx_change}) "
            f"and {oil_signal}"
        )
    if gvz and gvz.get("close_value") is not None:
        gvz_change = _signed_pct(gvz.get("change_pct"))
        summary_parts.append(
            f"GVZ at {gvz['close_value']:.2f} ({gvz_change}) "
            f"and {gold_signal}"
        )
    if commodity_signal not in {
        "commodity volatility is mixed to steady",
        "no commodity-volatility read",
    }:
        summary_parts.append(commodity_signal)

    return {
        "score": round(score, 3),
        "signal": signal,
        "regime_label": regime_label,
        "front_end_signal": front_end_signal,
        "tail_signal": tail_signal,
        "em_signal": em_signal,
        "oil_signal": oil_signal,
        "gold_signal": gold_signal,
        "commodity_signal": commodity_signal,
        "summary_parts": summary_parts,
        "vix_close": vix.get("close_value") if vix else None,
        "vix_change_pct": vix.get("change_pct") if vix else None,
        "vix9d_close": vix9d.get("close_value") if vix9d else None,
        "vix9d_change_pct": vix9d.get("change_pct") if vix9d else None,
        "vvix_close": vvix.get("close_value") if vvix else None,
        "vvix_change_pct": vvix.get("change_pct") if vvix else None,
        "vxeem_close": vxeem.get("close_value") if vxeem else None,
        "vxeem_change_pct": vxeem.get("change_pct") if vxeem else None,
        "ovx_close": ovx.get("close_value") if ovx else None,
        "ovx_change_pct": ovx.get("change_pct") if ovx else None,
        "gvz_close": gvz.get("close_value") if gvz else None,
        "gvz_change_pct": gvz.get("change_pct") if gvz else None,
    }


def _volatility_component_score(
    item: dict | None,
    *,
    threshold: float,
    weight: float,
) -> float:
    if not item:
        return 0.0
    change_pct = item.get("change_pct")
    trend = item.get("trend")
    if change_pct is None or trend not in {"up", "down"}:
        return 0.0
    if trend == "up" and change_pct >= threshold:
        return weight
    if trend == "down" and change_pct <= -threshold:
        return -weight
    return 0.0


def _front_end_vol_signal(vix: dict | None, vix9d: dict | None) -> str:
    if not vix9d or vix9d.get("close_value") is None:
        return "no short-dated vol read"
    if not vix or vix.get("close_value") is None:
        return "short-dated volatility has no VIX anchor"
    vix_close = vix["close_value"]
    vix9d_close = vix9d["close_value"]
    vix_change = vix.get("change_pct") or 0.0
    vix9d_change = vix9d.get("change_pct") or 0.0
    if vix9d_close >= vix_close + 0.75 or vix9d_change >= vix_change + 1.5:
        return "short-dated event risk is leading"
    if vix9d_close <= vix_close - 0.75 or vix9d_change <= vix_change - 1.5:
        return "short-dated event risk is muted"
    return "short-dated event risk is in line with VIX"


def _single_vol_signal(
    item: dict | None,
    *,
    rising_label: str,
    easing_label: str,
    steady_label: str,
    threshold: float,
) -> str:
    if not item or item.get("change_pct") is None or item.get("trend") not in {"up", "down"}:
        return steady_label
    change_pct = item["change_pct"]
    if item["trend"] == "up" and change_pct >= threshold:
        return rising_label
    if item["trend"] == "down" and change_pct <= -threshold:
        return easing_label
    return steady_label


def _commodity_vol_signal(ovx: dict | None, gvz: dict | None) -> str:
    oil_state = _vol_signal_state(ovx, threshold=3.0)
    gold_state = _vol_signal_state(gvz, threshold=2.5)
    if oil_state == "rising" and gold_state == "rising":
        return "commodity volatility is rising across oil and gold"
    if oil_state == "easing" and gold_state == "easing":
        return "commodity volatility is easing across oil and gold"
    if oil_state == "rising" or gold_state == "rising":
        return "commodity volatility is rising in at least one key complex"
    if oil_state == "easing" or gold_state == "easing":
        return "commodity volatility is easing in at least one key complex"
    if ovx or gvz:
        return "commodity volatility is mixed to steady"
    return "no commodity-volatility read"


def _vol_signal_state(item: dict | None, *, threshold: float) -> str:
    if not item or item.get("change_pct") is None or item.get("trend") not in {"up", "down"}:
        return "steady"
    change_pct = item["change_pct"]
    if item["trend"] == "up" and change_pct >= threshold:
        return "rising"
    if item["trend"] == "down" and change_pct <= -threshold:
        return "easing"
    return "steady"


def _signed_pct(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.3f}%"


def _signed_delta(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:+.3f}"


def _source_entry(session: Session, source: Source) -> dict:
    latest_run = session.scalar(
        select(SourceFetchRun)
        .where(SourceFetchRun.source_id == source.id)
        .order_by(SourceFetchRun.started_at.desc(), SourceFetchRun.id.desc())
        .limit(1)
    )
    return {
        "slug": source.slug,
        "name": source.name,
        "category": source.category,
        "trust_tier": source.trust_tier,
        "collection_method": source.collection_method,
        "freshness_expectation": source.freshness_expectation,
        "endpoint": source.endpoint,
        "last_fetch": {
            "status": latest_run.status if latest_run else "never-run",
            "started_at": (
                latest_run.started_at.replace(tzinfo=UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
                if latest_run
                else None
            ),
            "finished_at": (
                latest_run.finished_at.replace(tzinfo=UTC)
                .isoformat(timespec="seconds")
                .replace("+00:00", "Z")
                if latest_run and latest_run.finished_at
                else None
            ),
            "items_ingested": latest_run.items_ingested if latest_run else 0,
            "error_message": latest_run.error_message if latest_run else None,
        },
    }
