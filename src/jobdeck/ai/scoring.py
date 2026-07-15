"""Match scoring: how well a discovered posting fits the user's profile.

The score only sorts the job inbox — it never filters anything out (the user
applies broadly). The reason is a short German note shown next to the score.
"""

import json

from jobdeck.ai import llm

MAX_DESCRIPTION_CHARS = 8000  # bounds cost; postings rarely exceed this

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "reason": {"type": "string"},
    },
    "required": ["score", "reason"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You rate how well a German job posting matches a candidate profile.

Rules:
- Base the rating ONLY on the posting and the profile given below; never
  invent facts about the candidate.
- score: integer 0-100. 100 = the requirements match the profile almost
  fully; 50 = partial overlap still worth applying to; low = little overlap.
  Weigh skills, experience level, language requirements and location/remote
  fit against the profile.
- reason: at most two short sentences, written in German, naming the main
  overlaps or gaps.
"""


def build_user_content(job, profile_text: str) -> str:
    description = (job["description"] or "")[:MAX_DESCRIPTION_CHARS]
    remote = " (remote)" if job["remote"] else ""
    return (
        f"## Candidate profile\n{profile_text}\n\n"
        f"## Job posting\n"
        f"Title: {job['title']}\n"
        f"Company: {job['company']}\n"
        f"Location: {job['location'] or 'n/a'}{remote}\n\n"
        f"{description or '(no description available)'}"
    )


def score_job(job, profile_text: str) -> tuple[int, str, llm.LLMResult]:
    """Score one posting against the profile. Returns (score, reason, usage)."""
    result = llm.complete(
        system=SYSTEM_PROMPT,
        user_content=build_user_content(job, profile_text),
        max_tokens=300,
        output_schema=SCORE_SCHEMA,
    )
    try:
        data = json.loads(result.text)
        score = max(0, min(100, int(data["score"])))
        reason = str(data["reason"]).strip()
    except (ValueError, KeyError, TypeError) as exc:
        raise llm.LLMError(
            f"unparseable scoring response: {result.text!r}", usage=result
        ) from exc
    return score, reason, result
