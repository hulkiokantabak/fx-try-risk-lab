from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from pathlib import Path
from textwrap import wrap

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models.entities import (
    AgentRoundOutput,
    AssessmentCycle,
    CycleSpecialistActivation,
    HouseView,
    RealizedOutcome,
    Report,
)
from app.services.assessment_engine import load_evidence_pack
from app.services.cycle_delta import CycleDeltaSummary, build_cycle_delta_summary
from app.services.horizons import horizon_due_date, horizon_sort_key
from app.services.report_store import purge_cycle_reports


@dataclass(frozen=True)
class GeneratedReportBundle:
    html_report: Report
    pdf_report: Report


def generate_assessment_report(
    session: Session,
    settings: Settings,
    *,
    cycle_id: int,
) -> GeneratedReportBundle:
    cycle = session.get(AssessmentCycle, cycle_id)
    if cycle is None:
        raise ValueError(f"Assessment cycle {cycle_id} was not found.")

    house_view = session.scalar(
        select(HouseView)
        .where(HouseView.cycle_id == cycle.id)
        .order_by(HouseView.created_at.desc(), HouseView.id.desc())
        .limit(1)
    )
    if house_view is None:
        raise ValueError("Run FX Experts rounds before generating a report.")

    evidence_pack = load_evidence_pack(cycle) or {}
    activations = list(
        session.scalars(
            select(CycleSpecialistActivation)
            .where(CycleSpecialistActivation.cycle_id == cycle.id)
            .order_by(CycleSpecialistActivation.id.asc())
        ).all()
    )
    round4_outputs = list(
        session.scalars(
            select(AgentRoundOutput)
            .where(
                AgentRoundOutput.cycle_id == cycle.id,
                AgentRoundOutput.round_name == "round4",
            )
            .order_by(AgentRoundOutput.created_at.asc(), AgentRoundOutput.id.asc())
        ).all()
    )
    realized_outcomes = list(
        session.scalars(
            select(RealizedOutcome)
            .where(RealizedOutcome.cycle_id == cycle.id)
            .order_by(RealizedOutcome.id.asc())
        ).all()
    )
    realized_outcomes.sort(key=lambda item: horizon_sort_key(item.horizon))
    purge_cycle_reports(session, cycle_id=cycle.id, reports_dir=settings.reports_dir)
    delta_summary = build_cycle_delta_summary(
        session,
        cycle,
        evidence_pack=evidence_pack,
        activations=activations,
        latest_house_view=house_view,
        round4_outputs=round4_outputs,
    )

    rendered_html = _render_report_document(
        cycle=cycle,
        house_view=house_view,
        evidence_pack=evidence_pack,
        round4_outputs=round4_outputs,
        activations=activations,
        realized_outcomes=realized_outcomes,
        delta_summary=delta_summary,
    )
    rendered_pdf = _render_pdf_report_document(
        cycle=cycle,
        house_view=house_view,
        evidence_pack=evidence_pack,
        round4_outputs=round4_outputs,
        activations=activations,
        realized_outcomes=realized_outcomes,
        delta_summary=delta_summary,
    )

    html_path = settings.reports_dir / f"assessment-cycle-{cycle.id:05d}.html"
    html_path.write_text(rendered_html, encoding="utf-8")
    pdf_path = settings.reports_dir / f"assessment-cycle-{cycle.id:05d}.pdf"
    pdf_path.write_bytes(rendered_pdf)

    html_report = Report(
        cycle_id=cycle.id,
        report_type="html_assessment",
        title=f"TRY Risk Report HTML - {cycle.label}",
        file_path=str(html_path),
    )
    pdf_report = Report(
        cycle_id=cycle.id,
        report_type="pdf_assessment",
        title=f"TRY Risk Report PDF - {cycle.label}",
        file_path=str(pdf_path),
    )
    session.add_all([html_report, pdf_report])
    session.commit()
    session.refresh(html_report)
    session.refresh(pdf_report)
    return GeneratedReportBundle(html_report=html_report, pdf_report=pdf_report)


def _validated_report_path(report: Report, reports_dir: Path | None = None) -> Path:
    if not report.file_path:
        raise ValueError("Report file path is missing.")
    path = Path(report.file_path).resolve()
    if reports_dir is not None:
        allowed_root = reports_dir.resolve()
        try:
            path.relative_to(allowed_root)
        except ValueError as exc:
            raise ValueError("Report file path is outside the reports directory.") from exc
    if not path.exists():
        raise ValueError("Report file does not exist on disk.")
    return path


def load_report_html(report: Report, reports_dir: Path | None = None) -> str:
    path = _validated_report_path(report, reports_dir)
    return path.read_text(encoding="utf-8")


def load_report_bytes(report: Report, reports_dir: Path | None = None) -> bytes:
    path = _validated_report_path(report, reports_dir)
    return path.read_bytes()


def _render_report_document(
    *,
    cycle: AssessmentCycle,
    house_view: HouseView,
    evidence_pack: dict,
    round4_outputs: list[AgentRoundOutput],
    activations: list[CycleSpecialistActivation],
    realized_outcomes: list[RealizedOutcome],
    delta_summary: CycleDeltaSummary,
) -> str:
    macro_summary = evidence_pack.get("macro_summary", {})
    price_summary = evidence_pack.get("price_summary", {})
    news_summary = evidence_pack.get("news_summary", {})
    expert_readiness = evidence_pack.get("expert_readiness", {})
    action_queue = evidence_pack.get("action_queue", [])
    generated_at = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    title = escape(cycle.label)
    summary = escape(cycle.summary or "No cycle summary was stored.")
    prompt = escape(cycle.user_prompt or "No custom context was supplied.")

    curve_html = "".join(
        f"""
        <div class="metric-card {'highlight' if horizon == house_view.primary_horizon else ''}">
          <span>{escape(horizon)}</span>
          <strong>{score:.1f}</strong>
        </div>
        """
        for horizon, score in house_view.risk_curve.items()
    )
    activation_html = (
        "".join(
            f"""
            <li>
              <strong>{escape(item.specialist_name)}</strong>
              <span>{escape(item.trigger_topic)}</span>
              <p>{escape(item.materiality_reason or '')}</p>
            </li>
            """
            for item in activations
        )
        if activations
        else "<li><strong>No on-call specialists were triggered.</strong></li>"
    )
    round4_html = "".join(
        f"""
        <article class="agent-card">
          <div class="agent-head">
            <strong>{escape(output.agent_name)}</strong>
            <span>{escape(output.agent_role)}</span>
          </div>
          <p class="meta">
            {escape(output.stance or 'advisory')} |
            {_score_display(output.primary_risk_score)} |
            {escape(output.confidence or 'n/a')}
          </p>
          <p>{escape(output.content)}</p>
          {_pill_list("Top drivers", output.top_drivers)}
          {_pill_list("Watch triggers", output.watch_triggers)}
          <p><strong>Counterevidence:</strong> {escape(output.counterevidence or 'n/a')}</p>
        </article>
        """
        for output in round4_outputs
    )
    macro_html = "".join(
        f"""
        <li>
          <strong>{escape(item.get('name', 'Unknown series'))}</strong>
          <span>{escape(item.get('source_name', 'Unknown source'))}</span>
          <p>
            {escape(str(item.get('value', 'n/a')))} {escape(item.get('unit') or '')}
            on {escape(item.get('observation_date') or 'n/a')}
          </p>
        </li>
        """
        for item in macro_summary.get("key_observations", [])
    )
    rates_regime = macro_summary.get("rates_regime", {})
    rates_regime_html = ""
    if rates_regime.get("ready"):
        rates_regime_html = (
            "<p><strong>"
            + escape(rates_regime.get("regime_label", "Rates backdrop"))
            + ".</strong> "
            + escape(rates_regime.get("external_signal", ""))
            + ". "
            + escape(rates_regime.get("summary", ""))
            + "</p>"
        )
    turkey_policy = macro_summary.get("turkey_policy_reserves", {})
    turkey_policy_html = ""
    if turkey_policy.get("ready"):
        turkey_policy_html = (
            "<p><strong>"
            + escape(turkey_policy.get("regime_label", "Turkey policy and reserves"))
            + ".</strong> "
            + escape(turkey_policy.get("relative_signal", ""))
            + ". "
            + escape(turkey_policy.get("summary", ""))
            + "</p>"
        )
    readiness_html = "".join(
        f"""
        <li>
          <strong>{escape(item.get('agent_name', 'Unknown agent'))}</strong>
          <span>{escape(item.get('status', 'thin'))}</span>
          <p>
            Present: {escape(str(item.get('present_count', 0)))}/
            {escape(str(item.get('required_count', 0)))}
          </p>
          <p>Missing: {escape(', '.join(item.get('missing', [])) or 'none')}</p>
        </li>
        """
        for item in expert_readiness.get("agents", [])
    )
    action_queue_html = "".join(
        f"""
        <li>
          <strong>{escape(item.get('title', 'Unknown action'))}</strong>
          <span>
            {escape(item.get('priority', 'medium'))} |
            {escape(item.get('target_area', 'Workspace'))}
          </span>
          <p>{escape(item.get('detail', ''))}</p>
        </li>
        """
        for item in action_queue
    )
    derived_market_items = list(price_summary.get("derived_pairs", []))[:3]
    volatility_market_items = [
        item
        for item in price_summary.get("series", [])
        if item.get("category") == "volatility"
    ][:6]
    spot_market_items = [
        item for item in price_summary.get("series", []) if item.get("category") != "volatility"
    ][:2]
    market_items = derived_market_items + volatility_market_items + spot_market_items
    market_regime = price_summary.get("market_regime", {})
    market_regime_html = ""
    if market_regime.get("ready"):
        market_regime_html = (
            "<p><strong>"
            + escape(market_regime.get("regime_label", "Market regime"))
            + ".</strong> "
            + escape(market_regime.get("relative_signal", ""))
            + ". "
            + escape(market_regime.get("summary", ""))
            + "</p>"
        )
    market_html = "".join(
        f"""
        <li>
          <strong>{escape(item.get('name', 'Unknown series'))}</strong>
          <span>{escape(item.get('source_name', 'Unknown source'))}</span>
          <p>
            Close: {escape(_format_numeric(item.get('close_value'), 4))}
            | Trend: {escape(item.get('trend') or 'n/a')}
          </p>
          <p>
            Date: {escape(item.get('observed_at') or 'n/a')}
            | Change: {escape(_format_change(item.get('change_pct')))}
          </p>
        </li>
        """
        for item in market_items
    )
    headline_html = "".join(
        f"""
        <li>
          <strong>{escape(item.get('title', 'Untitled'))}</strong>
          <span>{escape(item.get('source_name', 'Unknown source'))}</span>
          <p>{escape(item.get('published_at') or item.get('posted_at') or 'n/a')}</p>
        </li>
        """
        for item in news_summary.get("recent_headlines", [])[:5]
    )
    realized_html = "".join(
        f"""
        <li>
          <strong>{escape(outcome.horizon)}</strong>
          <span>
            Due {escape(_outcome_due_label(cycle.assessment_timestamp, outcome.horizon))}
          </span>
          <p>
            Threshold: {escape(_format_change(outcome.threshold_pct))}
            | Realized: {escape(_format_change(outcome.realized_move_pct))}
          </p>
          <p>
            Outcome: {escape(_realized_status_label(outcome.event_occurred))}
            | Known on: {escape(_known_on_label(outcome.outcome_known_on))}
          </p>
        </li>
        """
        for outcome in realized_outcomes
    )
    realized_fallback = "<li><strong>No realized outcomes have been computed yet.</strong></li>"

    minority_note = (
        f"<p class='minority-note'><strong>Minority risk:</strong> "
        f"{escape(house_view.minority_risk_note)}</p>"
        if house_view.minority_risk_note
        else ""
    )
    delta_html = _delta_report_html(delta_summary)

    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>{title}</title>
    <style>
      :root {{
        color-scheme: light;
        --ink: #19211d;
        --muted: #5f6d68;
        --line: rgba(25, 33, 29, 0.12);
        --accent: #1d5c4b;
        --signal: #b76a2b;
        --paper: #fffaf1;
        --bg: #f1eadf;
      }}

      * {{
        box-sizing: border-box;
      }}

      body {{
        margin: 0;
        font-family: Georgia, "Times New Roman", serif;
        color: var(--ink);
        background: linear-gradient(180deg, var(--bg), #f7f1e6);
      }}

      main {{
        max-width: 1040px;
        margin: 0 auto;
        padding: 2.4rem;
      }}

      h1, h2, h3, p {{
        margin-top: 0;
      }}

      .hero,
      .panel,
      .agent-card {{
        border: 1px solid var(--line);
        border-radius: 24px;
        background: rgba(255, 250, 241, 0.95);
        box-shadow: 0 18px 36px rgba(25, 33, 29, 0.08);
      }}

      .hero {{
        padding: 1.8rem;
        display: grid;
        grid-template-columns: 1.6fr 1fr;
        gap: 1rem;
      }}

      .hero h1 {{
        font-size: 2.5rem;
        line-height: 1;
        margin-bottom: 0.8rem;
      }}

      .eyebrow,
      .meta span,
      .agent-head span,
      .metric-card span,
      li span {{
        display: block;
        font-size: 0.78rem;
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: var(--signal);
      }}

      .hero-side {{
        padding: 1.2rem;
        border-radius: 20px;
        background: rgba(29, 92, 75, 0.08);
      }}

      .hero-side strong {{
        display: block;
        font-size: 2.3rem;
      }}

      .grid {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 1rem;
        margin-top: 1rem;
      }}

      .panel {{
        padding: 1.4rem;
      }}

      .curve-grid,
      .pill-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(120px, 1fr));
        gap: 0.8rem;
      }}

      .curve-grid {{
        margin-top: 1rem;
      }}

      .metric-card {{
        border: 1px solid var(--line);
        border-radius: 18px;
        padding: 0.9rem 1rem;
        background: white;
      }}

      .metric-card.highlight {{
        background: rgba(29, 92, 75, 0.12);
        border-color: rgba(29, 92, 75, 0.24);
      }}

      .metric-card strong {{
        display: block;
        margin-top: 0.35rem;
        font-size: 1.6rem;
      }}

      ul {{
        padding-left: 1.15rem;
        margin: 0.8rem 0 0;
      }}

      li {{
        margin-bottom: 0.8rem;
      }}

      .minority-note {{
        margin-top: 1rem;
        color: #7e3b32;
      }}

      .agent-grid {{
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 1rem;
        margin-top: 1rem;
      }}

      .agent-card {{
        padding: 1.1rem;
      }}

      .agent-head {{
        display: flex;
        justify-content: space-between;
        gap: 1rem;
      }}

      .meta {{
        color: var(--muted);
      }}

      .pill-grid {{
        margin: 1rem 0;
      }}

      .pill {{
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 0.55rem 0.75rem;
        background: white;
        font-size: 0.92rem;
      }}

      .split {{
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 1rem;
        margin-top: 1rem;
      }}

      @media print {{
        body {{
          background: white;
        }}

        main {{
          max-width: none;
          padding: 0;
        }}

        .hero,
        .panel,
        .agent-card {{
          box-shadow: none;
          break-inside: avoid;
        }}
      }}

      @media (max-width: 860px) {{
        .hero,
        .grid,
        .split,
        .agent-grid {{
          grid-template-columns: 1fr;
        }}
      }}
    </style>
  </head>
  <body>
    <main>
      <section class="hero">
        <div>
          <p class="eyebrow">TRY Risk Report</p>
          <h1>{title}</h1>
          <p>{summary}</p>
          <p><strong>Generated:</strong> {generated_at}</p>
          <p><strong>Primary horizon:</strong> {escape(cycle.primary_horizon)}</p>
        </div>
        <div class="hero-side">
          <span class="eyebrow">House Probability</span>
          <strong>{house_view.house_primary_score:.1f}</strong>
          <p>{escape(house_view.house_confidence)} confidence</p>
          <p>Disagreement range: {house_view.disagreement_range:.1f}</p>
          <p>{'Stress flag raised' if house_view.stress_flag else 'No stress flag'}</p>
        </div>
      </section>

      <section class="grid">
        <article class="panel">
          <p class="eyebrow">Context</p>
          <h2>User prompt</h2>
          <p>{prompt}</p>
          <h3>Specialist activation</h3>
          <ul>{activation_html}</ul>
        </article>
        <article class="panel">
          <p class="eyebrow">House Curve</p>
          <h2>Event probability by horizon</h2>
          <div class="curve-grid">{curve_html}</div>
          {minority_note}
        </article>
      </section>

      {delta_html}

      <section class="panel" style="margin-top: 1rem;">
        <p class="eyebrow">Round 4</p>
        <h2>Final core-agent verdicts</h2>
        <div class="agent-grid">{round4_html}</div>
      </section>

      <section class="panel" style="margin-top: 1rem;">
        <p class="eyebrow">Readiness</p>
        <h2>Expert coverage before debate</h2>
        <ul>{readiness_html or '<li><strong>No readiness summary stored.</strong></li>'}</ul>
      </section>

      <section class="panel" style="margin-top: 1rem;">
        <p class="eyebrow">Setup Queue</p>
        <h2>Next setup actions</h2>
        <ul>{action_queue_html or '<li><strong>No urgent setup actions.</strong></li>'}</ul>
      </section>

      <section class="split">
        <article class="panel">
          <p class="eyebrow">Round 0</p>
          <h2>Macro snapshot</h2>
          <p>
            Coverage:
            {escape(str(macro_summary.get('series_with_observations', 0)))}/
            {escape(str(macro_summary.get('configured_series', 0)))} series
          </p>
          {rates_regime_html}
          {turkey_policy_html}
          <ul>{macro_html or '<li><strong>No macro observations stored.</strong></li>'}</ul>
        </article>
        <article class="panel">
          <p class="eyebrow">Round 0</p>
          <h2>Market snapshot</h2>
          <p>
            Coverage:
            {escape(str(price_summary.get('series_with_observations', 0)))}/
            {escape(str(price_summary.get('configured_series', 0)))} series
          </p>
          {market_regime_html}
          <ul>{market_html or '<li><strong>No market observations stored.</strong></li>'}</ul>
        </article>
      </section>

      <section class="panel" style="margin-top: 1rem;">
        <article>
          <p class="eyebrow">Recent Evidence</p>
          <h2>Headline context</h2>
          <p>
            Headlines in 14d: {escape(str(news_summary.get('headline_count_14d', 0)))} |
            Chatter in 14d: {escape(str(news_summary.get('chatter_count_14d', 0)))}
          </p>
          <ul>{headline_html or '<li><strong>No recent headlines stored.</strong></li>'}</ul>
        </article>
      </section>

      <section class="panel" style="margin-top: 1rem;">
        <article>
          <p class="eyebrow">Backtesting</p>
          <h2>Realized outcomes to date</h2>
          <ul>{realized_html or realized_fallback}</ul>
        </article>
      </section>
    </main>
  </body>
</html>
"""


def _render_pdf_report_document(
    *,
    cycle: AssessmentCycle,
    house_view: HouseView,
    evidence_pack: dict,
    round4_outputs: list[AgentRoundOutput],
    activations: list[CycleSpecialistActivation],
    realized_outcomes: list[RealizedOutcome],
    delta_summary: CycleDeltaSummary,
) -> bytes:
    lines = _pdf_report_lines(
        cycle=cycle,
        house_view=house_view,
        evidence_pack=evidence_pack,
        round4_outputs=round4_outputs,
        activations=activations,
        realized_outcomes=realized_outcomes,
        delta_summary=delta_summary,
    )
    return _build_simple_pdf(lines)


def _pdf_report_lines(
    *,
    cycle: AssessmentCycle,
    house_view: HouseView,
    evidence_pack: dict,
    round4_outputs: list[AgentRoundOutput],
    activations: list[CycleSpecialistActivation],
    realized_outcomes: list[RealizedOutcome],
    delta_summary: CycleDeltaSummary,
) -> list[str]:
    macro_summary = evidence_pack.get("macro_summary", {})
    price_summary = evidence_pack.get("price_summary", {})
    news_summary = evidence_pack.get("news_summary", {})
    expert_readiness = evidence_pack.get("expert_readiness", {})
    action_queue = evidence_pack.get("action_queue", [])
    lines: list[str] = []

    def add_heading(title: str) -> None:
        if lines and lines[-1] != "":
            lines.append("")
        lines.append(title)
        lines.append("=" * min(len(title), 48))

    def add_bullet(text: str) -> None:
        wrapped = wrap(text, width=92) or [text]
        for index, line in enumerate(wrapped):
            prefix = "- " if index == 0 else "  "
            lines.append(prefix + line)

    add_heading("TRY Risk Report")
    lines.append(cycle.label)
    lines.append(f"Generated: {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Primary horizon: {cycle.primary_horizon}")
    lines.append(f"House probability: {house_view.house_primary_score:.1f}")
    lines.append(f"House confidence: {house_view.house_confidence}")
    lines.append(f"Disagreement range: {house_view.disagreement_range:.1f}")
    lines.append("Stress flag: raised" if house_view.stress_flag else "Stress flag: not raised")
    if cycle.summary:
        lines.append(f"Cycle summary: {cycle.summary}")
    if cycle.user_prompt:
        lines.append(f"User prompt: {cycle.user_prompt}")

    add_heading("House Curve")
    for horizon, score in house_view.risk_curve.items():
        lines.append(f"{horizon}: {score:.1f}")
    if house_view.minority_risk_note:
        add_bullet(f"Minority risk note: {house_view.minority_risk_note}")

    if delta_summary.parent_cycle is not None:
        add_heading("What Changed Since Last Cycle")
        add_bullet(
            f"Parent cycle: #{delta_summary.parent_cycle.id} | "
            f"{delta_summary.parent_cycle.label}"
        )
        add_bullet(delta_summary.score_sentence)
        add_bullet(delta_summary.market_sentence)
        add_bullet(delta_summary.turkey_sentence)
        add_bullet(delta_summary.specialist_sentence)
        add_bullet(delta_summary.watch_trigger_sentence)

    add_heading("Specialist Activation")
    if activations:
        for item in activations:
            add_bullet(
                f"{item.specialist_name} ({item.trigger_topic}): "
                f"{item.materiality_reason or 'no reason stored'}"
            )
    else:
        add_bullet("No on-call specialists were triggered.")

    add_heading("Round 4 Final Core-Agent Verdicts")
    if round4_outputs:
        for output in round4_outputs:
            add_bullet(
                f"{output.agent_name} | {output.agent_role} | "
                f"{output.stance or 'advisory'} | "
                f"{_score_display(output.primary_risk_score)} | "
                f"{output.confidence or 'n/a'}"
            )
            if output.content:
                add_bullet(output.content)
            if output.top_drivers:
                add_bullet("Top drivers: " + ", ".join(str(item) for item in output.top_drivers))
            if output.watch_triggers:
                add_bullet(
                    "Watch triggers: " + ", ".join(str(item) for item in output.watch_triggers)
                )
            if output.counterevidence:
                add_bullet("Counterevidence: " + output.counterevidence)
    else:
        add_bullet("No Round 4 outputs were stored.")

    add_heading("Expert Coverage Before Debate")
    for item in expert_readiness.get("agents", []):
        missing = ", ".join(item.get("missing", [])) or "none"
        add_bullet(
            f"{item.get('agent_name', 'Unknown agent')} | "
            f"{item.get('status', 'thin')} | "
            f"present {item.get('present_count', 0)}/{item.get('required_count', 0)} | "
            f"missing: {missing}"
        )

    add_heading("Next Setup Actions")
    if action_queue:
        for item in action_queue:
            add_bullet(
                f"{item.get('priority', 'medium').upper()} | "
                f"{item.get('target_area', 'Workspace')} | "
                f"{item.get('title', 'Unknown action')}: {item.get('detail', '')}"
            )
    else:
        add_bullet("No urgent setup actions are queued.")

    add_heading("Macro Snapshot")
    lines.append(
        f"Coverage: {macro_summary.get('series_with_observations', 0)}/"
        f"{macro_summary.get('configured_series', 0)} series"
    )
    rates_regime = macro_summary.get("rates_regime", {})
    if rates_regime.get("ready"):
        add_bullet(
            f"{rates_regime.get('regime_label', 'Rates backdrop')}: "
            f"{rates_regime.get('summary', '')}"
        )
    turkey_policy = macro_summary.get("turkey_policy_reserves", {})
    if turkey_policy.get("ready"):
        add_bullet(
            f"{turkey_policy.get('regime_label', 'Turkey policy/reserves')}: "
            f"{turkey_policy.get('summary', '')}"
        )
    for item in macro_summary.get("key_observations", []):
        add_bullet(
            f"{item.get('name', 'Unknown series')} | "
            f"{item.get('source_name', 'Unknown source')} | "
            f"{item.get('value', 'n/a')} {item.get('unit') or ''} | "
            f"{item.get('observation_date', 'n/a')}"
        )

    add_heading("Market Snapshot")
    lines.append(
        f"Coverage: {price_summary.get('series_with_observations', 0)}/"
        f"{price_summary.get('configured_series', 0)} series"
    )
    market_regime = price_summary.get("market_regime", {})
    if market_regime.get("ready"):
        add_bullet(
            f"{market_regime.get('regime_label', 'Market regime')}: "
            f"{market_regime.get('summary', '')}"
        )
    for item in list(price_summary.get("derived_pairs", []))[:4]:
        add_bullet(
            f"{item.get('name', 'Unknown series')} | close "
            f"{_format_numeric(item.get('close_value'), 4)} | "
            f"change {_format_change(item.get('change_pct'))}"
        )
    volatility_items = [
        item
        for item in price_summary.get("series", [])
        if item.get("category") == "volatility"
    ][:6]
    spot_items = [
        item
        for item in price_summary.get("series", [])
        if item.get("category") != "volatility"
    ][:2]
    for item in volatility_items + spot_items:
        add_bullet(
            f"{item.get('name', 'Unknown series')} | close "
            f"{_format_numeric(item.get('close_value'), 4)} | "
            f"change {_format_change(item.get('change_pct'))}"
        )

    add_heading("Headline Context")
    lines.append(
        f"Headlines in 14d: {news_summary.get('headline_count_14d', 0)} | "
        f"Chatter in 14d: {news_summary.get('chatter_count_14d', 0)}"
    )
    for item in news_summary.get("recent_headlines", [])[:5]:
        add_bullet(
            f"{item.get('source_name', 'Unknown source')} | "
            f"{item.get('published_at', 'n/a')} | "
            f"{item.get('title', 'Untitled')}"
        )

    add_heading("Realized Outcomes To Date")
    if realized_outcomes:
        for outcome in realized_outcomes:
            add_bullet(
                f"{outcome.horizon} | threshold {_format_change(outcome.threshold_pct)} | "
                f"realized {_format_change(outcome.realized_move_pct)} | "
                f"outcome {_realized_status_label(outcome.event_occurred)} | "
                f"known {_known_on_label(outcome.outcome_known_on)}"
            )
    else:
        add_bullet("No realized outcomes have been computed yet.")

    return lines


def _build_simple_pdf(lines: list[str]) -> bytes:
    page_lines = 48
    safe_lines = [_pdf_text(line) for line in lines] or ["TRY Risk Report", "No report data."]
    pages = [
        safe_lines[index : index + page_lines]
        for index in range(0, len(safe_lines), page_lines)
    ] or [["TRY Risk Report", "No report data."]]

    objects: dict[int, bytes] = {
        1: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    next_object_id = 2
    page_ids: list[int] = []
    content_ids: list[int] = []
    pages_object_id = 2 + len(pages) * 2
    catalog_object_id = pages_object_id + 1

    for page in pages:
        content_id = next_object_id
        page_id = next_object_id + 1
        next_object_id += 2
        content_stream = _pdf_page_stream(page)
        objects[content_id] = (
            f"<< /Length {len(content_stream)} >>\nstream\n".encode("latin-1")
            + content_stream
            + b"\nendstream"
        )
        objects[page_id] = (
            f"<< /Type /Page /Parent {pages_object_id} 0 R "
            f"/MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 1 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("latin-1")
        content_ids.append(content_id)
        page_ids.append(page_id)

    page_refs = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    objects[pages_object_id] = (
        f"<< /Type /Pages /Count {len(page_ids)} /Kids [{page_refs}] >>"
    ).encode("latin-1")
    objects[catalog_object_id] = (
        f"<< /Type /Catalog /Pages {pages_object_id} 0 R >>"
    ).encode("latin-1")

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {0: 0}
    for object_id in range(1, catalog_object_id + 1):
        offsets[object_id] = len(pdf)
        pdf.extend(f"{object_id} 0 obj\n".encode("latin-1"))
        pdf.extend(objects[object_id])
        pdf.extend(b"\nendobj\n")

    xref_offset = len(pdf)
    pdf.extend(f"xref\n0 {catalog_object_id + 1}\n".encode("latin-1"))
    pdf.extend(b"0000000000 65535 f \n")
    for object_id in range(1, catalog_object_id + 1):
        pdf.extend(f"{offsets[object_id]:010} 00000 n \n".encode("latin-1"))
    pdf.extend(
        (
            f"trailer << /Size {catalog_object_id + 1} /Root {catalog_object_id} 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF"
        ).encode("latin-1")
    )
    return bytes(pdf)


def _delta_report_html(delta_summary: CycleDeltaSummary) -> str:
    if delta_summary.parent_cycle is None:
        return ""

    score_headline = (
        f"{delta_summary.score_delta:+.1f}"
        if delta_summary.score_delta is not None
        else _score_display(delta_summary.current_score)
    )
    specialist_headline = (
        f"+{len(delta_summary.added_specialists)} / -{len(delta_summary.removed_specialists)}"
        if delta_summary.added_specialists or delta_summary.removed_specialists
        else "unchanged"
    )
    watch_headline = (
        f"+{len(delta_summary.added_watch_triggers)} / -{len(delta_summary.removed_watch_triggers)}"
        if delta_summary.added_watch_triggers or delta_summary.removed_watch_triggers
        else (
            str(len(delta_summary.current_watch_triggers))
            if delta_summary.current_watch_triggers
            else "n/a"
        )
    )
    watch_pills = _pill_list("Current watch triggers", delta_summary.current_watch_triggers)

    return f"""
      <section class="panel" style="margin-top: 1rem;">
        <p class="eyebrow">Follow-up Delta</p>
        <h2>What changed since last cycle</h2>
        <p>
          <strong>Against cycle #{delta_summary.parent_cycle.id}.</strong>
          {escape(delta_summary.summary)}
        </p>
        <div class="curve-grid">
          <div class="metric-card highlight">
            <span>House View</span>
            <strong>{escape(score_headline)}</strong>
            <p>{escape(delta_summary.score_sentence)}</p>
          </div>
          <div class="metric-card">
            <span>Market Regime</span>
            <strong>{escape(delta_summary.current_market_regime or 'n/a')}</strong>
            <p>{escape(delta_summary.market_sentence)}</p>
          </div>
          <div class="metric-card">
            <span>Turkey Layer</span>
            <strong>{escape(delta_summary.current_turkey_regime or 'n/a')}</strong>
            <p>{escape(delta_summary.turkey_sentence)}</p>
          </div>
          <div class="metric-card">
            <span>Specialists</span>
            <strong>{escape(specialist_headline)}</strong>
            <p>{escape(delta_summary.specialist_sentence)}</p>
          </div>
          <div class="metric-card">
            <span>Watch Triggers</span>
            <strong>{escape(watch_headline)}</strong>
            <p>{escape(delta_summary.watch_trigger_sentence)}</p>
          </div>
        </div>
        {watch_pills}
      </section>
    """


def _pdf_page_stream(lines: list[str]) -> bytes:
    stream_lines = [
        "BT",
        "/F1 11 Tf",
        "50 742 Td",
        "14 TL",
    ]
    for index, line in enumerate(lines):
        if index:
            stream_lines.append("T*")
        stream_lines.append(f"({_pdf_escape(line)}) Tj")
    stream_lines.append("ET")
    return "\n".join(stream_lines).encode("latin-1", errors="replace")


def _pdf_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value)
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    return ascii_text if ascii_text.strip() else " "


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _pill_list(title: str, values: list | None) -> str:
    if not values:
        return ""
    pills = "".join(f"<span class='pill'>{escape(str(value))}</span>" for value in values)
    return f"<strong>{escape(title)}:</strong><div class='pill-grid'>{pills}</div>"


def _score_display(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f}"


def _format_numeric(value: float | None, precision: int) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{precision}f}"


def _format_change(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.3f}%"


def _realized_status_label(value: bool | None) -> str:
    if value is None:
        return "pending"
    return "triggered" if value else "cleared"


def _outcome_due_label(observed_at: datetime, horizon: str) -> str:
    return horizon_due_date(observed_at, horizon).strftime("%Y-%m-%d")


def _known_on_label(value: datetime | None) -> str:
    if value is None:
        return "pending"
    return value.strftime("%Y-%m-%d")
