---
status: current
owner: Engineering Lead
scope: Architecture decision record index, status vocabulary, and maintenance rules.
last_verified: 2026-08-23
supersedes: []
superseded_by: null
related_adrs:
  - 0009-canonical-documentation-ownership.md
---

# Architecture decision records

ADRs preserve decisions that materially affect product behavior, data
integrity, security, compatibility, or future implementation boundaries.

## Status vocabulary

- `proposed`: under review and not authoritative;
- `accepted`: approved and normative, whether or not fully implemented;
- `deprecated`: still present but discouraged pending removal;
- `superseded`: replaced by a named later ADR;
- `rejected`: considered and not adopted.

An accepted ADR states a decision, not implementation completion. The current
implementation is documented separately in
[`Current Delivery State`](../engineering/current-delivery-state.md).

Accepted ADRs are not rewritten to change the original decision. Clarifications
that do not change meaning may be added with a dated note. A changed decision
requires a new ADR and reciprocal supersession links.

## Index

| ADR | Status | Decision |
| --- | --- | --- |
| [0001](0001-local-first-runtime-boundary.md) | Accepted | Local-first, single-user, loopback-only runtime boundary. |
| [0002](0002-application-identity-and-duplicate-policy.md) | Accepted | Posting, position, company, and contact identity rules. |
| [0003](0003-candidate-controlled-send-policy.md) | Accepted | Explicit approval before every application can be transmitted. |
| [0004](0004-assisted-application-form-boundary.md) | Accepted | Candidate-triggered form support with preview and controlled submit. |
| [0005](0005-job-specific-application-documents.md) | Accepted | Versioned, job-specific documents and immutable submission manifests. |
| [0006](0006-candidate-facts-and-external-ai-processing.md) | Accepted | Verified facts, minimized external processing, and provider replaceability. |
| [0007](0007-retention-backup-and-erasure.md) | Accepted | Active-data erasure and finite, protected backup retention. |
| [0008](0008-ux-navigation-direction.md) | Accepted | Six-area navigation direction without pixel-level prescription. |
| [0009](0009-canonical-documentation-ownership.md) | Accepted | Tracked, project-owned canonical documentation. |
