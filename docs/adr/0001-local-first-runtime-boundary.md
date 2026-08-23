---
status: accepted
owner: Product Owner
scope: Supported runtime, user model, network exposure, and deferred hosted operation.
last_verified: 2026-08-23
supersedes: []
superseded_by: null
related_adrs: []
---

# ADR 0001: Local-first runtime boundary

## Context

JobDeck currently stores candidate data locally, binds its NiceGUI interface to
loopback, and has no application-level identity or authorization model. Adding
hosted access would affect every stored object and every external action.

## Decision

JobDeck remains local-first, single-user, and loopback-only for the current
product. The operating-system account is the supported user boundary.

The application must not be documented or deployed as a LAN, reverse-proxied,
shared-host, or multi-user service. A future hosted product requires a separate
decision covering authentication, object authorization, tenant isolation,
consent, encryption, operations, migration, and recovery.

A hosted version is not considered a database-only migration.

## Consequences

- SQLite and an in-process scheduler remain suitable for the supported mode.
- Process-local coordination is acceptable only while one process is supported.
- Loopback binding is necessary but does not protect against other local
  processes or operating-system users.
- Multi-user abstractions are not added speculatively.
- External providers remain trust boundaries even though primary storage is
  local.
