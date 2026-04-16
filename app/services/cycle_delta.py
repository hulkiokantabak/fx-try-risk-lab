from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.entities import (
    AgentRoundOutput,
    AssessmentCycle,
    CycleSpecialistActivation,
    HouseView,
)
from app.services.assessment_engine import load_evidence_pack


@dataclass(frozen=True)
class CycleDeltaSummary:
    parent_cycle: AssessmentCycle | None
    previous_score: float | None
    current_score: float | None
    score_delta: float | None
    score_sentence: str
    previous_market_regime: str | None
    current_market_regime: str | None
    market_changed: bool
    market_sentence: str
    previous_turkey_regime: str | None
    current_turkey_regime: str | None
    turkey_changed: bool
    turkey_sentence: str
    added_specialists: list[str]
    removed_specialists: list[str]
    specialist_sentence: str
    previous_watch_triggers: list[str]
    current_watch_triggers: list[str]
    added_watch_triggers: list[str]
    removed_watch_triggers: list[str]
    watch_trigger_sentence: str
    summary: str


def build_cycle_delta_summary(
    session: Session,
    cycle: AssessmentCycle,
    *,
    evidence_pack: dict | None = None,
    activations: list[CycleSpecialistActivation] | None = None,
    latest_house_view: HouseView | None = None,
    round4_outputs: list[AgentRoundOutput] | None = None,
) -> CycleDeltaSummary:
    current_pack = evidence_pack or load_evidence_pack(cycle) or {}
    current_watch_triggers = collect_watch_triggers(round4_outputs or [])
    parent_cycle = (
        session.get(AssessmentCycle, cycle.parent_cycle_id)
        if cycle.parent_cycle_id is not None
        else None
    )
    if parent_cycle is None:
        return CycleDeltaSummary(
            parent_cycle=None,
            previous_score=None,
            current_score=latest_house_view.house_primary_score if latest_house_view else None,
            score_delta=None,
            score_sentence="This is the root cycle, so there is no earlier house view to compare.",
            previous_market_regime=None,
            current_market_regime=_market_regime_label(current_pack),
            market_changed=False,
            market_sentence="No previous cycle is available for a market-regime comparison yet.",
            previous_turkey_regime=None,
            current_turkey_regime=_turkey_regime_label(current_pack),
            turkey_changed=False,
            turkey_sentence=(
                "No previous cycle is available for a Turkey policy/reserve comparison yet."
            ),
            added_specialists=[],
            removed_specialists=[],
            specialist_sentence=(
                "Specialist changes will appear once this root cycle has follow-ups."
            ),
            previous_watch_triggers=[],
            current_watch_triggers=current_watch_triggers,
            added_watch_triggers=[],
            removed_watch_triggers=[],
            watch_trigger_sentence=(
                "Watch-trigger changes will appear once there is a previous cycle."
            ),
            summary=(
                "This is the first cycle in its chain, so there is no prior snapshot to diff "
                "against."
            ),
        )

    previous_pack = load_evidence_pack(parent_cycle) or {}
    current_house_view = latest_house_view or _latest_house_view(session, cycle.id)
    previous_house_view = _latest_house_view(session, parent_cycle.id)
    current_outputs = round4_outputs or _round4_outputs(session, cycle.id)
    previous_outputs = _round4_outputs(session, parent_cycle.id)
    current_activations = activations or _activations(session, cycle.id)
    previous_activations = _activations(session, parent_cycle.id)

    previous_score = (
        previous_house_view.house_primary_score if previous_house_view is not None else None
    )
    current_score = (
        current_house_view.house_primary_score if current_house_view is not None else None
    )
    score_delta = None
    if previous_score is not None and current_score is not None:
        score_delta = round(current_score - previous_score, 1)

    previous_market_regime = _market_regime_label(previous_pack)
    current_market_regime = _market_regime_label(current_pack)
    market_changed = (
        previous_market_regime is not None
        and current_market_regime is not None
        and previous_market_regime != current_market_regime
    )

    previous_turkey_regime = _turkey_regime_label(previous_pack)
    current_turkey_regime = _turkey_regime_label(current_pack)
    turkey_changed = (
        previous_turkey_regime is not None
        and current_turkey_regime is not None
        and previous_turkey_regime != current_turkey_regime
    )

    previous_specialists = {
        activation.specialist_name
        for activation in previous_activations
        if activation.specialist_name
    }
    current_specialists = {
        activation.specialist_name
        for activation in current_activations
        if activation.specialist_name
    }
    added_specialists = sorted(current_specialists - previous_specialists)
    removed_specialists = sorted(previous_specialists - current_specialists)

    previous_watch_triggers = collect_watch_triggers(previous_outputs)
    current_watch_triggers = collect_watch_triggers(current_outputs)
    previous_trigger_set = set(previous_watch_triggers)
    current_trigger_set = set(current_watch_triggers)
    added_watch_triggers = [
        trigger for trigger in current_watch_triggers if trigger not in previous_trigger_set
    ]
    removed_watch_triggers = [
        trigger for trigger in previous_watch_triggers if trigger not in current_trigger_set
    ]

    score_sentence = _score_sentence(previous_score, current_score, score_delta)
    market_sentence = _regime_sentence(
        "Market regime",
        previous_market_regime,
        current_market_regime,
    )
    turkey_sentence = _regime_sentence(
        "Turkey policy/reserve layer",
        previous_turkey_regime,
        current_turkey_regime,
    )
    specialist_sentence = _specialist_sentence(
        current_specialists,
        added_specialists,
        removed_specialists,
    )
    watch_trigger_sentence = _watch_trigger_sentence(
        current_watch_triggers,
        added_watch_triggers,
        removed_watch_triggers,
    )

    summary = " ".join(
        [
            score_sentence,
            market_sentence,
            turkey_sentence,
            specialist_sentence,
            watch_trigger_sentence,
        ]
    )

    return CycleDeltaSummary(
        parent_cycle=parent_cycle,
        previous_score=previous_score,
        current_score=current_score,
        score_delta=score_delta,
        score_sentence=score_sentence,
        previous_market_regime=previous_market_regime,
        current_market_regime=current_market_regime,
        market_changed=market_changed,
        market_sentence=market_sentence,
        previous_turkey_regime=previous_turkey_regime,
        current_turkey_regime=current_turkey_regime,
        turkey_changed=turkey_changed,
        turkey_sentence=turkey_sentence,
        added_specialists=added_specialists,
        removed_specialists=removed_specialists,
        specialist_sentence=specialist_sentence,
        previous_watch_triggers=previous_watch_triggers,
        current_watch_triggers=current_watch_triggers,
        added_watch_triggers=added_watch_triggers,
        removed_watch_triggers=removed_watch_triggers,
        watch_trigger_sentence=watch_trigger_sentence,
        summary=summary,
    )


def _latest_house_view(session: Session, cycle_id: int) -> HouseView | None:
    return session.scalar(
        select(HouseView)
        .where(HouseView.cycle_id == cycle_id)
        .order_by(HouseView.created_at.desc(), HouseView.id.desc())
        .limit(1)
    )


def _round4_outputs(session: Session, cycle_id: int) -> list[AgentRoundOutput]:
    return list(
        session.scalars(
            select(AgentRoundOutput)
            .where(
                AgentRoundOutput.cycle_id == cycle_id,
                AgentRoundOutput.round_name == "round4",
            )
            .order_by(AgentRoundOutput.created_at.asc(), AgentRoundOutput.id.asc())
        ).all()
    )


def _activations(session: Session, cycle_id: int) -> list[CycleSpecialistActivation]:
    return list(
        session.scalars(
            select(CycleSpecialistActivation)
            .where(CycleSpecialistActivation.cycle_id == cycle_id)
            .order_by(
                CycleSpecialistActivation.activated_at.asc(),
                CycleSpecialistActivation.id.asc(),
            )
        ).all()
    )


def collect_watch_triggers(outputs: list[AgentRoundOutput]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for output in outputs:
        for trigger in output.watch_triggers or []:
            normalized = str(trigger).strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            ordered.append(normalized)
    return ordered


def _market_regime_label(evidence_pack: dict) -> str | None:
    return (((evidence_pack.get("price_summary") or {}).get("market_regime")) or {}).get(
        "regime_label"
    )


def _turkey_regime_label(evidence_pack: dict) -> str | None:
    return (
        (((evidence_pack.get("macro_summary") or {}).get("turkey_policy_reserves")) or {}).get(
            "regime_label"
        )
    )


def _score_sentence(
    previous_score: float | None,
    current_score: float | None,
    score_delta: float | None,
) -> str:
    if previous_score is None and current_score is None:
        return "No stored house view is available on either cycle yet."
    if previous_score is None and current_score is not None:
        return f"House view is now {current_score:.1f}; the prior cycle had no stored score."
    if previous_score is not None and current_score is None:
        return (
            f"Current cycle has no house view yet; the prior cycle closed at {previous_score:.1f}."
        )
    if score_delta is None:
        return f"House view is {current_score:.1f}."
    if abs(score_delta) < 0.1:
        return f"House view held at {current_score:.1f} versus the prior cycle."
    direction = "rose" if score_delta > 0 else "fell"
    return (
        f"House view {direction} from {previous_score:.1f} to {current_score:.1f} "
        f"({score_delta:+.1f})."
    )


def _regime_sentence(
    label: str,
    previous_value: str | None,
    current_value: str | None,
) -> str:
    if previous_value is None and current_value is None:
        return f"{label} is not stored on either cycle yet."
    if previous_value is None and current_value is not None:
        return f"{label} is now {current_value}; the prior cycle had no stored label."
    if previous_value is not None and current_value is None:
        return f"{label} is missing now; the prior cycle read {previous_value}."
    if previous_value == current_value:
        return f"{label} held at {current_value}."
    return f"{label} moved from {previous_value} to {current_value}."


def _specialist_sentence(
    current_specialists: set[str],
    added_specialists: list[str],
    removed_specialists: list[str],
) -> str:
    if not current_specialists and not added_specialists and not removed_specialists:
        return "No on-call specialists were active in either cycle."
    if not added_specialists and not removed_specialists:
        held = ", ".join(sorted(current_specialists)) if current_specialists else "none"
        return f"Specialist mix is unchanged: {held}."
    parts: list[str] = []
    if added_specialists:
        parts.append("added " + ", ".join(added_specialists))
    if removed_specialists:
        parts.append("removed " + ", ".join(removed_specialists))
    return "Specialist mix changed: " + "; ".join(parts) + "."


def _watch_trigger_sentence(
    current_watch_triggers: list[str],
    added_watch_triggers: list[str],
    removed_watch_triggers: list[str],
) -> str:
    if not current_watch_triggers and not added_watch_triggers and not removed_watch_triggers:
        return "No Round 4 watch triggers are stored yet."
    if not added_watch_triggers and not removed_watch_triggers:
        return "Watch triggers are unchanged: " + ", ".join(current_watch_triggers) + "."
    parts: list[str] = []
    if added_watch_triggers:
        parts.append("new: " + ", ".join(added_watch_triggers))
    if removed_watch_triggers:
        parts.append("cleared: " + ", ".join(removed_watch_triggers))
    return "Watch triggers shifted: " + "; ".join(parts) + "."
