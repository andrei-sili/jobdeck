"""Application configuration: data directory layout and environment secrets.

All personal data (database, credentials, profile, templates, generated
documents) lives in the user's data directory — never in the repository.
"""

import os
from pathlib import Path

from dotenv import load_dotenv


def _xdg_data_home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))


DATA_DIR = Path(os.environ.get("JOBDECK_DATA_DIR", _xdg_data_home() / "jobdeck"))

DB_PATH = DATA_DIR / "jobdeck.db"
BACKUP_DIR = DATA_DIR / "backups"
OUTPUT_DIR = DATA_DIR / "output"
# ONE folder an employer's file picker ever opens in. Every file chooser on the
# platform reopens wherever it was last used, and the Mappe is archived under
# output/job_<id>/ — a new folder per application, so the picker was
# structurally guaranteed to offer the PREVIOUS employer's documents. German
# name because he reads it in the dialog's breadcrumb.
UPLOAD_DIR = DATA_DIR / "Bewerbung-hochladen"
ENV_PATH = DATA_DIR / ".env"
SECRETS_PATH = DATA_DIR / "secrets.env"  # user-managed; takes precedence over .env
TOKEN_PATH = DATA_DIR / "token.json"
CLIENT_SECRET_PATH = DATA_DIR / "client_secret.json"
PROFILE_PATH = DATA_DIR / "profile.md"


def user_path(text: str) -> Path | None:
    """A path out of a SETTING, expanded — or None when there is no usable one.

    `Path.expanduser()` raises RuntimeError on a "~name" whose user does not
    exist, and every path this app holds is free text in a table the user
    edits. That made a single typo in the Anlagen folder field — "~andrei/…"
    on a machine where the account is called something else — an exception
    raised while a page was being built. It is the same failure shape as the
    non-finite age threshold that once took down the inbox AND the settings
    page that could have fixed it, so it is screened here, once, rather than
    at each of the ten places that expand one.
    """
    text = (text or "").strip()
    if not text:
        return None
    try:
        return Path(text).expanduser()
    except (RuntimeError, ValueError, OSError):
        return None


def ensure_data_dirs() -> None:
    """Create the data directory tree on first run."""
    for path in (DATA_DIR, BACKUP_DIR, OUTPUT_DIR, UPLOAD_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_env() -> None:
    """Load secrets from the data dir (repo-root .env works too, for dev).

    secrets.env is loaded first and therefore wins: it is meant to be
    created and edited exclusively by the user, so credentials never pass
    through any tooling that watches the regular .env file.
    """
    load_dotenv(SECRETS_PATH)
    load_dotenv(ENV_PATH)
    load_dotenv()  # fallback: .env in the working directory


def jooble_api_key() -> str:
    return os.environ.get("JOOBLE_API_KEY", "")


def anthropic_api_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "")


def anthropic_model() -> str:
    """Default model — the cheap, high-volume path (scoring, contact
    extraction). Drafting overrides it with anthropic_drafting_model()."""
    return os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")


DEFAULT_PORT = 8123


def ui_port() -> int:
    """The port the UI binds to.

    Hardcoding it meant a second instance could not exist: a verification run
    against a COPY of the data had to take the port the real app uses, so
    starting JobDeck while one was open silently failed to bind. An unreadable
    value falls back rather than refusing to start — the port is a convenience,
    and a typo in an env var must never be the reason the app will not open.
    """
    raw = os.environ.get("JOBDECK_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        return DEFAULT_PORT
    # 1-1023 need root, which this app never has: binding would fail at
    # startup, which is the one outcome the fallback exists to prevent.
    return port if 1024 <= port <= 65535 else DEFAULT_PORT


def anthropic_drafting_model() -> str:
    """Model for application drafting — the low-volume, quality-critical call
    the user actually applies with. Stronger than the scoring default so the
    letter reads professionally (accurate attribution, role-fit positioning,
    no typos); configurable via env."""
    return os.environ.get("ANTHROPIC_DRAFTING_MODEL", "claude-sonnet-5")
