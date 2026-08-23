---
status: proposed
owner: Engineering Lead
scope: Incremental transition from the verified implementation to the accepted product direction.
last_verified: 2026-08-23
supersedes: []
superseded_by: null
related_adrs:
  - ../adr/0001-local-first-runtime-boundary.md
  - ../adr/0002-application-identity-and-duplicate-policy.md
  - ../adr/0003-candidate-controlled-send-policy.md
  - ../adr/0004-assisted-application-form-boundary.md
  - ../adr/0005-job-specific-application-documents.md
  - ../adr/0006-candidate-facts-and-external-ai-processing.md
  - ../adr/0007-retention-backup-and-erasure.md
  - ../adr/0008-ux-navigation-direction.md
---

# Refactoring roadmap

This roadmap proposes small, independently verifiable slices. The accepted
product constraints are fixed by ADRs; sequencing remains subject to product
priority and implementation evidence.

## Delivery rules

Each slice must:

- preserve unrelated behavior and existing data;
- state its migration and rollback path;
- add failure-path and concurrency tests proportional to risk;
- update `Current Delivery State` when behavior changes;
- update an ADR when a decision changes;
- avoid external submission during tests unless a separately authorized test
  environment exists.

## D0 — Canonical documentation

**Status:** Implemented by the initial documentation consolidation.

**Scope:** Establish product, current-state, target-architecture, roadmap,
operations, and ADR ownership; align public setup documentation; remove dangling
roadmap references from tracked comments.

**Acceptance criteria:**

- every normative subject has one owner document;
- current-state claims have repository evidence;
- accepted product decisions have accepted ADRs;
- tracked documents contain no private operational data or internal working
  material;
- relative documentation links and provenance checks pass.

## S0 — Reliability baseline

**Status:** Implemented.

**Purpose:** Make current storage and validation behavior trustworthy before
adding new domain data.

**Scope:**

- report backup creation failure accurately;
- create and verify a pre-migration recovery point;
- make application undo atomic across database and staging operations;
- centralize typed settings parsing;
- add CI timeouts and deterministic synchronization for timing-sensitive tests;
- add Python 3.13, package build/install smoke, link checks, and security scans
  incrementally.

**Dependencies:** None.

**Acceptance criteria:** Failure injection proves safe migration retry, backup
failure is visible, undo either completes or leaves a recoverable state, and CI
finishes on every declared Python version.

**Rollback:** No public schema removal. Keep old readers available until the new
result types and settings accessors are fully adopted.

## S1 — Application identity and attempt integrity

**Purpose:** Enforce one identity policy across e-mail, form, and manual paths.

**Scope:** Canonical posting relationships, company/position identity,
persistent reservations, application attempts, idempotency keys, and candidate
override evidence for another role at the same company.

**Dependencies:** S0 transaction and migration reliability.

**Acceptance criteria:** Concurrent e-mail and manual/form operations admit one
attempt for the same posting; republications are blocked; another position at
the same company warns and proceeds only after recorded confirmation.

**Validation:** Thread/process concurrency tests, provider-uncertainty tests,
and an identity corpus covering spelling variants, reposts, and distinct roles.

**Rollback:** Retain legacy `bewerbungen` reads while writing the new attempt
records in parallel until reconciliation is verified.

## S2 — Candidate profile and verified facts

**Purpose:** Replace the free-form factual boundary with a structured,
versioned candidate aggregate.

**Scope:** Profile versions, experiences, education, skills, languages,
credentials, preferences, field provenance, proposal/review workflow, and an
explicit compatibility importer for `profile.md`.

**Dependencies:** S0 migrations and privacy inventory from S3 design.

**Acceptance criteria:** Drafting can select only confirmed facts from a named
profile version; importing `profile.md` changes nothing until the candidate
reviews it; historical applications retain their original version reference.

**Validation:** Negative tests for unverified facts, correction/version tests,
and deterministic provenance checks.

**Rollback:** Preserve `profile.md` as read-only compatibility input until the
candidate confirms the structured migration.

## S3 — Documents, privacy, and erasure

**Purpose:** Establish versioned documents and a complete local data lifecycle.

**Scope:** Document registry, immutable versions, MIME/hash metadata, template
versions, retention policy, active-data erasure, backup expiry, restore-time
erasure reconciliation, local permission audit, and safer rendering.

**Dependencies:** S0 backup reliability; coordination with S2 identifiers.

**Acceptance criteria:** Every active and removed file is represented in the
data inventory; a sentinel erasure test finds no value in active stores; expired
backups are removed; restoring an older backup does not permanently resurrect an
erased candidate record.

**Validation:** Restore drills, filesystem permission tests, malicious-document
fixtures, and template network/script isolation tests.

**Rollback:** Document versions are additive. Keep original files untouched
until the registry copy, hash, and read path are verified.

## S4 — Job provenance and source governance

**Purpose:** Preserve source evidence and improve freshness and deduplication.

**Scope:** Source observations, fingerprints, republishes, adapter capabilities,
rate policies, refresh of known postings, authorization records, and contract
fixtures.

**Dependencies:** S0 migration reliability and accepted source authorization.

**Acceptance criteria:** Alternate source provenance is retained; known postings
can update without changing identity; `429` and expiry behavior are deterministic;
each enabled source has an approved operational and usage policy.

**Rollback:** Continue populating legacy job fields from the newest accepted
observation until all readers migrate.

## S5 — Explainable matching and processing ledger

**Purpose:** Make recommendations and external processing reproducible.

**Scope:** Provider-neutral language-model port, append-only processing records,
versioned instructions and schemas, minimized inputs, match dimensions,
uncertainty, feedback, persistent retry state, and evaluation fixtures.

**Dependencies:** S2 verified profiles and S4 source observations.

**Acceptance criteria:** Every assessment names its input versions and evidence;
unsupported facts cannot become verified; provider replacement requires one
adapter rather than changes across domain services.

**Validation:** Adversarial input corpus, schema compatibility tests, cost/retry
tests, and a second fake adapter contract.

**Rollback:** Keep the existing Anthropic adapter behind the new port until
behavioral parity is demonstrated.

## S6 — Job-specific application packages

**Purpose:** Produce immutable, candidate-approved submission manifests.

**Scope:** Job-specific CV and Anschreiben versions, attachment proposals,
candidate selection, template versions, final render, hashes, approval snapshot,
and submitted artifacts.

**Dependencies:** S1 attempts, S2 verified facts, S3 documents.

**Acceptance criteria:** The candidate can change proposed attachments before
approval; changing any approved input invalidates approval; a sent application
is reconstructible byte-for-byte from its manifest.

**Validation:** Mutation-after-approval tests, render reproducibility checks,
and e-mail/form manifest parity tests.

**Rollback:** Preserve the current all-Anlagen build as an explicit legacy mode
during migration, never as the silent fallback for a new manifest.

## S7 — E-mail submission on the application aggregate

**Purpose:** Move the working Gmail flow onto persistent attempts and immutable
evidence.

**Scope:** Mail port, approval snapshot, scheduled-send lease, uncertain-outcome
reconciliation, submitted message body, attachment manifest, and communication
records.

**Dependencies:** S1 and S6.

**Acceptance criteria:** Only approved content is transmitted; scheduled sends
cannot change content; every accepted or uncertain result has a single durable
attempt and recovery path.

**Validation:** Blocking-provider concurrency tests, ambiguous transport tests,
and test-mode/real-mode separation tests with no external recipient.

**Rollback:** Keep the existing Gmail path available behind a migration flag
until attempt reconciliation proves complete.

## S8 — Candidate-triggered form and ATS support

**Purpose:** Evolve copy-ready support without removing candidate control.

**Scope:** Semantic fields, answer provenance, sensitive-field classification,
candidate-triggered autofill, preview, authorized API adapters, controlled
submit, receipts, and duplicate prevention.

**Dependencies:** S1, S2, S3, and S6.

**Acceptance criteria:** Unknown and sensitive fields block completion; every
filled value points to a confirmed fact or candidate entry; CAPTCHA returns
control to the candidate; no submit occurs without preview and confirmation.

**Validation:** Adapter contract tests, hostile-form fixtures, browser tests with
submission disabled by default, and idempotency tests.

**Rollback:** Ship copy-ready support first, then autofill per adapter. Submit
remains disabled until an authorized integration passes its contract suite.

## S9 — Onboarding and import

**Purpose:** Provide structured onboarding once the destination profile and
document models are stable.

**Scope:** Manual profile entry, CV/Bewerbungsmappe import, PDF/DOCX parsing,
classification, candidate correction, and migration from existing local data.

**Dependencies:** S2 and S3.

**Acceptance criteria:** Import never marks facts verified automatically;
parsing is resource-bounded; candidates can review each proposed fact and
document classification before activation.

**Validation:** Malformed and hostile document corpus, correction workflow
tests, and import/export round trips.

## S10 — Scale only when measured

Persistent workers, another database, hosted operation, or multi-user support
require measured pressure and a separate accepted architecture decision. Before
that decision, improve observability and profile the local modular monolith
rather than introducing distributed infrastructure.

## Documentation migration plan

The initial consolidation applies these operations. No project-owned document
currently requires archival, so `docs/archive/` is intentionally absent.

| Source | Destination | Reason | Information preserved | Context-loss risk | Acceptance criterion | Product Owner approval |
| --- | --- | --- | --- | --- | --- | --- |
| Existing `README.md` product and setup statements | `README.md`, Product Direction, Current Delivery State, and Local Operations | Keep the public entry point concise and assign detailed claims to one owner | Public purpose, supported runtime, setup, integrations, and verified feature summary | Medium: a short overview can hide important operational limits | README links to each owner; setup and feature claims match code | Required for changes to product positioning; completed for the initial boundary |
| Existing `profile.example.md` guidance | `profile.example.md` and Product Direction | Separate the operational free-form input from the target structured profile | Current file location, provider use, review warning, and a non-personal example | Low | The example matches current loading behavior and does not claim deterministic verification | Required for changing the target profile; completed for the initial boundary |
| Existing `.env.example` comments and configuration defaults | `.env.example` and Local Operations | Keep variables close to executable configuration and move policy to operations documentation | Supported keys, current defaults, precedence, and external-processing purpose | Medium: an undocumented default can change cost or data flow | Every public setting is documented without a secret value and agrees with `config.py` | Not required for factual synchronization; required for policy changes |
| Tracked source comments that referenced a missing roadmap | Canonical engineering documents and corrected source comments | Remove dangling references and distinguish current behavior from accepted target policy | Relevant invariants and the current implementation constraint | Low | No tracked comment references a missing roadmap; comments link to the owning ADR where useful | Not required when behavior is unchanged |
| Verified implementation evidence from code and tests | Current Delivery State | Prevent plans and historical snapshots from being treated as implementation | Runtime boundaries, implemented flows, partial capabilities, limitations, and verification gaps | High: stale current-state claims can misdirect delivery and security work | Each material statement can be traced to current code, tests, or configuration during review | Not required for factual corrections; required if scope is reinterpreted |
| Accepted product constraints and durable rationale | Product Direction and ADRs 0001–0009 | Make approved behavior normative without mixing it with delivery state | Product boundaries, application identity, approval, forms, documents, external processing, erasure, UX, and documentation ownership | High: omitted constraints could permit irreversible or privacy-sensitive behavior | Each accepted decision has one ADR and is not presented as already implemented | Required; completed for ADRs 0001–0009 |
| Unaccepted technical designs and sequencing options | Target Architecture and this roadmap | Preserve useful direction while keeping proposals non-authoritative | Module boundaries, ports, invariants, dependencies, validation, and rollback options | Medium | Documents are marked proposed and do not override accepted ADRs | Required before a proposal becomes an accepted architectural decision |
| Historical project-owned documents superseded in the future | `docs/archive/` | Preserve decision context without leaving obsolete material normative | Original content, status, date, and link to the replacement | Medium | Archive is created only when needed; archived files declare `superseded_by` and canonical files declare `supersedes` | Required when archival could remove product context |

Internal working records, raw reviews, private operational state, and personal
data are not documentation sources that tracked files may link to or reproduce.

## Documentation maintenance

| Source of change | Required destination | Acceptance criterion |
| --- | --- | --- |
| Product scope or behavior decision | Product Direction and an ADR when architectural | Decision status and consequences are explicit. |
| Implemented or removed behavior | Current Delivery State | Statement is linked to current code or tests during review. |
| Technical boundary or invariant | Target Architecture and ADR | Proposal and accepted decision are not conflated. |
| Delivery sequencing | This roadmap | Slice has dependencies, acceptance, validation, and rollback. |
| Setup, credentials, backup, or restore behavior | Local Operations | Procedure matches current code and exposes limitations. |
| Superseded project-owned document | `docs/archive/` when needed | Historical document links to its canonical replacement. |

CI should check relative Markdown links, required metadata, and references to
missing documentation. Semantic current-state accuracy remains a maintainer
review responsibility and part of the Definition of Done.
