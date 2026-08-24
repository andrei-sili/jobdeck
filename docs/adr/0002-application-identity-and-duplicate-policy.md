---
status: accepted
owner: Product Owner
scope: Application identity, duplicate blocking, company warnings, and contact-address semantics.
last_verified: 2026-08-23
supersedes: []
superseded_by: 0010-company-cooling-off-window.md
related_adrs:
  - 0003-candidate-controlled-send-policy.md
  - 0010-company-cooling-off-window.md
---

# ADR 0002: Application identity and duplicate policy

## Context

A job may appear through multiple sources, be republished, or share a title and
company with another real position. The current company-or-contact rule blocks
some valid applications and does not express posting identity reliably.

## Decision

- The same posting or a republication of it is blocked as a duplicate.
- The same company and the same position is blocked by default or treated as a
  duplicate.
- A different position at the same company produces a warning and may continue
  after explicit candidate confirmation.
- A shared contact address is evidence, but never the sole application identity.
- E-mail, form, API, and manual recording paths use the same persistent identity
  and reservation policy.

Identity decisions retain their evidence and confidence. Uncertain identity is
shown to the candidate rather than silently collapsed.

## Consequences

- The domain must represent canonical postings, source observations, and
  republication relationships.
- Application attempts require persistent idempotency keys and reservations.
- Company history remains visible without turning every position at that
  company into a hard duplicate.
- Existing company-or-contact behavior requires migration and compatibility
  tests before replacement.

## Superseded in part (2026-08-24)

[ADR 0010](0010-company-cooling-off-window.md) replaces exactly one decision
above: "A different position at the same company produces a warning and may
continue after explicit candidate confirmation." That case is now resolved by a
company cooling-off window.

Every other decision in this record — posting and republication identity, the
status of a shared contact address, one persistent identity and reservation
policy across all channels, and retaining the evidence behind a decision —
remains accepted and normative. This note records the supersession; it does not
rewrite the original decision.
