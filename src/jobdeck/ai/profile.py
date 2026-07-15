"""User profile: the single source of truth the LLM may claim facts from.

The file lives in the data dir (never in the repo); profile.example.md in the
repo root documents the expected shape.
"""

from jobdeck import config


def load_profile() -> str:
    """Return the profile text, or '' when the user has not created one yet."""
    try:
        return config.PROFILE_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
