# JobDeck

**Your job application cockpit.** JobDeck discovers job postings across German job boards, drafts a tailored application (cover letter + email) for each posting with AI, and lets you review and send it with one click via Gmail — with every send gated behind your approval. Reply tracking, which closes the loop by updating an application's status when an answer lands in your inbox, is the next phase.

> Built for the German job market (Bewerbung norms: one merged PDF, proper Betreff, named contact person), but the architecture is source-pluggable and language-agnostic.

## Features

- **Multi-source discovery** — parallel saved search profiles against the Bundesagentur für Arbeit Jobsuche, Jooble, and Arbeitnow, with cross-source deduplication and duplicate protection against companies you already applied to.
- **AI-tailored drafting** — reads the full posting and writes a concise, posting-specific German cover letter and email. Facts come only from your profile file; the AI cannot invent experience.
- **Human-in-the-loop sending** — a review queue where you edit and approve every application before it goes out through the Gmail API. Optional per-profile auto-send with a hard daily cap, off by default.
- **Local-first** — your data lives in a local SQLite database with automatic rotating backups. No cloud, no accounts, no telemetry.

### Planned

- **Automatic reply tracking** — poll the inbox, match replies to applications, classify them (confirmation / rejection / interview invitation) and update statuses with a full audit trail.

## Status

Work in progress. Discovery, AI match scoring, application drafting, PDF assembly and Gmail sending are implemented; reply tracking is next. See the issues for the roadmap.

## Requirements

- Python ≥ 3.12, [uv](https://docs.astral.sh/uv/)
- Google Chrome or Chromium (PDF rendering)
- API keys: [Jooble](https://jooble.org/api/about) (free), [Anthropic](https://console.anthropic.com) (drafting), Google OAuth client (Gmail send/read)

## Quick start

```bash
git clone https://github.com/andrei-sili/jobdeck && cd jobdeck
uv sync
uv run jobdeck
```

On first run JobDeck creates its data directory (`~/.local/share/jobdeck/`), where your database, `.env`, profile, and templates live — nothing personal is ever stored in the repository.

## License

[MIT](LICENSE)
