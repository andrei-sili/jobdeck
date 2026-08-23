---
status: current
owner: Engineering Lead
scope: Canonical documentation index, ownership, and document lifecycle rules.
last_verified: 2026-08-23
supersedes: []
superseded_by: null
related_adrs:
  - adr/0009-canonical-documentation-ownership.md
---

# JobDeck documentation

This directory contains the canonical product and engineering documentation for
JobDeck. Each subject has one owner document. Other documents may summarize the
subject briefly, but must link to its owner instead of copying it.

## Canonical documents

| Subject | Owner document | Responsibility |
| --- | --- | --- |
| Public overview and quick start | [`../README.md`](../README.md) | A short description, requirements, installation, and links into the documentation. |
| Target product and boundaries | [`product/product-direction.md`](product/product-direction.md) | Accepted product scope, candidate control, non-goals, and open product decisions. |
| Verified implementation state | [`engineering/current-delivery-state.md`](engineering/current-delivery-state.md) | Behavior verified in code, current limitations, and implementation gaps. |
| Future technical structure | [`engineering/target-architecture.md`](engineering/target-architecture.md) | Target module boundaries, domain invariants, ports, and trust boundaries. |
| Incremental transition | [`engineering/refactoring-roadmap.md`](engineering/refactoring-roadmap.md) | Ordered slices, dependencies, acceptance criteria, validation, and rollback. |
| Local operation | [`engineering/local-operations.md`](engineering/local-operations.md) | Installation, data paths, credentials, external processing, backup, restore, and the local security boundary. |
| Architectural decisions | [`adr/README.md`](adr/README.md) | Accepted and proposed decisions, their rationale, alternatives, and consequences. |
| Current free-form profile format | [`../profile.example.md`](../profile.example.md) | The operational format of the current `profile.md` file. |
| Environment variables | [`../.env.example`](../.env.example) | Supported public environment configuration. |
| Packaging and dependencies | [`../pyproject.toml`](../pyproject.toml) | Executable package metadata, dependency constraints, and tool configuration. |
| CI gates | [`../.github/workflows/ci.yml`](../.github/workflows/ci.yml) | Checks actually executed by CI. |
| License | [`../LICENSE`](../LICENSE) | Legal terms for distribution and reuse. |

## Statement types

Normative documents must distinguish these categories:

- **Verified in code**: observed in the current implementation or tests.
- **Accepted decision**: approved product or engineering policy.
- **Proposal**: a design or implementation option that is not yet accepted.
- **Assumption**: a condition used temporarily and requiring confirmation.
- **Planned**: accepted work that is not yet implemented.
- **Implemented**: behavior present in the current codebase.
- **Abandoned**: work that is no longer intended.
- **Superseded**: content replaced by a named newer document or decision.
- **Cannot determine**: evidence is not available in the repository.

`Current Delivery State` may use only verified implementation facts and explicit
`Cannot determine` statements. Product intent must not be inferred from code.

## Document lifecycle

Every normative Markdown document must declare:

- `status`;
- `owner`;
- `scope`;
- `last_verified`;
- `supersedes` and `superseded_by`;
- `related_adrs`.

Accepted ADRs are not rewritten to change history. A later decision supersedes
the earlier ADR and links both directions. Historical documents remain
non-normative and must point to their replacement.

Changes that alter externally visible behavior, a domain invariant, a provider
boundary, data handling, or delivery behavior must update the relevant owner
document as part of the Definition of Done.
