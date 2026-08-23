# JobDeck

**Your local job application cockpit.** JobDeck discovers postings, evaluates
fit, drafts German application documents, prepares form applications, sends
candidate-approved e-mails through Gmail, and tracks replies.

JobDeck is designed for the German employment market and currently runs as a
single-user application on the local machine.

## Features

- **Multi-source discovery** — saved search profiles query Arbeitsagentur,
  Jooble, and Arbeitnow through a common adapter contract.
- **Match scoring and drafting** — optional Anthropic processing scores postings
  and prepares a tailored Anschreiben and e-mail from `profile.md`. Generated
  content remains subject to candidate review.
- **Application documents** — a local HTML template and Anlagen PDFs are
  rendered into a merged Bewerbungsmappe with channel-aware size handling.
- **Candidate-controlled sending** — every application must be approved before
  it can be sent. Per-profile scheduled sending can transmit previously
  approved drafts and is off by default.
- **Form support** — JobDeck detects common ATS and company-form channels,
  prepares copy-ready values and a staged PDF, and never submits the form.
  JobDeck records an application after candidate confirmation or when a
  strongly matched receipt proves that a submission already occurred.
- **Reply tracking** — Gmail replies are matched to applications, classified by
  deterministic rules with optional Anthropic fallback, and presented for
  review when confidence or identity is insufficient.
- **Local-first storage** — the active database, profile, credentials, generated
  files, and backups remain in the local data directory.

Local-first does not mean offline. Enabled discovery, Anthropic, contact lookup,
and Gmail features communicate with external services. See
[Local Operations](docs/engineering/local-operations.md) for the data boundary
and current limitations.

## Status

JobDeck is alpha software. Discovery, scoring, drafting, PDF assembly, Gmail
sending, form preparation, application tracking, and reply ingestion are
implemented with documented limitations. See
[Current Delivery State](docs/engineering/current-delivery-state.md) for the
verified implementation and the
[Refactoring Roadmap](docs/engineering/refactoring-roadmap.md) for proposed
next slices.

## Requirements

- Python ≥ 3.12, [uv](https://docs.astral.sh/uv/)
- Google Chrome or Chromium (PDF rendering)
- Optional external integrations: [Jooble](https://jooble.org/api/about),
  [Anthropic](https://console.anthropic.com), and a Google OAuth installed-app
  client for Gmail send/read access

## Quick start

```bash
git clone https://github.com/andrei-sili/jobdeck && cd jobdeck
uv sync
uv run jobdeck
```

On first run JobDeck creates its data directory at
`~/.local/share/jobdeck/`. Keep candidate data and credentials there rather
than in the repository. Start from [`.env.example`](.env.example) and
[`profile.example.md`](profile.example.md).

## Documentation

- [Product Direction](docs/product/product-direction.md)
- [Current Delivery State](docs/engineering/current-delivery-state.md)
- [Target Architecture](docs/engineering/target-architecture.md)
- [Refactoring Roadmap](docs/engineering/refactoring-roadmap.md)
- [Local Operations](docs/engineering/local-operations.md)
- [Architecture Decision Records](docs/adr/README.md)

## License

[MIT](LICENSE)
