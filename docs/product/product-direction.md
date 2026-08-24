---
status: accepted
owner: Product Owner
scope: Target product behavior, candidate control, product boundaries, and non-goals.
last_verified: 2026-08-24
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
  - ../adr/0010-company-cooling-off-window.md
---

# Product direction

JobDeck is a candidate-controlled system for discovering jobs, evaluating fit,
preparing job-specific application documents, submitting approved applications,
and tracking outcomes.

This document describes the target product. It does not claim that every
capability is implemented. The verified implementation is documented in
[`Current Delivery State`](../engineering/current-delivery-state.md).

## Product boundary

JobDeck is currently a local-first, single-user, loopback-only product. A
multi-user or hosted product may be considered later, but is outside the active
scope. A hosted version would require explicit identity, authorization,
tenant isolation, consent, operational security, and data lifecycle design; it
is not treated as a database-only migration.

The candidate remains the decision-maker. JobDeck may automate collection,
analysis, generation, preparation, and scheduled execution only within an
explicitly approved application.

## Candidate profile and verified facts

The target profile supports personal and contact details, employment history,
education, training, projects, skills, languages, certificates, references,
portfolio links, availability, salary expectations, work authorization,
relocation, travel, and remote-work preferences.

Candidate data must have one structured source of truth with versions and
provenance. Imported or externally generated values are proposals until the
candidate confirms them. Only confirmed facts may appear in a final
application. A correction creates a new profile version without changing the
historical basis of an already submitted application.

## Job preferences, discovery, and matching

The candidate can define desired and excluded roles, technologies, seniority,
locations, geographic radius, work mode, compensation, contract type,
availability, industries, companies, languages, relocation, travel, and other
hard or soft criteria.

Job sources must use authorized integrations. Each source has explicit
provenance, rate policy, freshness behavior, retry behavior, and operational
ownership. JobDeck normalizes source observations without discarding their
origin.

Matching distinguishes mandatory requirements, missing mandatory requirements,
preferred requirements, experience, technologies, language, location,
compensation, work mode, seniority, candidate preferences, and uncertainty.
The candidate sees the explanation and can provide feedback.

## Application identity

The same posting or a republication of it is a duplicate and must be blocked.
The same company and the same position is blocked by default or treated as a
duplicate. An application also opens a cooling-off window on its company: other
positions there are held back for a configurable period and return by
themselves once it passes. The candidate can apply during that period after an
explicit, recorded confirmation. A contact address alone does not define
application identity.

Application attempts use persistent idempotency and concurrency controls. The
system must not send or record the same application twice.

## Application documents

Each application package is job-specific. The CV, Anschreiben, certificates,
and other attachments are versioned documents. JobDeck may propose relevant
attachments, but the candidate can change the selection before approval.

The final submission stores an immutable manifest containing the exact document
versions, hashes, recipient, subject, message body, answers, channel, and
provider identifiers used for that attempt.

## E-mail applications

JobDeck may generate the Anschreiben, select a CV version, propose attachments,
and prepare the message automatically. The candidate must explicitly approve
the application before transmission. Scheduled transmission is allowed only
after that approval. Fully autonomous submission of unapproved jobs is not part
of the current target product.

Ambiguous provider outcomes must not be retried automatically when doing so
could duplicate a submission. The application remains recoverable through a
candidate-visible resolution path.

## Forms and ATS integrations

The target progression is:

1. copy-ready field support;
2. candidate-triggered autofill;
3. preview and confirmation;
4. controlled submission.

Unknown, sensitive, legal, or eliminatory fields require candidate input.
JobDeck does not invent answers, bypass CAPTCHA, or submit implicitly. API
submission is allowed only through an authorized integration. The system must
preserve answer provenance, candidate overrides, attempt identity, and proof of
submission.

## Tracking

The application lifecycle must distinguish discovery, evaluation, saving,
candidate rejection, preparation, document generation, approval, submission,
technical result, receipt, information requests, interviews, assessments,
rejection, withdrawal, offer, and employment.

The model distinguishes the same company, the same role, the same posting, a
republication, and another role at the same company. Status changes and
communications retain an audit trail without rewriting submitted evidence.

## External processing and privacy

External processing is disclosed clearly. Each operation applies data
minimization and records its purpose, provider, configuration version, and
consent basis. The technical architecture must permit replacement of the
current Anthropic integration.

Permanent deletion removes data from the active database and active files.
Backups have a documented, limited retention period, are protected, and do not
permanently reintroduce erased records after a restore.

## UX direction

The approved navigation direction groups work into six primary destinations:
Unterlagen, Suchprofile, Stellen, Bewerbungen, Antworten, and Einstellungen.
The design favors visible state, one clear primary action, candidate control,
and direct paths to hidden or deferred work. This is a navigation and workflow
reference, not a pixel-perfect specification.

## Current non-goals

- Multi-user hosting or SaaS operation.
- Fully autonomous applications to unapproved jobs.
- CAPTCHA bypass or covert browser submission.
- Provider-specific automation without authorization and an adapter boundary.
- Microservices, event streaming, Kubernetes, or a vector database without a
  demonstrated operational need.
