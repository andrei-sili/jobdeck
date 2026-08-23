---
status: accepted
owner: Product Owner
scope: Approval, scheduled sending, autonomous preparation, and submission control.
last_verified: 2026-08-23
supersedes: []
superseded_by: null
related_adrs:
  - 0002-application-identity-and-duplicate-policy.md
  - 0005-job-specific-application-documents.md
---

# ADR 0003: Candidate-controlled sending

## Context

Generation and preparation can save substantial work, while external
transmission is irreversible and may consume the candidate's opportunity with
an employer. Scheduling must not weaken content approval.

## Decision

Every application requires explicit candidate approval before it can be
transmitted. Generation, matching, document preparation, and attachment
proposals may run automatically.

Scheduled transmission is permitted only for the exact approved application
version. Any change to recipient, message, answer, profile version, document
version, attachment selection, or target job invalidates approval.

Fully autonomous submission to jobs the candidate has not approved is outside
the current target product.

## Consequences

- Approval is a persistent domain record, not only a UI state.
- The approval record references immutable content and document versions.
- Auto-send is an execution policy over approved attempts, not autonomous
  application creation.
- Unknown provider outcomes remain candidate-visible and are not retried
  blindly.
