---
status: accepted
owner: Product Owner
scope: Tracked canonical documentation and separation from ignored internal working material.
last_verified: 2026-08-23
supersedes: []
superseded_by: null
related_adrs: []
---

# ADR 0009: Canonical documentation ownership

## Context

Project facts, decisions, plans, and historical rationale previously existed in
several public files, source comments, ignored planning materials, and local
handoff records. This made current behavior difficult to distinguish from
proposals and historical snapshots.

## Decision

Canonical, sanitized, project-owned documentation is tracked in Git and is
independent of any development tool. Each important subject has one owner
document as defined in [`docs/README.md`](../README.md).

Raw reviews, private operational state, personal data, temporary planning
material, and tool-specific configuration remain ignored. Tracked documentation
does not link to or reproduce those materials.

Current implementation claims are verified against code and tests. Product
decisions, proposals, assumptions, plans, implemented behavior, abandoned work,
and superseded material are labelled explicitly.

## Consequences

- README remains a concise public entry point rather than the complete product
  specification.
- Product Direction, Current Delivery State, Target Architecture, and the
  Refactoring Roadmap cannot substitute for one another.
- Architectural decisions use ADRs and explicit supersession.
- Documentation updates are part of the Definition of Done for behavior and
  boundary changes.
- CI may enforce links and metadata, while semantic accuracy remains a review
  responsibility.
