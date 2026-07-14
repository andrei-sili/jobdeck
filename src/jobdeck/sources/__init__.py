"""Job source adapters (one module per board, common interface in base)."""

import httpx

from jobdeck.sources.arbeitnow import ArbeitnowSource
from jobdeck.sources.arbeitsagentur import ArbeitsagenturSource
from jobdeck.sources.base import JobPosting, JobSource, SourceUnavailable
from jobdeck.sources.jooble import JoobleSource

__all__ = [
    "JobPosting",
    "JobSource",
    "SourceUnavailable",
    "get_sources",
]


def get_sources(client: httpx.AsyncClient) -> dict[str, JobSource]:
    """All available sources keyed by name. Sources that lack credentials
    are still registered; they raise SourceUnavailable when used, which
    the polling service reports per profile."""
    sources: list[JobSource] = [
        ArbeitsagenturSource(client),
        JoobleSource(client),
        ArbeitnowSource(client),
    ]
    return {s.name: s for s in sources}
