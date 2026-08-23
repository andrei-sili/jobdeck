---
status: current
owner: Engineering Lead
scope: Behavior and limitations verified in the current JobDeck implementation.
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
---

# Current delivery state

This document contains only behavior verified in the repository. Target
behavior and accepted product boundaries are documented in
[`Product Direction`](../product/product-direction.md).

## Runtime and architecture

JobDeck is a Python application with a NiceGUI interface bound to
`127.0.0.1:8123`. It has no JobDeck user accounts or authorization layer. The
process starts an in-process APScheduler scheduler and stores state in a local
SQLite database configured with WAL, foreign keys, and short-lived connections.

The implementation is a modular monolith:

- `src/jobdeck/ui/` contains NiceGUI pages and presentation helpers;
- `src/jobdeck/services/` orchestrates discovery, scoring, drafting, document
  generation, sending, form recording, and reply handling;
- `src/jobdeck/sources/` contains three job-source adapters behind the
  `JobSource` protocol;
- `src/jobdeck/ai/` contains Anthropic-specific scoring, drafting, claim
  proposal, and reply-classification code;
- `src/jobdeck/db.py` and `src/jobdeck/migrations.py` own SQLite access and the
  schema;
- local files hold the free-form profile, templates, generated PDFs, uploads,
  OAuth credentials, and backups.

The scheduler runs only while the application process is running. Its locks and
`max_instances=1` settings coordinate one process, not multiple JobDeck
processes sharing a database.

## Implemented capabilities

| Capability | Verified implementation |
| --- | --- |
| Job discovery | Parallel polling of Arbeitsagentur, Jooble, and Arbeitnow through a common adapter contract. Search-call failures are isolated. |
| Source identity | SQLite enforces `UNIQUE(source, external_id)`. |
| Cross-source deduplication | A company-and-title normalization heuristic is applied before insertion. |
| Search profiles | Keywords, location, radius, sources, hard tags, soft preferences, strictness, polling interval, and optional auto-send are persisted. |
| Match scoring | Opt-in Anthropic scoring stores a score, one text reason, and extracted contact fields. |
| Drafting | On-demand Anthropic drafting produces an Anschreiben and e-mail body from the free-form profile and the selected posting. |
| Candidate claims register | Anthropic can propose claim entries from `profile.md`; the candidate can add selected entries to the local claims register. |
| Anlagen | Local PDF upload, readability checks, ordering, removal to a recovery folder, and merge into a Mappe. |
| PDF generation | A local HTML template is rendered through Chrome and merged with Anlagen. Size-aware compression is available. |
| E-mail sending | Gmail OAuth, preview/edit, explicit approval state, test mode, real-send switch, daily cap, and ambiguous-outcome recovery. |
| Scheduled sending | Approved drafts may be transmitted by the scheduler for search profiles with auto-send enabled. |
| Form support | JobDeck detects known ATS and form channels, opens the employer page, prepares copy-ready values, and stages a PDF. It records an application after candidate confirmation or after a strongly matched receipt; it does not submit the form. |
| Reply tracking | Gmail history polling, deterministic and optional Anthropic classification, matching, review, Gmail labels, and status history. |
| Application register | Applications, status changes, inbound/outbound message metadata, and selected reply bodies are stored locally. |
| Backups | Startup snapshots use the SQLite backup API, are validated, rotated, and retain the best valid snapshot. |

## Partial or disconnected capabilities

- `profile.md` is the factual input for scoring and drafting, but is unstructured,
  unversioned, and not explicitly marked verified.
- Applicant contact and form values are stored separately in `app_settings`, so
  the current system does not have one candidate aggregate.
- The claims register does not constrain drafting or sending. Factual grounding
  is instruction-based rather than deterministically enforced.
- Match persistence contains only one score and one free-text reason, without a
  structured dimension breakdown, uncertainty, or candidate feedback model.
- Cross-source deduplication does not model source observations, fingerprints,
  or republications and discards alternate provenance.
- Known postings are not refreshed when the same source identifier is observed
  again.
- The application register stores paths and mutable draft fields rather than an
  immutable submitted-artifact manifest.
- Reply bodies associated with applications may be retained in `email_log`, but
  there is no configured retention or complete erasure workflow.
- Provider retries, rate limits, and capabilities are not represented by one
  provider policy contract.

## Missing target capabilities

- CV or existing Bewerbungsmappe import, extraction, and review.
- Structured and versioned candidate profiles and documents.
- Document classification and job-specific attachment selection.
- Persistent job fingerprints and republication relationships.
- Structured match explanations and feedback-driven recommendations.
- Semantic ATS field mapping, candidate-triggered autofill, answer provenance,
  controlled submit, and submission receipts.
- Immutable application attempts and submitted-artifact manifests.
- Complete retention, export, erasure, and consent records.
- Authentication, object ownership, tenant isolation, and multi-user operation.
- A provider-neutral language-model interface and append-only processing ledger.

## Current application identity

The current duplicate gate treats a matching company or contact address as an
existing application. Cross-source jobs are compared using normalized company
and title. These rules do not yet implement the accepted identity policy for a
different position at the same company, and a demonstrated cross-channel race
can create two application rows while a Gmail send is in flight.

## Current document behavior

The current Mappe is built from one mutable draft, one configured HTML template,
and all PDFs found in the Anlagen directory. The build records a `pdf_path` and
checks that the draft did not change during rendering. It does not retain
document versions, template versions, a selected attachment set, or hashes for
the exact submission.

The PDF renderer executes a user-configured HTML template in Chrome with
`--no-sandbox`. Template values are escaped, but JavaScript and foreground
network access are not explicitly disabled by a strict content policy.

## Current external processing

- Anthropic receives profile and posting content for enabled scoring and
  drafting operations. Claim proposal sends up to 20,000 characters from
  `profile.md`. Contact lookup sends company and location and permits provider
  web search. Ambiguous reply text may be sent when reply classification is
  enabled. The application stores aggregate usage and cost, but not a complete
  processing ledger or consent snapshot.
- Gmail is used for OAuth identity, sending, reading replies, and applying
  labels. The saved authorization requests `gmail.send` and `gmail.modify`.
- Job discovery and selected contact/channel resolution make outbound requests
  to configured sources and public employer pages.

There is no product telemetry integration in the repository. This does not make
the application offline: its configured features use external services.

## Delivery and verification

The package supports Python 3.12 or newer. CI currently runs Ruff and pytest on
Python 3.12 and 3.14. Python 3.13 is declared by package classifiers but is not
an explicit CI matrix entry. CI does not currently run a type checker, package
build/install smoke test, dependency scan, secret scan, or Markdown link check.

The test suite covers substantial domain, database, source, Gmail, PDF, SSRF,
and NiceGUI behavior. It does not provide a real browser end-to-end workflow or
live provider contract gate. Live external behavior cannot be inferred from
mocked provider tests.

A local live-page validation currently exposes a timing-sensitive unread-marker
risk: after keyboard navigation persists `opened_at`, the first rendered row can
retain `data-unread="true"`. The database transition succeeds, but the UI marker
assertion in
`tests/test_live_pages.py::test_reading_a_posting_marks_it_read_and_empties_neu`
fails. Production reachability cannot be determined from the available test
evidence.

## Known operational limitations

- Startup migrations run before the startup backup, so the backup is not a
  pre-migration recovery point.
- Backup creation failures can be swallowed and are not reliably distinguished
  from success in the current UI.
- Permanent deletion does not purge all related draft, e-mail, output, removed
  attachment, and backup data.
- Local data directory and database permissions depend on the process umask,
  except for the Gmail token, which is written with owner-only permissions.
- The safe-fetch layer validates DNS before connecting, leaving a documented
  check-to-connect DNS rebinding residual.
- The PDF effective-DPI estimate assumes page-sized placement and can
  conservatively skip some useful compression; it does not reduce image quality
  below the configured floor.
