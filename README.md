# JobDeck

**Your job application cockpit.** JobDeck discovers job postings across German job boards, drafts a tailored application (cover letter + email) for each posting with AI, lets you review and send with one click via Gmail, and tracks replies automatically — updating each application's status the moment a rejection, invitation, or confirmation lands in your inbox.

> Built for the German job market (Bewerbung norms: one merged PDF, proper Betreff, named contact person), but the architecture is source-pluggable and language-agnostic.

## Features

- **Multi-source discovery** — parallel saved search profiles against the Bundesagentur für Arbeit Jobsuche, Jooble, and Arbeitnow, with cross-source deduplication and duplicate protection against companies you already applied to.
- **AI-tailored drafting** — reads the full posting and writes a concise, posting-specific German cover letter and email. Facts come only from your profile file; the AI cannot invent experience.
- **Human-in-the-loop sending** — a review queue where you edit and approve every application before it goes out through the Gmail API. Optional per-profile auto-send with a hard daily cap, off by default.
- **Automatic reply tracking** — polls your inbox, matches replies to applications, classifies them (confirmation / rejection / interview invitation) with rule-based matching plus an AI fallback, and updates statuses with a full audit trail.
- **Local-first** — your data lives in a local SQLite database with automatic rotating backups. No cloud, no accounts, no telemetry.

## Status

Work in progress — Phase 1 (discovery + tracking) under active development. See the issues for the roadmap.

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
