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
ENV_PATH = DATA_DIR / ".env"
SECRETS_PATH = DATA_DIR / "secrets.env"  # user-managed; takes precedence over .env
TOKEN_PATH = DATA_DIR / "token.json"
CLIENT_SECRET_PATH = DATA_DIR / "client_secret.json"
PROFILE_PATH = DATA_DIR / "profile.md"


def ensure_data_dirs() -> None:
    """Create the data directory tree on first run."""
    for path in (DATA_DIR, BACKUP_DIR, OUTPUT_DIR):
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
    return os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
