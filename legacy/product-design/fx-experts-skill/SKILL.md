---
name: fx-experts
description: Coordinate a four-agent FX debate team plus four on-call specialists to assess Turkish lira depreciation risk, preserve evidence-backed dissent, produce probability-based risk curves, and guide implementation of the local-first TRY risk research tool.
---

# FX Experts

Use this skill when the task involves:
- defining or running the TRY depreciation-risk expert workflow,
- prompting or coordinating the `Atlas`, `Bosphorus`, `Flow`, and `Vega` agents,
- activating or refining the on-call specialists,
- building the evidence pack, round orchestration, risk math, or reporting logic for the FX tool,
- reviewing whether a proposed change still matches the agreed expert process.

## Quick Start

1. Read [references/operating-spec.md](references/operating-spec.md) for the expert-team rules, score semantics, debate flow, and house-view logic.
2. Read [references/build-guidelines.md](references/build-guidelines.md) when the task touches architecture, data ingestion, storage, UI, reporting, or deployment.
3. Default to the `4` core agents.
4. Activate on-call specialists only on direct and material mentions tied to the thesis.
5. Preserve dissent. Do not force consensus when disagreement is informative.

## Core Working Rules

- Treat `risk_curve` values as true event probabilities, not generic sentiment scores.
- Always produce the full horizon curve and highlight one `primary_horizon`.
- Keep social chatter below official, policy, and market-implied evidence in the hierarchy.
- Use the frozen evidence pack for one assessment cycle; do not mix in ad hoc facts mid-round unless they are explicitly added as new evidence.
- Debate should be sharp, personality-driven, and evidence-backed. Empty rhetoric is a process failure.
- Codex is the coordinator and synthesizer, not a fifth voting expert.

## Output Expectations

When running or simulating an assessment, preserve:
- each round's outputs,
- each core agent's final view,
- specialist overlays if activated,
- the house curve,
- the primary-horizon house score,
- disagreement range,
- minority-risk note,
- stress flag,
- watch triggers.

## References

- [references/operating-spec.md](references/operating-spec.md): team design, rounds, debate charter, score rules, and archival expectations.
- [references/build-guidelines.md](references/build-guidelines.md): local-first deploy-ready app architecture, data-source strategy, storage, UI, PDF reporting, and deployment path.
