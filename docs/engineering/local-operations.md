---
status: current
owner: Engineering Lead
scope: Installation, local data, credentials, external processing, backup, restore, and runtime security.
last_verified: 2026-08-23
supersedes: []
superseded_by: null
related_adrs:
  - ../adr/0001-local-first-runtime-boundary.md
  - ../adr/0003-candidate-controlled-send-policy.md
  - ../adr/0006-candidate-facts-and-external-ai-processing.md
  - ../adr/0007-retention-backup-and-erasure.md
---

# Local operations

## Requirements

- Python 3.12 or newer;
- [`uv`](https://docs.astral.sh/uv/);
- Google Chrome or Chromium for PDF rendering;
- credentials for the external features the operator enables.

Install and start the application:

```bash
uv sync
uv run jobdeck
```

JobDeck listens on `127.0.0.1:8123`. It is not designed to be exposed through
a reverse proxy, LAN binding, or shared host. There is no JobDeck authentication
or authorization layer.

## Data directory

On Linux the default directory is:

```text
~/.local/share/jobdeck/
```

`XDG_DATA_HOME` changes the XDG base. `JOBDECK_DATA_DIR` overrides the complete
JobDeck path, which is useful for isolated development and tests.

The directory contains or may contain:

| Path | Purpose |
| --- | --- |
| `jobdeck.db` | Active SQLite database. |
| `profile.md` | Current free-form professional profile used by scoring and drafting. |
| `.env` | Local environment configuration. |
| `secrets.env` | Preferred local secrets file; loaded before `.env`. |
| `client_secret.json` | Google OAuth installed-application client configuration. |
| `token.json` | Saved Gmail authorization; written with owner-only permissions. |
| `backups/` | Rotating SQLite snapshots. |
| `output/` | Generated application PDFs. |
| `Bewerbung-hochladen/` | Staging folder opened for form uploads. |

Do not place real candidate data, credentials, OAuth files, generated
applications, or database copies in the repository.

## Environment configuration

Existing process environment values take precedence. For keys that are not
already set, JobDeck loads files in this order without overriding an earlier
file value:

1. `secrets.env` in the data directory;
2. `.env` in the data directory;
3. `.env` discovered from the current working directory.

Use [`.env.example`](../../.env.example) as the public variable reference. Keep
secret values out of shell history, logs, screenshots, issue descriptions, and
tracked files.

## External processing

Local-first describes storage and the runtime boundary, not an offline system.
Enabled features may contact:

- Arbeitsagentur, Jooble, and Arbeitnow for job discovery;
- public job or employer pages for channel and contact resolution;
- Anthropic for enabled scoring, drafting, contact lookup, and optional reply
  classification;
- Google OAuth and Gmail for identity, sending, reply ingestion, and labels.

The current application sends the free-form profile and job content for scoring
and drafting. Claim proposal may send up to 20,000 characters from the profile.
Enabled contact lookup sends company and location and permits provider web
search. Enable these features only after reviewing the data involved. The
current implementation does not yet provide field-level minimization or a
complete consent and processing ledger.

## Gmail setup and sending

Place an installed-application OAuth client file at:

```text
~/.local/share/jobdeck/client_secret.json
```

Use Settings to connect Gmail. The current authorization requests:

- `openid` and e-mail identity, so the UI can show the connected account;
- `gmail.send`, for outbound applications;
- `gmail.modify`, for reading replies and managing JobDeck labels.

Real sending is disabled until explicitly enabled. Test mode redirects messages
to the configured test recipient. Real sending also applies the daily send cap.
An approved draft may be transmitted later by the scheduler only when its search
profile has auto-send enabled.

If Gmail may have accepted a message but the response was lost, JobDeck leaves
the draft in `sending`. Check the Gmail Sent folder and resolve the attempt in
the review queue. Do not retry blindly.

## Backups and restore

At startup, JobDeck attempts to create a consistent SQLite snapshot using the
SQLite backup API. A snapshot is validated before retention. Backups are grouped
by database directory, the normal retention target is ten snapshots, and the
best valid snapshot is protected from rotation.

Current limitations:

- startup migration occurs before the startup backup;
- backup failure does not stop startup and is not always reported accurately;
- there is no built-in restore workflow;
- backup retention is count-based rather than an explicit time period;
- restoring an old backup can reintroduce data that was later deleted.

To perform a manual restore, stop JobDeck, preserve the current database as a
separate recovery copy, select a validated snapshot, and replace `jobdeck.db`.
Do not copy a SQLite database while the application is writing to it. After a
restore, review records deleted after the snapshot and remove them again where
required. The target restore behavior is defined in
[`ADR 0007`](../adr/0007-retention-backup-and-erasure.md), but is not yet
implemented.

## Local security

- Run JobDeck only under the intended operating-system account.
- Do not bind the UI beyond loopback or place it behind a reverse proxy.
- Protect the data directory with owner-only permissions and full-disk or home
  directory encryption where available.
- Treat HTML templates as trusted local code until rendering isolation is
  implemented.
- Review generated text, recipients, answers, and attachments before approval.
- Revoke Gmail access through Settings when the integration is no longer used.

## Safe development checks

The repository-defined checks are:

```bash
uv run ruff check .
uv run pytest
```

Tests must use their temporary data fixtures. Do not point tests, experiments,
or migrations at the active candidate data directory.
