---
status: accepted
owner: Product Owner
scope: Candidate fact verification, external processing transparency, data minimization, and provider replaceability.
last_verified: 2026-08-23
supersedes: []
superseded_by: null
related_adrs:
  - 0005-job-specific-application-documents.md
---

# ADR 0006: Candidate facts and external AI processing

## Context

The current Anthropic integration receives a free-form candidate profile and
external job content. Instructions ask the provider to remain factual, but
provider output is not a verified source and the claims register does not yet
enforce final content.

## Decision

External processing is disclosed transparently and uses the minimum candidate
data needed for the operation. Extracted or generated values never become
verified facts automatically. Only candidate-confirmed facts may be used in a
final application.

Each external processing record identifies its purpose, provider configuration,
input versions, minimized field set, output schema, usage, validation result,
and applicable consent version without copying raw personal content into
operational logs.

Domain services depend on a provider-neutral interface. Anthropic remains the
current adapter and must be replaceable without changing candidate, job,
matching, document, or application domain rules.

## Consequences

- Candidate facts require provenance, proposal, confirmation, and version
  states.
- Provider output is validated before use and cannot authorize submission.
- Scoring, drafting, extraction, and reply classification share processing
  records but may minimize different fields.
- Provider-specific retry and SDK behavior stays inside the adapter.
