---
status: accepted
owner: Product Owner
scope: Form and ATS assistance, candidate confirmation, sensitive fields, CAPTCHA, and authorized submit.
last_verified: 2026-08-23
supersedes: []
superseded_by: null
related_adrs:
  - 0002-application-identity-and-duplicate-policy.md
  - 0003-candidate-controlled-send-policy.md
---

# ADR 0004: Assisted application form boundary

## Context

The current product provides copy-ready values and manual recording. The target
should reduce repetitive entry without inventing answers or hiding an
irreversible submission from the candidate.

## Decision

Form support evolves in four controlled stages:

1. copy-ready values;
2. autofill triggered by the candidate;
3. preview and confirmation;
4. controlled submission.

Unknown, sensitive, legal, or eliminatory fields require candidate input.
JobDeck does not invent answers, bypass CAPTCHA, or submit implicitly. API
submission is permitted only through authorized integrations.

Every filled answer records its source, and every candidate override is visible
in the preview. Submission requires the approval defined by ADR 0003.

## Consequences

- ATS adapters expose capabilities rather than pretending all providers support
  the same operations.
- Copy support can ship before autofill; autofill can ship before submit.
- Browser and API attempts share idempotency and evidence requirements.
- CAPTCHA and unsupported fields return control to the candidate.
