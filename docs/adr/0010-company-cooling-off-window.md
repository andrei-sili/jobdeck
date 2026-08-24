---
status: accepted
owner: Product Owner
scope: How long an employer is left alone after an application, and how held-back postings return.
last_verified: 2026-08-24
supersedes:
  - 0002-application-identity-and-duplicate-policy.md
superseded_by: null
related_adrs:
  - 0002-application-identity-and-duplicate-policy.md
  - 0003-candidate-controlled-send-policy.md
---

# ADR 0010: Company cooling-off window

## Context

[ADR 0002](0002-application-identity-and-duplicate-policy.md) resolved another
position at a previously contacted company with a warning and an explicit
candidate confirmation. Two findings from the current implementation and a real
corpus argue against that resolution.

First, the warning needs to know which position an earlier application was for,
and the ledger records it for only some rows. In a corpus of 131 applications,
44 have no linked posting at all, so the position cannot be recovered for a
third of the register. A rule that degrades to "unknown" for a third of its
inputs is not the rule that was accepted.

Second, and decisive: the candidate's own working practice is not "warn me",
it is "leave that employer alone for a while". A hold that never expires is the
present defect — 83 open postings at 30 companies were hidden with no way back,
every one of them at a company contacted within the preceding weeks. What was
wrong was not that they were hidden; it was that hiding them was permanent.

## Decision

An application to a company opens a **cooling-off window** on that company.

- The window length is a candidate setting, default 60 days, counted from the
  date of the application. A value of zero switches the rule off.
- While the window runs, other postings at that company are held back from the
  working list. They are counted beneath it and reachable through their own
  view. Nothing is deleted and no status is written: the hold is a read-time
  decision, so waiting it out returns the postings by itself.
- The candidate may apply from that view during the window. Doing so requires
  an explicit confirmation, and the confirmation is recorded as evidence on
  the attempt.
- When the window passes, the company's postings return to the working list
  without any action.

These decisions of ADR 0002 are unchanged and remain normative:

- the same posting, or a republication of one that already produced an
  application, is blocked permanently — no window makes a second application
  to the same position reasonable;
- a shared contact address is evidence and never an application identity;
- e-mail, form, API, and manual recording paths use one persistent identity
  and reservation policy;
- identity decisions retain their evidence, and uncertain identity is shown to
  the candidate rather than silently collapsed.

Only the "different position at the same company produces a warning and may
continue after explicit candidate confirmation" decision is replaced.

## Consequences

- The rule is a company and time rule, so it applies whether or not the earlier
  position is known. Postings whose position cannot be recovered are held for
  the window like any other, and return with it.
- A refusal during the window must not be written as a posting status. The
  existing `duplicate` status stays reserved for permanent refusals.
- The window interacts with the posting age filter: a posting rarely survives
  long enough to be offered again after a long window, so most postings that
  return are ones discovered after the application.
- Freshness of the hold depends on the application date being readable. An
  application with no usable date keeps its company held and says so, rather
  than assuming the window has passed.
- The setting belongs beside the other candidate-facing timing settings and
  must state what changing it does to the working list.
