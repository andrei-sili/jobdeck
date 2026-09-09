"""Arbeitnow: what the keyword filter reads, and what the training rule does."""

import httpx
import pytest

from jobdeck.sources.arbeitnow import ArbeitnowSource, offers_training
from jobdeck.sources.base import SearchQuery


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _arbeitnow_item(slug: str, title: str, tags=(), description="", **over) -> dict:
    item = {
        "slug": slug, "title": title, "company_name": "Firma", "location": "Berlin",
        "remote": False, "url": f"https://arbeitnow.com/jobs/{slug}",
        "description": description, "tags": list(tags), "created_at": 1780000000,
    }
    item.update(over)
    return item


def _arbeitnow_client(items):
    def handler(request):
        page = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json={"data": items if page == 1 else []})
    return make_client(handler)


async def test_arbeitnow_matches_keywords_in_the_title_and_tags_only():
    """The body is where a data-labelling or flight-test advert mentions the
    language in passing: measured on this feed's 1288 scored postings, the 380
    with no developer role in the title produced one score above 60."""
    source = ArbeitnowSource(_arbeitnow_client([
        _arbeitnow_item("labelling", "Data Labelling Specialist", tags=["Data"],
                        description="<p>Scripts in Python are a plus.</p>"),
        _arbeitnow_item("tagged", "Backend Engineer", tags=["Python"],
                        description="<p>Go services</p>"),
        _arbeitnow_item("titled", "Python Developer", description="<p>x</p>"),
    ]))
    postings = await source.search(SearchQuery(keywords="python"))
    assert [p.external_id for p in postings] == ["tagged", "titled"]


async def test_arbeitnow_withholds_training_positions_only_when_asked():
    items = [
        _arbeitnow_item("ws", "Software Engineer", tags=["python"],
                        job_types=["Working student"]),
        _arbeitnow_item("azubi", "Werkstudent Softwareentwicklung (m/w/d)",
                        tags=["python"], job_types=["Part time"]),
        _arbeitnow_item("intl", "International Python Developer",
                        job_types=["Full time"]),
        _arbeitnow_item("dev", "Python Developer", job_types=["Permanent"]),
    ]
    source = ArbeitnowSource(_arbeitnow_client(items))
    kept = await source.search(SearchQuery(keywords="python"))
    assert [p.external_id for p in kept] == ["ws", "azubi", "intl", "dev"]
    kept = await source.search(SearchQuery(keywords="python", exclude_training=True))
    assert [p.external_id for p in kept] == ["intl", "dev"]


@pytest.mark.parametrize("item, expected", [
    ({"title": "Intern Software (m/f/d)", "job_types": []}, True),
    ({"title": "Internship Data", "job_types": []}, True),
    ({"title": "International Sales Developer", "job_types": []}, False),
    ({"title": "Praktikant Backend", "job_types": []}, True),
    ({"title": "Duales Studium Informatik", "job_types": []}, True),
    ({"title": "Trainee Program", "job_types": ["Full time"]}, True),
    ({"title": "Backend Developer", "job_types": ["Intern"]}, True),
    ({"title": "Backend Developer", "job_types": ["Working student"]}, True),
    ({"title": "Backend Developer", "job_types": ["Permanent", 7]}, False),
    ({"title": "Ausbilder Metall (m/w/d)", "job_types": None}, False),
])
def test_offers_training_reads_the_boards_words_and_the_title(item, expected):
    assert offers_training(item) is expected
