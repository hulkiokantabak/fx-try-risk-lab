# FX Experts Build Guidelines

## Objective

Build the TRY risk tool as a polished local-first research application that is also ready for later deployment through a web link.

## Product Shape

Use:
- one Python web app,
- one browser UI,
- one local database,
- local artifact storage for evidence packs and reports.

Avoid a split frontend/backend architecture in `v1`.

## Preferred Stack

- `Python`
- `FastAPI`
- server-rendered HTML templates
- `HTMX`
- `SQLite`
- local filesystem storage

This keeps the application simple locally and easy to deploy later.

## Storage Rules

Use `SQLite` for:
- prices,
- macro data,
- headlines,
- chatter,
- assessment cycles,
- agent outputs,
- house views,
- realized outcomes.

Use the filesystem for:
- raw payload snapshots,
- frozen evidence packs,
- rendered reports,
- PDF exports,
- optional DOCX exports.

Use Git for:
- code,
- templates,
- prompts,
- specs,
- methodology,
- optionally selected sanitized reports.

Do not use Git as the live analytical database.

## Source Strategy

Use a tiered model:

### Tier 1

Official and structured sources such as:
- central banks,
- official statistics,
- multilateral organizations,
- structured market data feeds where publicly accessible.

### Tier 2

Reputable market and news sources such as:
- established financial news,
- official press releases,
- structured feeds,
- attributed commentary.

### Tier 3

Social and market chatter for:
- awareness,
- trigger detection,
- rumor monitoring.

Tier 3 must not outrank Tier 1 or market-implied evidence.

## How Experts Get Current Information

Experts should not browse live ad hoc during a cycle.

Instead:
1. collectors fetch public updates,
2. data is normalized and timestamped,
3. the evidence pack builder selects relevant items,
4. specialist triggers fire from direct and material mentions,
5. the debate uses the frozen cycle snapshot.

This makes the system reproducible and backtestable.

## Main Modules

Build:
- source registry,
- ingestion engine,
- market and macro store,
- evidence pack builder,
- assessment engine,
- reporting engine,
- calibration engine.

## Report Design

The user prefers document-style outputs.

Use this report pipeline:
1. structured assessment objects
2. HTML report template
3. PDF export
4. optional DOCX export

Use a dual-path PDF strategy:
- automated HTML-to-PDF when available
- browser print-to-PDF fallback

## UI Direction

Primary screens:
- home dashboard
- new assessment
- assessment detail
- history
- calibration
- source health

The UI should feel like a serious research workstation for one user, not an admin console.

## Deployment Path

### Stage A

Local:
- run locally,
- use SQLite,
- use local file storage.

### Stage B

Private remote deployment:
- deploy same app to one server or managed container,
- keep SQLite with persistent storage initially,
- add authentication.

### Stage C

Optional growth:
- Postgres,
- Supabase or similar only if remote sync or multi-user access becomes worth it,
- object storage,
- task queue,
- frontend split only if truly needed.

## Reliability Rules

- provider failure should not kill the whole cycle by default,
- stale or missing data should reduce confidence,
- unavailable chatter should not block assessments,
- failed automated PDF export should fall back to the printable browser report.

## `v1` Defaults

- local-first single-user design
- Python monolith
- FastAPI plus server-rendered UI
- SQLite primary database
- filesystem artifact storage
- PDF-first reporting
- DOCX optional
- pluggable social chatter layer
- deploy-ready without mandatory hosted services
