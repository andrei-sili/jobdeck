---
status: accepted
owner: Product Owner
scope: Navigation and workflow direction without pixel-level UI specification.
last_verified: 2026-08-23
supersedes: []
superseded_by: null
related_adrs:
  - 0003-candidate-controlled-send-policy.md
  - 0004-assisted-application-form-boundary.md
---

# ADR 0008: UX navigation direction

## Context

The current application contains substantial functionality, but some actions
and state are difficult to discover because they are grouped by implementation
concern rather than candidate workflow.

## Decision

The approved navigation direction has six primary destinations:

- Unterlagen;
- Suchprofile;
- Stellen;
- Bewerbungen;
- Antworten;
- Einstellungen.

Screens favor visible state, one clear primary action, direct access to hidden
or deferred work, and explicit candidate control. The approved reference guides
information architecture and workflow only. It is not a pixel-perfect
specification and does not prove current implementation.

## Consequences

- Current implementation state remains documented independently from UX intent.
- Layout details may change while preserving navigation and workflow principles.
- New capabilities should be placed according to candidate intent rather than
  provider or storage implementation.
- Accessibility, keyboard behavior, and responsive use require explicit tests,
  not visual inference from a static reference.
