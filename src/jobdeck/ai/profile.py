"""User profile: the single source of truth the LLM may claim facts from.

The file lives in the data dir (never in the repo); profile.example.md in the
repo root documents the expected shape.
"""

from jobdeck import config


def load_profile() -> str:
    """Return the profile text, or '' when it cannot be read.

    Every OSError, not only a missing file. Since the Unterlagen screen
    measures this file on every render, a directory left in its place or a
    mode that stops it being read would blank a whole page rather than be
    reported — the same shape as the Anlagen path that once took down every
    screen at once.

    Empty is the safe answer in both directions: nothing in this app claims a
    fact without a profile, so drafting and scoring refuse with a sentence
    instead of proceeding on nothing.
    """
    try:
        return config.PROFILE_PATH.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
