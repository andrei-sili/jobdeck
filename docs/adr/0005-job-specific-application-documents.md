---
status: accepted
owner: Product Owner
scope: Document versioning, job-specific selection, candidate override, and submitted-artifact evidence.
last_verified: 2026-08-23
supersedes: []
superseded_by: null
related_adrs:
  - 0003-candidate-controlled-send-policy.md
---

# ADR 0005: Job-specific application documents

## Context

The current Mappe uses one mutable draft, one configured template, and all PDFs
in the Anlagen directory. This cannot prove which source versions and
attachments formed a submission and cannot tailor supporting evidence safely.

## Decision

The Bewerbungsmappe is job-specific. CVs, Anschreiben, certificates, references,
and other attachments are versioned documents.

JobDeck may propose relevant document versions and attachments. The candidate
may add, remove, or replace them before approval. The final submission records
an immutable manifest containing the exact versions and hashes that were sent.

Generated documents also reference the profile version, job observation,
template version, placeholder values, and generation record used to create
them.

## Consequences

- The current always-include-all-Anlagen behavior is a legacy behavior, not the
  target invariant.
- A changed document or selection invalidates application approval.
- Historical submissions remain reconstructible after profile, template, or
  attachment changes.
- Document storage and rendering require explicit integrity and trust controls.
