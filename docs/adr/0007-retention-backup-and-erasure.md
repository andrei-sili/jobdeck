---
status: accepted
owner: Product Owner
scope: Permanent deletion, active stores, backup retention, protection, restore, and erasure reconciliation.
last_verified: 2026-08-23
supersedes: []
superseded_by: null
related_adrs:
  - 0001-local-first-runtime-boundary.md
---

# ADR 0007: Retention, backup, and erasure

## Context

Candidate data spans SQLite, generated documents, uploads, removed files,
credentials, and rotating backups. Deleting one application row does not erase
all related data, and restoring an older snapshot can resurrect records deleted
after that snapshot.

## Decision

Permanent deletion removes the selected data from the active database and
active files. Each retained data category has a documented purpose and
retention period.

Backups use a finite, documented retention policy and protection appropriate to
the sensitivity of candidate data. Restore procedures must not permanently
reintroduce data covered by a later erasure. The design may use durable erasure
markers, a post-restore reconciliation ledger, or cryptographic erasure, but the
chosen mechanism must be testable.

## Consequences

- The project requires a complete data inventory and deletion dependency map.
- Backup retention cannot be described only as an implementation constant.
- Restore drills include replaying erasures that occurred after the snapshot.
- Deletion UX distinguishes ordinary record removal, legally justified
  retention, and permanent erasure.
- Credentials and provider-side data require separate revocation or deletion
  procedures where applicable.
