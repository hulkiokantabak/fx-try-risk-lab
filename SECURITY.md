# Security Policy

## Supported version

Security fixes apply to the current `main` branch and the site deployed from it.
Archived code under `legacy/` is retained for reference and is not supported.

## Report a vulnerability privately

Please do not open a public issue for a suspected vulnerability. Use the
repository's **Security → Report a vulnerability** flow to create a private
security advisory:

<https://github.com/hulkiokantabak/fx-try-risk-lab/security/advisories/new>

Include the affected URL or commit, reproduction steps, expected impact, and a
safe proof of concept if one is available. Non-sensitive hardening suggestions
can use a normal issue. Do not include credentials, personal data, or weaponized
payloads in an issue.

## Security boundaries

The supported product is a static GitHub Pages application. It has no
application server, login, payment flow, or first-party user database. Personal
notes remain in the user's browser local storage and are never transmitted by
the application.

The principal risks are therefore:

- malicious or malformed content arriving from public data feeds;
- compromised refresh or deployment automation;
- client-side injection through rendered headlines, URLs, or snapshot fields;
- misleading data caused by stale, partial, or poisoned upstream observations;
- disclosure of secrets accidentally committed to the public repository; and
- dependency or GitHub Action supply-chain compromise.

The application renders untrusted text with DOM text nodes rather than raw
HTML. It loads no third-party runtime JavaScript; its Content Security Policy
restricts scripts and network connections to the site origin. The bundle
validator rejects dangerous JavaScript sinks and semantically invalid public
snapshots. CodeQL and Dependabot provide additional automated coverage.

## Data integrity is part of security

A valid response from an upstream server is not automatically trustworthy.
Each production source should have explicit provenance, freshness, minimum-data
requirements, and degradation behavior. Cached values must be identified as
cached; unavailable values must never silently become a neutral observation.
The latest snapshot, history, source-health metadata, and forecast ledger are
validated together before deployment.

Refresh transport is restricted to HTTPS and an explicit institutional-host
allowlist, including redirects and links discovered in upstream pages. Response
sizes, ZIP expansion, member counts, compression ratios, XML declarations,
worksheet dimensions, feed fields, and dates are bounded before publication.
Last-good cache payloads are revalidated against their recorded SHA-256 digest
before use. This digest detects corruption or an inconsistent checkout; it is
not a signature and cannot defend against an attacker who can rewrite both the
payload and digest in the trusted branch.

Forecast records are intended to be append-only. Realized outcomes may be added
when their evaluation windows close, but historical forecasts must not be
rewritten to improve apparent performance. Model versions and event definitions
must remain sufficient to reconstruct what was forecast.

Forecast identity digests are content addresses, not signatures. They detect
inconsistent or partial edits and bind the public snapshot to its issuance, but
repository history, access control, and review remain the authenticity boundary
against an attacker with trusted-branch write access.

## Secrets and automation

The application does not need browser-visible API keys. Never place secrets in
JavaScript, JSON under `docs/`, workflow files, fixtures, or commit history.
GitHub Actions use least-privilege job permissions, bounded timeouts, concurrency
controls, explicit checkout refs, and full-commit pins for every third-party
Action. Dependabot tracks the human-readable major versions recorded beside
those pins. The refresh job alone receives temporary `contents: write`;
deployment receives only Pages and OIDC permissions.

If a secret is exposed, revoke or rotate it first, then remove it from the
repository and assess the history. Removing it in a later commit is not enough.

## Out of scope

The following are not security vulnerabilities by themselves:

- disagreement with the published financial judgment;
- ordinary public-feed delay that is accurately disclosed;
- denial of service against an upstream public provider; or
- issues that require modification of the user's own browser storage or local
  files without crossing a trust boundary.

Data-integrity bugs that suppress a warning, falsely label stale data as live,
or alter a forecast record are in scope even when they do not execute code.

## Disclosure

Please allow the maintainer time to reproduce, fix, and deploy an issue before
publishing details. Credit will be given when requested and appropriate.
