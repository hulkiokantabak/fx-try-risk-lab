# FX Implementation Blueprint

## Objective

This blueprint turns the `FX Experts` spec into a buildable system.

The target for `v1` is:
- excellent local use,
- a clean browser-based interface,
- no mandatory hosted database,
- PDF-first reporting,
- a straightforward path to later deployment through a web link.

The design principle is `local-first, deploy-ready`.

## Product Shape

`v1` should be a single web application that runs locally on the user's machine and can later be deployed with minimal architectural change.

Recommended product shape:
- one Python application,
- one browser UI,
- one local database file,
- one local storage area for evidence packs and generated reports.

This avoids unnecessary moving parts while still giving the user a polished interface.

## Core Architecture

Recommended `v1` stack:
- `Python` backend,
- `FastAPI` application server,
- server-rendered HTML templates for the UI,
- `HTMX` for lightweight interactivity,
- `SQLite` for structured storage,
- local filesystem storage for evidence packs, report assets, and exported PDFs,
- optional `APScheduler` or equivalent later for recurring runs.

Why this stack:
- Python is well suited for data collection, normalization, scoring, and report generation.
- FastAPI is easy to run locally and easy to deploy later.
- Server-rendered UI avoids the complexity of a separate frontend build system.
- HTMX keeps the interface responsive without forcing a full JavaScript SPA architecture.
- SQLite is simple, portable, reliable, and sufficient for a single-user research engine.

## Architecture Principles

### 1. Single-App First

Use one application for:
- ingestion,
- evidence assembly,
- assessment orchestration,
- report generation,
- UI rendering.

Do not split frontend and backend in `v1`.
That would add complexity without adding meaningful value at this stage.

### 2. Database Is Local, Not Hosted

Use `SQLite` as the primary database in `v1`.

Why:
- zero external setup,
- easy local backup,
- portable,
- deployable with a persistent volume if needed,
- sufficient for a single-user analytical tool.

### 3. Git Is For Versioning, Not Primary Data Storage

Use Git for:
- code,
- prompts,
- specs,
- templates,
- optionally published reports and selected snapshots.

Do not use Git as the main store for:
- time series,
- assessment history,
- evidence items,
- fetched headlines,
- application state.

Git is excellent for versioning artifacts, but weak for queryable analytical history.

### 4. Deployment Should Reuse The Same Codebase

The same application should support:
- local desktop use,
- private remote deployment,
- later upgrades to hosted database or object storage if needed.

The deployment path should be additive, not a rewrite.

## Why Not Supabase In `v1`

Supabase is optional, not required.

Reasons to avoid making it mandatory in `v1`:
- it adds external setup and operational dependency,
- the tool is initially single-user,
- SQLite is sufficient for research and private use,
- remote access can be achieved first with a single deployed app and persistent storage.

When Supabase would make sense later:
- multi-device syncing becomes essential,
- multiple users need access,
- authentication becomes important,
- remote database reliability matters more than local simplicity.

## System Modules

The application should be divided into the following modules.

### 1. Source Registry

Maintains the list of data providers, source types, trust levels, freshness rules, and collection methods.

Each source entry should track:
- name,
- source category,
- collection method,
- endpoint or feed location,
- required credentials if any,
- freshness expectation,
- trust tier,
- parsing adapter,
- enable or disable status.

### 2. Ingestion Engine

Fetches and normalizes data from public sources.

Responsibilities:
- fetch raw data,
- store raw payloads when useful,
- normalize into internal schemas,
- record fetch time and source metadata,
- handle retries and source failures,
- avoid duplicate entries.

### 3. Market And Macro Store

Stores normalized:
- FX prices,
- macro indicators,
- policy events,
- news items,
- chatter items,
- source metadata,
- realized outcome history.

### 4. Evidence Pack Builder

Creates the frozen evidence pack used for one assessment cycle.

Responsibilities:
- pull the latest relevant data,
- attach freshness dates,
- score source reliability,
- collect recent headlines and chatter,
- detect specialist triggers,
- prepare the package for Round 1.

### 5. Assessment Engine

Implements the `FX Experts` round logic:
- Round 0 pre-brief,
- Round 1 topic framing,
- Round 2 initial thesis,
- Round 3 challenge and revision,
- Round 4 final verdict.

Responsibilities:
- orchestrate agent prompts and responses,
- record each round separately,
- compute the house risk curve,
- apply specialist overlays,
- compute disagreement range,
- apply stress flags,
- save all cycle outputs.

### 6. Reporting Engine

Generates user-facing outputs from stored assessment data.

Primary outputs:
- browser report view,
- printable report page,
- PDF export,
- optional DOCX export.

### 7. Calibration Engine

Measures how prior assessments performed.

Responsibilities:
- compare predicted probabilities with realized USD/TRY moves,
- track score drift by horizon,
- evaluate whether round revisions improved signal quality,
- compare the value added by each core agent and specialist.

## Public Source Strategy

The system should use a tiered public-source model.

### Tier 1 - Official And Structured Sources

Highest trust tier.

Examples:
- central banks,
- official statistical agencies,
- multilateral institutions,
- structured public data APIs,
- exchange or benchmark market data where publicly accessible.

Typical use:
- macro indicators,
- policy rates,
- inflation,
- reserves,
- balance of payments,
- external debt and related statistics.

### Tier 2 - Reputable Market And News Sources

Medium trust tier.

Examples:
- established financial news organizations,
- official press releases,
- exchange bulletins,
- research commentary with clear attribution,
- RSS and structured feed sources.

Typical use:
- market-moving headlines,
- policy commentary,
- event detection,
- narrative context for the evidence pack.

### Tier 3 - Social And Market Chatter

Lower trust tier.

Examples:
- public social posts,
- financial community chatter,
- fast-moving commentary streams.

Typical use:
- early topic detection,
- sentiment shifts,
- rumor monitoring,
- specialist trigger hints.

Rules for Tier 3:
- never let chatter outrank official or market-implied evidence,
- use it as a signal source, not a truth source,
- tag it clearly in the evidence pack,
- reduce confidence when chatter is influential but unverified.

## Social Chatter Design

The system should support social chatter, but as an optional and carefully weighted layer.

`v1` design choice:
- chatter ingestion should be pluggable,
- the rest of the system must work even if chatter is unavailable,
- every chatter item must carry source, timestamp, and trust tier,
- chatter should influence topic detection more than probability math.

Recommended practical rule:
- use chatter for `awareness and triggers`,
- use official and market-implied sources for `probability shaping`.

## How Experts Learn Current Developments

Experts do not browse the live web on their own.
They learn current developments through the ingestion and evidence system.

The design should work like this:

1. Scheduled and on-demand collectors fetch updates from enabled sources.
2. Normalized items are stored with timestamps and source metadata.
3. The evidence pack builder selects the most relevant and freshest items for the current cycle.
4. Topic detection and direct-material mention rules determine which specialists are activated.
5. Core agents debate using the frozen evidence pack, not ad hoc browsing during the round.

This keeps assessments reproducible and backtestable.

## Data Model

The `v1` database should include at least these logical tables.

### Core Tables

- `sources`
- `source_fetch_runs`
- `price_series`
- `price_observations`
- `macro_series`
- `macro_observations`
- `policy_events`
- `headlines`
- `chatter_items`
- `assessment_cycles`
- `evidence_items`
- `cycle_specialist_activations`
- `agent_round_outputs`
- `house_views`
- `reports`
- `realized_outcomes`
- `calibration_runs`

### Important Time Fields

Where applicable, store:
- `observation_date`,
- `release_date`,
- `fetch_date`,
- `ingested_at`,
- `assessment_timestamp`.

This is necessary because macro data may be revised after the fact.

## File Storage Layout

Use the filesystem for larger or user-facing artifacts.

Recommended local structure:

```text
data/
  app.db
  raw/
  normalized/
  evidence_packs/
  reports/
  exports/
  logs/
```

Purpose:
- `app.db`: SQLite database
- `raw/`: optional raw provider payload snapshots
- `normalized/`: optional intermediate artifacts
- `evidence_packs/`: frozen cycle packages
- `reports/`: rendered assessment artifacts
- `exports/`: PDF and DOCX outputs
- `logs/`: operational logs

## Assessment Cycle Flow

### Step 1 - Refresh Data

Before a cycle starts, the app should:
- run on-demand refresh for selected sources,
- reuse fresh cached data when appropriate,
- record failures without blocking the entire run.

### Step 2 - Build Evidence Pack

The builder should:
- gather latest relevant market data,
- gather latest macro and policy data,
- gather current headlines and chatter,
- tag freshness and trust,
- detect specialist triggers,
- create the frozen cycle package.

### Step 3 - Run Core Rounds

The assessment engine should:
- run Round 1 independent framing,
- run Round 2 independent theses,
- run Round 3 directed challenge and revision,
- run Round 4 final verdicts.

### Step 4 - Compute House View

The engine should:
- calculate the full risk curve,
- highlight the selected primary horizon,
- calculate house confidence,
- calculate disagreement range,
- generate minority-risk notes,
- apply stress flags,
- apply specialist overlays with caps.

### Step 5 - Generate Report

The reporting engine should produce:
- a clean browser summary,
- a detailed assessment page,
- a printable HTML report,
- a PDF export,
- optional DOCX export.

### Step 6 - Archive And Backtest

The cycle should then be stored for:
- later browsing,
- comparison to earlier assessments,
- future calibration and score review.

## Specialist Trigger Engine

Triggering should use a two-step rule:

1. `topic match`
2. `materiality check`

This avoids over-activation from weak or peripheral mentions.

Example:
- a generic mention of sanctions in a broad article should not automatically activate `Strait`,
- a mention of sanctions tied to Turkey, regional flows, banking access, trade routes, or cross-asset repricing should.

The trigger engine should use:
- keyword lists,
- source type,
- recency,
- entity matching,
- context rules,
- optional confidence scores.

## Risk Math Design

`risk_curve` values are event probabilities.
They are not sentiment scores.

The house view should be computed separately for each horizon using the fixed `v1` core-agent weights from the spec.

Additional numerical layers:
- `house_primary_score`
- `house_confidence`
- `disagreement_range`
- `stress_flag`

### Confidence Handling

Confidence should affect interpretation, not weighting.

That means:
- agent self-reported confidence is stored,
- house confidence is derived with the spec rules,
- score weights do not change because one agent sounds more certain.

### Stress Override

The app should separately compute whether a `stress_flag` is active.

This is important because averaging can hide acute nonlinear risk.

## UI Blueprint

The interface should feel like a research terminal for one user, not an admin console.

Recommended primary screens:

### 1. Home Dashboard

Shows:
- latest house view,
- highlighted primary-horizon score,
- full risk curve,
- last assessment timestamp,
- active stress flag if any,
- latest activated specialists,
- quick action to run new assessment.

### 2. New Assessment Screen

Allows the user to:
- choose a primary horizon,
- choose refresh mode,
- enable or disable chatter sources,
- add custom prompt context,
- start a new cycle.

### 3. Assessment Detail Screen

Shows:
- evidence summary,
- round-by-round outputs,
- final core-agent views,
- specialist overlays,
- house synthesis,
- disagreement range,
- minority-risk note,
- watch triggers.

### 4. History Screen

Shows:
- prior assessment cycles,
- trend of house scores by horizon,
- what changed across cycles,
- links to prior PDF reports.

### 5. Calibration Screen

Shows:
- realized outcomes,
- forecast versus reality by horizon,
- calibration error,
- whether Round 3 improved results,
- agent contribution diagnostics.

### 6. Source Health Screen

Shows:
- enabled sources,
- last successful fetch,
- freshness status,
- recent errors,
- credential needs where applicable.

## PDF-First Reporting Design

The user prefers document-style outputs.
The reporting system should therefore treat the browser report as the source layout for PDF export.

Recommended reporting pipeline:

1. Generate structured assessment objects.
2. Render them into a polished HTML report template.
3. Export the HTML report to PDF.
4. Optionally generate DOCX from the same structured objects.

### PDF Generation Strategy

Use a dual-path approach:

- primary path: automated HTML-to-PDF export,
- fallback path: browser print-to-PDF from the same report template.

Why:
- keeps report styling high-quality,
- avoids locking the design to a low-flexibility PDF library,
- preserves a graceful fallback if automatic PDF export is unavailable on a machine.

### DOCX Strategy

DOCX should be optional and secondary.

Use it for:
- editable reports,
- sharing drafts,
- manual revisions outside the app.

Do not design the core report system around DOCX.

## Local Runtime Model

`v1` should run with minimal external software:
- Python runtime,
- project dependencies,
- local browser.

Recommended developer and user experience:
- one setup step for dependencies,
- one command to start the application,
- one browser tab to use the tool.

No mandatory requirements for:
- Docker,
- hosted database,
- external queue,
- external object storage,
- separate frontend toolchain.

## Deployment Path

The app should be deployable later as a single service.

### Stage A - Local

- run app locally,
- use SQLite,
- use local file storage,
- export reports locally.

### Stage B - Private Remote Deployment

- deploy same app to a single server or managed container platform,
- keep SQLite initially with a persistent volume,
- keep report files on attached storage,
- protect access with authentication when needed.

### Stage C - Growth Upgrades

Optional future upgrades:
- move SQLite to Postgres,
- use Supabase or another hosted Postgres only if remote access and sync justify it,
- move file storage to object storage,
- add task queue if scheduled ingestion becomes heavy,
- split frontend only if user experience later demands it.

This preserves the `deploy-ready` path without forcing extra infrastructure too early.

## Web Access Through A Link

If the user later wants access from anywhere, the recommended path is:
- deploy the same application to a stable URL,
- keep the current app structure,
- use authentication,
- attach persistent storage.

This is better than designing `v1` around a hosted stack the user may not yet need.

## GitHub Strategy

The project should be Git-friendly from day one.

Recommended approach:
- keep all code, specs, prompts, templates, and configuration in Git,
- optionally keep selected exported reports in the repository or in a release process,
- do not keep the live SQLite database in Git,
- do not commit large raw ingestion payloads unless deliberately selected as fixtures.

If a public-facing project page is later useful:
- publish project documentation,
- publish sanitized sample reports,
- publish methodology notes,
- keep live analytical state outside Git.

## Configuration Strategy

The application should separate:
- code,
- source configuration,
- feature toggles,
- environment secrets,
- report templates,
- scoring thresholds.

This allows:
- local use without code edits,
- future deployment without redesign,
- configurable providers and triggers.

## Reliability And Failure Handling

The tool should degrade gracefully.

If one provider fails:
- the whole cycle should not fail by default,
- the evidence pack should mark missing or stale inputs,
- house confidence should be downgraded when material gaps exist.

If chatter is unavailable:
- continue with macro, market, and headline evidence.

If PDF auto-export fails:
- show the browser report and allow manual print-to-PDF.

## Security And Privacy

Even in `v1`, basic protections should exist.

Recommended controls:
- keep secrets out of the repository,
- use environment variables or local config files,
- store only the minimum necessary raw data,
- sanitize logs where needed,
- add authentication before remote deployment.

## Recommended Build Order

### Phase 1 - Foundation

Build:
- project skeleton,
- config system,
- SQLite schema,
- source registry,
- basic FastAPI app shell,
- base UI layout.

### Phase 2 - Data Layer

Build:
- ingestion adapters,
- normalized storage,
- evidence pack builder,
- source health tracking.

### Phase 3 - Assessment Engine

Build:
- cycle model,
- round orchestration,
- specialist trigger engine,
- house view computation,
- result persistence.

### Phase 4 - Reporting

Build:
- assessment detail page,
- printable report template,
- PDF export,
- optional DOCX export.

### Phase 5 - Calibration

Build:
- realized outcome tracking,
- historical comparison,
- calibration dashboard,
- score review tools.

### Phase 6 - Deployment Hardening

Build:
- authentication,
- deployment config,
- persistent storage support,
- optional remote database adapter.

## Default Decisions Locked For `v1`

- local-first single-user design,
- Python monolith,
- FastAPI plus server-rendered UI,
- SQLite primary database,
- filesystem artifact storage,
- PDF-first reporting,
- DOCX optional,
- public-source ingestion with tiered trust,
- social chatter supported but lower-weighted,
- Git for code and methodology, not live analytical state,
- deploy-ready structure without mandatory hosted services.

## Final Recommendation

Build `v1` as a polished local web application that feels like a serious research workstation.

Keep the architecture simple enough that:
- it works reliably on one machine,
- it can be used by a non-coder through a browser,
- it can later be deployed almost unchanged,
- it does not force Supabase or any other hosted dependency until that need is real.
