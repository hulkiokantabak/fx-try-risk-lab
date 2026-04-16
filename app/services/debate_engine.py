from __future__ import annotations

from dataclasses import dataclass
from statistics import median

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.entities import (
    AgentRoundOutput,
    AssessmentCycle,
    CycleSpecialistActivation,
    HouseView,
)
from app.services.assessment_engine import load_evidence_pack
from app.services.report_store import purge_cycle_reports

HORIZONS = ("1w", "1m", "3m", "6m", "1y")

CATEGORY_LABELS = {
    "domestic_rates": "domestic policy-rate posture",
    "external_balance": "current-account financing",
    "fx": "USD/TRY spot pressure",
    "global_dollar": "broad dollar regime",
    "global_rates": "global rates pressure",
    "growth": "growth cushion",
    "peer_fx": "peer EM and CEE FX pressure",
    "reserves": "reserve adequacy",
    "turkey_inflation": "domestic inflation path",
    "volatility": "tail-risk pricing",
}

CATEGORY_DIRECTIONS = {
    "domestic_rates": -1,
    "external_balance": -1,
    "fx": 1,
    "global_dollar": 1,
    "global_rates": 1,
    "growth": -1,
    "peer_fx": 1,
    "reserves": -1,
    "turkey_inflation": 1,
    "volatility": 1,
}

CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2, "very_high": 3}
CONFIDENCE_VALUES = {value: key for key, value in CONFIDENCE_RANK.items()}

HOUSE_WEIGHTS = {
    "1w": {"Atlas": 15, "Bosphorus": 20, "Flow": 35, "Vega": 30},
    "1m": {"Atlas": 20, "Bosphorus": 30, "Flow": 25, "Vega": 25},
    "3m": {"Atlas": 30, "Bosphorus": 35, "Flow": 15, "Vega": 20},
    "6m": {"Atlas": 35, "Bosphorus": 35, "Flow": 15, "Vega": 15},
    "1y": {"Atlas": 40, "Bosphorus": 40, "Flow": 10, "Vega": 10},
}


@dataclass(frozen=True)
class AgentProfile:
    name: str
    role: str
    base_curve: dict[str, float]
    shift_scale: dict[str, float]
    signal_weights: dict[str, float]
    specialist_weights: dict[str, float]
    headline_weight: float
    chatter_weight: float
    blocker_penalty: float
    missing_focus: str
    hidden_risk: str


@dataclass(frozen=True)
class SpecialistProfile:
    name: str
    role: str
    curve_adjustments: dict[str, float]
    note: str


CORE_AGENTS = (
    AgentProfile(
        name="Atlas",
        role="Global Macro Economist",
        base_curve={"1w": 45.0, "1m": 48.0, "3m": 52.0, "6m": 55.0, "1y": 57.0},
        shift_scale={"1w": 0.65, "1m": 0.85, "3m": 1.0, "6m": 1.1, "1y": 1.15},
        signal_weights={
            "global_rates": 1.1,
            "global_dollar": 1.0,
            "growth": 0.8,
            "external_balance": 0.4,
            "peer_fx": 0.5,
        },
        specialist_weights={"Meridian": 2.5, "Strait": 1.8},
        headline_weight=0.6,
        chatter_weight=0.2,
        blocker_penalty=2.2,
        missing_focus="the next global rates or dollar-liquidity print",
        hidden_risk="a global dollar squeeze that pulls TRY into a broader EM deleveraging move",
    ),
    AgentProfile(
        name="Bosphorus",
        role="Turkey Macro Economist",
        base_curve={"1w": 46.0, "1m": 51.0, "3m": 57.0, "6m": 61.0, "1y": 64.0},
        shift_scale={"1w": 0.55, "1m": 0.8, "3m": 1.0, "6m": 1.1, "1y": 1.2},
        signal_weights={
            "domestic_rates": 1.0,
            "turkey_inflation": 1.2,
            "fx": 1.0,
            "external_balance": 0.9,
            "growth": 0.4,
            "peer_fx": 0.6,
            "reserves": 1.0,
        },
        specialist_weights={"Ankara": 2.2, "Ledger": 2.4},
        headline_weight=0.4,
        chatter_weight=0.1,
        blocker_penalty=1.8,
        missing_focus="the next policy signal that clarifies reserve use and credibility",
        hidden_risk="stability that is being funded temporarily rather than genuinely earned",
    ),
    AgentProfile(
        name="Flow",
        role="EM FX Spot Trader",
        base_curve={"1w": 48.0, "1m": 50.0, "3m": 49.0, "6m": 46.0, "1y": 43.0},
        shift_scale={"1w": 1.2, "1m": 1.0, "3m": 0.8, "6m": 0.55, "1y": 0.35},
        signal_weights={
            "domestic_rates": 0.4,
            "fx": 1.3,
            "global_dollar": 0.8,
            "peer_fx": 1.1,
            "volatility": 0.9,
        },
        specialist_weights={"Ledger": 1.2, "Strait": 1.5, "Meridian": 1.4},
        headline_weight=0.9,
        chatter_weight=0.5,
        blocker_penalty=2.5,
        missing_focus=(
            "clean spot or intervention evidence that confirms what price is really doing"
        ),
        hidden_risk="managed spot that looks calm while one-way pressure is building underneath",
    ),
    AgentProfile(
        name="Vega",
        role="FX Options / Vol Trader",
        base_curve={"1w": 49.0, "1m": 51.0, "3m": 54.0, "6m": 56.0, "1y": 58.0},
        shift_scale={"1w": 1.0, "1m": 1.0, "3m": 0.9, "6m": 0.8, "1y": 0.7},
        signal_weights={
            "domestic_rates": 0.4,
            "volatility": 1.2,
            "global_dollar": 0.7,
            "fx": 0.6,
            "peer_fx": 0.4,
        },
        specialist_weights={"Strait": 1.8, "Meridian": 1.7, "Ankara": 1.2},
        headline_weight=0.7,
        chatter_weight=0.2,
        blocker_penalty=2.8,
        missing_focus=(
            "the next options or implied-vol signal that tells us whether tails are getting "
            "pricier"
        ),
        hidden_risk="tail insurance repricing before spot fully reflects the change in regime",
    ),
)

SPECIALISTS = {
    "Ankara": SpecialistProfile(
        name="Ankara",
        role="Political / Policy Risk Analyst",
        curve_adjustments={"1w": 1.5, "1m": 2.5, "3m": 3.0, "6m": 2.2, "1y": 1.0},
        note="Domestic political or policy headlines can reprice TRY faster than macro releases.",
    ),
    "Ledger": SpecialistProfile(
        name="Ledger",
        role="Sovereign Credit / Reserves Analyst",
        curve_adjustments={"1w": 1.0, "1m": 2.0, "3m": 3.0, "6m": 3.0, "1y": 2.0},
        note="Reserve and funding strain can make stable spot action look stronger than it is.",
    ),
    "Strait": SpecialistProfile(
        name="Strait",
        role="Global Political Risk Analyst",
        curve_adjustments={"1w": 2.0, "1m": 2.5, "3m": 2.2, "6m": 1.5, "1y": 1.0},
        note=(
            "Geopolitical repricing can hit TRY through energy, regional risk, and global EM "
            "sentiment."
        ),
    ),
    "Meridian": SpecialistProfile(
        name="Meridian",
        role="Global Cycle / Liquidity Strategist",
        curve_adjustments={"1w": 1.0, "1m": 2.0, "3m": 2.5, "6m": 2.2, "1y": 1.5},
        note="Global liquidity and dollar-cycle stress change the entire carry backdrop for TRY.",
    ),
}


def run_fx_experts_rounds(
    session: Session,
    settings: Settings,
    *,
    cycle_id: int,
) -> AssessmentCycle:
    cycle = session.get(AssessmentCycle, cycle_id)
    if cycle is None:
        raise ValueError(f"Assessment cycle {cycle_id} was not found.")

    evidence_pack = load_evidence_pack(cycle)
    if not evidence_pack:
        raise ValueError("Round 0 evidence pack is missing; rebuild the cycle first.")

    _clear_existing_outputs(session, cycle.id)
    purge_cycle_reports(session, cycle_id=cycle.id, reports_dir=settings.reports_dir)

    activations = list(
        session.scalars(
            select(CycleSpecialistActivation)
            .where(CycleSpecialistActivation.cycle_id == cycle.id)
            .order_by(CycleSpecialistActivation.id.asc())
        ).all()
    )
    signals = _build_signal_snapshot(evidence_pack)

    round1_outputs = [_build_round1_output(profile, cycle, signals) for profile in CORE_AGENTS]
    _save_outputs(session, cycle.id, "round1", round1_outputs)

    round2_outputs = [
        _build_round2_output(profile, cycle, signals, evidence_pack) for profile in CORE_AGENTS
    ]
    _save_outputs(session, cycle.id, "round2", round2_outputs)

    specialist_outputs = [
        _build_specialist_overlay(profile, activation, cycle.primary_horizon)
        for activation in activations
        if (profile := SPECIALISTS.get(activation.specialist_name)) is not None
    ]
    if specialist_outputs:
        _save_outputs(session, cycle.id, "round2", specialist_outputs)

    round3_outputs = _build_round3_outputs(cycle, round2_outputs, signals)
    _save_outputs(session, cycle.id, "round3", round3_outputs)

    round4_outputs = _build_round4_outputs(cycle, round2_outputs, round3_outputs, signals)
    _save_outputs(session, cycle.id, "round4", round4_outputs)

    house_view = _build_house_view(
        cycle=cycle,
        round4_outputs=round4_outputs,
        specialist_outputs=specialist_outputs,
        signals=signals,
    )
    session.add(house_view)

    cycle.status = "assessed"
    cycle.summary = (
        f"FX Experts completed rounds 1-4. House {cycle.primary_horizon} probability "
        f"{house_view.house_primary_score:.1f} with {house_view.house_confidence} confidence."
    )
    session.commit()
    session.refresh(cycle)
    return cycle


def _clear_existing_outputs(session: Session, cycle_id: int) -> None:
    session.execute(delete(AgentRoundOutput).where(AgentRoundOutput.cycle_id == cycle_id))
    session.execute(delete(HouseView).where(HouseView.cycle_id == cycle_id))
    session.flush()


def _save_outputs(
    session: Session,
    cycle_id: int,
    round_name: str,
    outputs: list[dict],
) -> None:
    session.add_all(
        AgentRoundOutput(
            cycle_id=cycle_id,
            agent_name=output["agent_name"],
            agent_role=output["agent_role"],
            round_name=round_name,
            stance=output.get("stance"),
            primary_horizon=output.get("primary_horizon"),
            primary_risk_score=output.get("primary_risk_score"),
            confidence=output.get("confidence"),
            risk_curve=output.get("risk_curve"),
            top_drivers=output.get("top_drivers"),
            counterevidence=output.get("counterevidence"),
            watch_triggers=output.get("watch_triggers"),
            content=output["content"],
        )
        for output in outputs
    )
    session.flush()


def _build_signal_snapshot(evidence_pack: dict) -> dict:
    macro_summary = evidence_pack.get("macro_summary", {})
    price_summary = evidence_pack.get("price_summary", {})
    news_summary = evidence_pack.get("news_summary", {})
    data_completeness = evidence_pack.get("data_completeness", {})
    category_signals: dict[str, int] = {}
    observations_by_category: dict[str, list[dict]] = {}
    observations_by_code: dict[str, dict] = {}

    for observation in macro_summary.get("key_observations", []):
        category = observation.get("category")
        if category:
            observations_by_category.setdefault(category, []).append(observation)
        code = observation.get("code")
        if code:
            observations_by_code[code] = observation

    for category, observations in observations_by_category.items():
        direction = CATEGORY_DIRECTIONS.get(category)
        if direction is None:
            continue
        directional_values: list[int] = []
        for observation in observations:
            trend = observation.get("trend")
            if trend not in {"up", "down"}:
                continue
            movement = 1 if trend == "up" else -1
            directional_values.append(direction * movement)
        if not directional_values:
            continue
        average_signal = sum(directional_values) / len(directional_values)
        if average_signal >= 0.34:
            category_signals[category] = 1
        elif average_signal <= -0.34:
            category_signals[category] = -1

    price_series = price_summary.get("series", [])
    derived_pairs = price_summary.get("derived_pairs", [])
    price_by_symbol = {item.get("symbol"): item for item in price_series}
    peer_pairs: list[dict] = []
    derived_by_symbol = {item.get("symbol"): item for item in derived_pairs}
    if derived_by_symbol.get("USDTRY_DERIVED"):
        derived_usdtry = derived_by_symbol["USDTRY_DERIVED"]
        if derived_usdtry.get("trend") in {"up", "down"}:
            category_signals["fx"] = 1 if derived_usdtry["trend"] == "up" else -1
    elif price_by_symbol.get("D.TRY.EUR.SP00.A", {}).get("trend") in {"up", "down"}:
        category_signals["fx"] = (
            1 if price_by_symbol["D.TRY.EUR.SP00.A"]["trend"] == "up" else -1
        )

    peer_pairs = [
        item
        for item in derived_pairs
        if item.get("category") == "peer_fx" and item.get("change_pct") is not None
    ]
    if peer_pairs:
        peer_average = sum(item["change_pct"] for item in peer_pairs) / len(peer_pairs)
        if peer_average >= 0.25:
            category_signals["peer_fx"] = 1
        elif peer_average <= -0.25:
            category_signals["peer_fx"] = -1

        derived_usdtry = derived_by_symbol.get("USDTRY_DERIVED")
        if derived_usdtry and derived_usdtry.get("change_pct") is not None:
            relative_stress = derived_usdtry["change_pct"] - peer_average
            if relative_stress >= 0.75:
                category_signals["fx"] = 1
            elif relative_stress <= -0.75:
                category_signals["fx"] = -1

    market_regime = price_summary.get("market_regime", {})
    volatility_score = market_regime.get("volatility_score")
    if isinstance(volatility_score, (int, float)):
        if volatility_score >= 1:
            category_signals["volatility"] = 1
        elif volatility_score <= -1:
            category_signals["volatility"] = -1
    elif price_by_symbol.get("VIX", {}).get("trend") in {"up", "down"}:
        category_signals["volatility"] = (
            1 if price_by_symbol["VIX"]["trend"] == "up" else -1
        )

    activated_specialists = {
        item["specialist_name"] for item in evidence_pack.get("activated_specialists", [])
    }

    return {
        "macro_summary": macro_summary,
        "price_summary": price_summary,
        "news_summary": news_summary,
        "data_completeness": data_completeness,
        "category_signals": category_signals,
        "observations_by_category": observations_by_category,
        "observations_by_code": observations_by_code,
        "price_by_symbol": price_by_symbol,
        "derived_pairs": derived_pairs,
        "peer_pairs": peer_pairs,
        "activated_specialists": activated_specialists,
        "blocker_count": data_completeness.get("sources_with_blockers", 0),
        "headline_count": news_summary.get("headline_count_14d", 0),
        "chatter_count": news_summary.get("chatter_count_14d", 0),
    }


def _build_round1_output(profile: AgentProfile, cycle: AssessmentCycle, signals: dict) -> dict:
    topics = _top_topics(profile, signals)
    missing_categories = _missing_relevant_categories(profile, signals)
    missing_data = (
        f"I still need {CATEGORY_LABELS.get(missing_categories[0], missing_categories[0])}."
        if missing_categories
        else f"I still need {profile.missing_focus}."
    )
    content = (
        f"Topics: {', '.join(topics[:3])}. "
        f"Missing data point: {missing_data} "
        f"Hidden risk: {profile.hidden_risk}."
    )
    return {
        "agent_name": profile.name,
        "agent_role": profile.role,
        "stance": None,
        "primary_horizon": cycle.primary_horizon,
        "primary_risk_score": None,
        "confidence": None,
        "risk_curve": None,
        "top_drivers": topics[:5],
        "counterevidence": missing_data,
        "watch_triggers": [_watch_trigger(profile, signals)],
        "content": content,
    }


def _build_round2_output(
    profile: AgentProfile,
    cycle: AssessmentCycle,
    signals: dict,
    evidence_pack: dict,
) -> dict:
    curve = _initial_curve(profile, signals)
    primary_score = curve[cycle.primary_horizon]
    stance = _stance_from_score(primary_score)
    confidence = _agent_confidence(profile, signals)
    top_drivers = _round_drivers(profile, signals)
    counterevidence = _counterevidence(profile, signals)
    watch_triggers = _watch_triggers(profile, signals)
    content = (
        f"{profile.name} opens {stance} on TRY for {cycle.primary_horizon}. "
        f"The curve is driven by {', '.join(top_drivers[:3])}. "
        f"Evidence hierarchy: market and policy signals are thin, so hard-data coverage "
        f"and recent headlines carry more weight than usual. "
        f"Counterevidence: {counterevidence}."
    )
    return {
        "agent_name": profile.name,
        "agent_role": profile.role,
        "stance": stance,
        "primary_horizon": cycle.primary_horizon,
        "primary_risk_score": primary_score,
        "confidence": confidence,
        "risk_curve": curve,
        "top_drivers": top_drivers,
        "counterevidence": counterevidence,
        "watch_triggers": watch_triggers,
        "content": content,
        "profile": profile,
        "evidence_generated_at": evidence_pack.get("generated_at"),
    }


def _build_specialist_overlay(
    profile: SpecialistProfile,
    activation: CycleSpecialistActivation,
    primary_horizon: str,
) -> dict:
    content = (
        f"{profile.name} is active on {activation.trigger_topic}. {profile.note} "
        f"Recommended overlay: {profile.curve_adjustments[primary_horizon]:+.1f} on "
        f"{primary_horizon}, capped inside the house-view overlay rules."
    )
    return {
        "agent_name": profile.name,
        "agent_role": profile.role,
        "stance": None,
        "primary_horizon": primary_horizon,
        "primary_risk_score": round(profile.curve_adjustments[primary_horizon], 1),
        "confidence": "high",
        "risk_curve": {
            horizon: round(value, 1)
            for horizon, value in profile.curve_adjustments.items()
        },
        "top_drivers": [activation.trigger_topic],
        "counterevidence": "Overlay remains advisory and cannot replace the four core votes.",
        "watch_triggers": [activation.trigger_topic],
        "content": content,
    }


def _build_round3_outputs(
    cycle: AssessmentCycle,
    round2_outputs: list[dict],
    signals: dict,
) -> list[dict]:
    outputs: list[dict] = []
    for output in round2_outputs:
        profile = output["profile"]
        peers = sorted(
            (peer for peer in round2_outputs if peer["agent_name"] != profile.name),
            key=lambda peer: abs(
                (peer["primary_risk_score"] or 0.0) - (output["primary_risk_score"] or 0.0)
            ),
            reverse=True,
        )[:2]
        peer_average = sum(peer["primary_risk_score"] or 0.0 for peer in peers) / len(peers)
        revision_delta = round(
            max(
                min((peer_average - (output["primary_risk_score"] or 0.0)) * 0.18, 3.5),
                -3.5,
            ),
            1,
        )
        revised_curve = _shift_curve(output["risk_curve"], revision_delta, profile.shift_scale)
        revised_primary = revised_curve[cycle.primary_horizon]
        critique_lines = []
        for peer in peers:
            concession = (
                f"I grant that {peer['agent_name']} is right to flag {peer['top_drivers'][0]}"
                if peer.get("top_drivers")
                else f"I grant that {peer['agent_name']} sees a real source of pressure"
            )
            critique_lines.append(
                f"{concession}, but a {peer['primary_risk_score']:.1f} score misses "
                f"{_round_drivers(profile, signals)[0]} and underweights "
                f"{_watch_trigger(profile, signals)}."
            )
        own_risk = _self_critique(profile, signals)
        content = (
            " ".join(critique_lines)
            + f" Strongest reason I may be wrong: {own_risk}. "
            + f"I revise to {revised_primary:.1f} on {cycle.primary_horizon} because "
            + "debate exposed "
            + f"{'more' if revision_delta > 0 else 'less' if revision_delta < 0 else 'no extra'} "
            + "risk than my Round 2 frame captured."
        )
        outputs.append(
            {
                "agent_name": profile.name,
                "agent_role": profile.role,
                "stance": _stance_from_score(revised_primary),
                "primary_horizon": cycle.primary_horizon,
                "primary_risk_score": revised_primary,
                "confidence": output["confidence"],
                "risk_curve": revised_curve,
                "top_drivers": output["top_drivers"],
                "counterevidence": own_risk,
                "watch_triggers": output["watch_triggers"],
                "content": content,
                "profile": profile,
            }
        )
    return outputs


def _build_round4_outputs(
    cycle: AssessmentCycle,
    round2_outputs: list[dict],
    round3_outputs: list[dict],
    signals: dict,
) -> list[dict]:
    round2_by_agent = {output["agent_name"]: output for output in round2_outputs}
    outputs: list[dict] = []
    for round3 in round3_outputs:
        profile = round3["profile"]
        round2 = round2_by_agent[profile.name]
        primary_delta = round(
            (round3["primary_risk_score"] or 0.0) - (round2["primary_risk_score"] or 0.0),
            1,
        )
        watch_triggers = _watch_triggers(profile, signals)
        content = (
            f"Since Round 2, {profile.name} moved {primary_delta:+.1f} points on "
            f"{cycle.primary_horizon}. Final drivers: {', '.join(round3['top_drivers'][:3])}. "
            f"Counterevidence remains {round2['counterevidence']}. "
            f"Single watch trigger: {watch_triggers[0]}."
        )
        outputs.append(
            {
                "agent_name": profile.name,
                "agent_role": profile.role,
                "stance": round3["stance"],
                "primary_horizon": cycle.primary_horizon,
                "primary_risk_score": round3["primary_risk_score"],
                "confidence": round2["confidence"],
                "risk_curve": round3["risk_curve"],
                "top_drivers": round3["top_drivers"],
                "counterevidence": round2["counterevidence"],
                "watch_triggers": watch_triggers,
                "content": content,
            }
        )
    return outputs


def _build_house_view(
    *,
    cycle: AssessmentCycle,
    round4_outputs: list[dict],
    specialist_outputs: list[dict],
    signals: dict,
) -> HouseView:
    core_scores = {output["agent_name"]: output["risk_curve"] for output in round4_outputs}
    house_curve: dict[str, float] = {}
    for horizon in HORIZONS:
        weighted = sum(
            core_scores[agent_name][horizon] * weight / 100
            for agent_name, weight in HOUSE_WEIGHTS[horizon].items()
        )
        overlay = sum(
            specialist["risk_curve"].get(horizon, 0.0) for specialist in specialist_outputs
        )
        overlay = max(min(overlay, 10.0), -10.0)
        house_curve[horizon] = _clamp_score(weighted + overlay)

    primary_scores = [output["risk_curve"][cycle.primary_horizon] for output in round4_outputs]
    disagreement_range = (
        round(max(primary_scores) - min(primary_scores), 1) if primary_scores else 0.0
    )
    house_confidence = _house_confidence(round4_outputs, disagreement_range, signals)
    minority_note = _minority_risk_note(round4_outputs, house_curve[cycle.primary_horizon])
    stress_flag = _stress_flag(round4_outputs, specialist_outputs, house_curve)

    return HouseView(
        cycle_id=cycle.id,
        primary_horizon=cycle.primary_horizon,
        house_primary_score=house_curve[cycle.primary_horizon],
        house_confidence=house_confidence,
        disagreement_range=disagreement_range,
        stress_flag=stress_flag,
        minority_risk_note=minority_note,
        risk_curve=house_curve,
    )


def _initial_curve(profile: AgentProfile, signals: dict) -> dict[str, float]:
    macro_shift = sum(
        profile.signal_weights.get(category, 0.0) * signal
        for category, signal in signals["category_signals"].items()
    ) * 6.0
    headline_shift = min(signals["headline_count"], 6) * profile.headline_weight * 0.6
    chatter_shift = min(signals["chatter_count"], 6) * profile.chatter_weight * 0.4
    specialist_shift = sum(
        profile.specialist_weights.get(name, 0.0) for name in signals["activated_specialists"]
    )
    blocker_penalty = min(signals["blocker_count"], 4) * profile.blocker_penalty * 0.25
    raw_shift = macro_shift + headline_shift + chatter_shift + specialist_shift - blocker_penalty
    return {
        horizon: _clamp_score(
            profile.base_curve[horizon] + raw_shift * profile.shift_scale[horizon]
        )
        for horizon in HORIZONS
    }


def _agent_confidence(profile: AgentProfile, signals: dict) -> str:
    relevant = [category for category in profile.signal_weights if category in CATEGORY_LABELS]
    observed = [category for category in relevant if category in signals["category_signals"]]
    ratio = len(observed) / len(relevant) if relevant else 0.0
    blockers = signals["blocker_count"]
    if ratio >= 0.8 and blockers == 0:
        return "very_high"
    if ratio >= 0.6 and blockers <= 1:
        return "high"
    if ratio >= 0.35:
        return "medium"
    return "low"


def _round_drivers(profile: AgentProfile, signals: dict) -> list[str]:
    scored_categories: list[tuple[float, str]] = []
    for category, weight in profile.signal_weights.items():
        score = abs(signals["category_signals"].get(category, 0)) * weight
        if score > 0:
            scored_categories.append((score, CATEGORY_LABELS.get(category, category)))
    scored_categories.sort(key=lambda item: (-item[0], item[1]))

    drivers = [label for _, label in scored_categories[:4]]
    if signals["headline_count"] > 0 and len(drivers) < 5:
        drivers.append("event-rich headline tape")
    if signals["activated_specialists"] and len(drivers) < 5:
        drivers.append(
            ", ".join(sorted(signals["activated_specialists"])) + " specialist overlay"
        )
    if not drivers:
        drivers.append("thin evidence and unresolved data gaps")
    return drivers[:5]


def _counterevidence(profile: AgentProfile, signals: dict) -> str:
    missing = _missing_relevant_categories(profile, signals)
    if missing:
        labels = ", ".join(CATEGORY_LABELS.get(category, category) for category in missing[:2])
        return f"Key evidence is still missing around {labels}."
    if signals["blocker_count"]:
        return "Blocked sources reduce how much conviction I can place on this frame."
    return "Existing data does not yet show a decisive contradiction."


def _watch_trigger(profile: AgentProfile, signals: dict) -> str:
    missing = _missing_relevant_categories(profile, signals)
    if missing:
        return f"Fresh {CATEGORY_LABELS.get(missing[0], missing[0])} data"
    if signals["activated_specialists"]:
        specialist = sorted(signals["activated_specialists"])[0]
        return f"{specialist} trigger escalating from mention to confirmed event"
    return profile.missing_focus


def _watch_triggers(profile: AgentProfile, signals: dict) -> list[str]:
    triggers = [_watch_trigger(profile, signals)]
    if signals["headline_count"]:
        triggers.append("A step-change in headline density or tone")
    if signals["blocker_count"]:
        triggers.append("Blocked official sources becoming available")
    return triggers[:3]


def _self_critique(profile: AgentProfile, signals: dict) -> str:
    missing = _missing_relevant_categories(profile, signals)
    if missing:
        return (
            "my view is leaning on indirect evidence because "
            f"{CATEGORY_LABELS.get(missing[0], missing[0])} is still missing"
        )
    return (
        "the data may be orderly precisely because policy management is suppressing visible "
        "stress"
    )


def _top_topics(profile: AgentProfile, signals: dict) -> list[str]:
    topics = _round_drivers(profile, signals)
    hidden = profile.hidden_risk
    if hidden not in topics:
        topics.append(hidden)
    return topics[:5]


def _missing_relevant_categories(profile: AgentProfile, signals: dict) -> list[str]:
    return [
        category
        for category in profile.signal_weights
        if category not in signals["category_signals"]
    ]


def _shift_curve(
    curve: dict[str, float],
    delta: float,
    shift_scale: dict[str, float],
) -> dict[str, float]:
    return {
        horizon: _clamp_score(curve[horizon] + delta * shift_scale[horizon] * 0.6)
        for horizon in HORIZONS
    }


def _house_confidence(round4_outputs: list[dict], disagreement_range: float, signals: dict) -> str:
    confidence_values = [
        CONFIDENCE_RANK.get(output.get("confidence", "low"), 0) for output in round4_outputs
    ]
    base_value = int(median(confidence_values)) if confidence_values else 0
    if disagreement_range >= 20:
        base_value -= 1
    if signals["data_completeness"].get("macro_coverage_ratio", 0.0) < 0.5:
        base_value -= 1
    if signals["blocker_count"] >= 2:
        base_value -= 1
    aligned = _core_alignment(round4_outputs)
    if aligned and base_value >= 2 and not signals["activated_specialists"]:
        base_value = 3
    return CONFIDENCE_VALUES[max(0, min(3, base_value))]


def _core_alignment(round4_outputs: list[dict]) -> bool:
    if len(round4_outputs) < 3:
        return False
    scores = [
        output["primary_risk_score"]
        for output in round4_outputs
        if output["primary_risk_score"] is not None
    ]
    if len(scores) < 3:
        return False
    return max(scores) - min(scores) <= 10


def _minority_risk_note(round4_outputs: list[dict], house_primary_score: float) -> str | None:
    dissenters = [
        output
        for output in round4_outputs
        if output["primary_risk_score"] is not None
        and abs(output["primary_risk_score"] - house_primary_score) >= 15
        and CONFIDENCE_RANK.get(output.get("confidence", "low"), 0) >= CONFIDENCE_RANK["high"]
    ]
    if not dissenters:
        return None
    dissenter = max(
        dissenters,
        key=lambda output: abs(output["primary_risk_score"] - house_primary_score),
    )
    return (
        f"{dissenter['agent_name']} remains the main dissenter with "
        f"{dissenter['primary_risk_score']:.1f} on the primary horizon."
    )


def _stress_flag(
    round4_outputs: list[dict],
    specialist_outputs: list[dict],
    house_curve: dict[str, float],
) -> bool:
    by_name = {output["agent_name"]: output for output in round4_outputs}
    if by_name.get("Flow", {}).get("risk_curve", {}).get("1w", 0.0) >= 65 and by_name.get(
        "Vega", {}
    ).get("risk_curve", {}).get("1w", 0.0) >= 65:
        return True
    if by_name.get("Bosphorus", {}).get("risk_curve", {}).get("3m", 0.0) >= 65 and any(
        specialist["agent_name"] == "Ledger" for specialist in specialist_outputs
    ):
        return True
    if any(
        specialist["agent_name"] in {"Ankara", "Strait"}
        for specialist in specialist_outputs
    ) and (house_curve["1w"] >= 60 or house_curve["1m"] >= 60):
        return True
    return False


def _stance_from_score(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 55:
        return "bearish TRY"
    if score <= 45:
        return "bullish TRY"
    return "neutral"


def _clamp_score(value: float) -> float:
    return round(max(1.0, min(99.0, value)), 1)
