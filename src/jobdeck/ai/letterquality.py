"""What a letter says about itself, measured — before a person reads it.

Two gates the research (2026-09-04) put in front of a German application
are served here, deterministically and for free:

* **The parser gate** ranks on the posting's own vocabulary. `coverage` says
  which of the posting's terms the letter and the CV actually carry, in the
  posting's spelling. Nothing is added by this module — a term the profile
  does not support must NOT appear, and the count is shown so the candidate
  can see the gap rather than have it papered over.
* **The human gate** reads dozens of letters a week and spots the machine by
  its tells: the stock phrases every generated letter opens with, a sentence
  lifted from the advert, the same opening as the last ten letters. Those are
  reported as notes; the drafting service re-rolls once on them, the
  Postausgang prints what is left.

Everything is computed from stored text at read time — the letter, the
posting and the CV template — so nothing here needs a column, and a note
can never go stale against the text it describes.
"""

import dataclasses
import html
import pathlib
import re

# The vocabulary a German software posting is written in, and the spelling
# each term is matched by. Matching is case-insensitive on word boundaries;
# a term is listed with its variants so "Django REST Framework" in the advert
# and "DRF" in the letter still count as one term carried.
# Kept deliberately about THIS candidate's market (backend, Python/Java, the
# tooling around them, the process words HR filters on) — a general skills
# taxonomy would flag "Excel" in a letter about REST APIs.
TERM_VARIANTS: dict[str, tuple[str, ...]] = {
    "Python": ("python",),
    "Django": ("django",),
    "Django REST Framework": ("django rest framework", "drf", "django rest"),
    "FastAPI": ("fastapi",),
    "Flask": ("flask",),
    "Java": ("java",),
    "Spring Boot": ("spring boot", "spring"),
    "Kotlin": ("kotlin",),
    "TypeScript": ("typescript",),
    "JavaScript": ("javascript",),
    "React": ("react",),
    "Angular": ("angular",),
    "Vue": ("vue", "vue.js", "vuejs"),
    "Node.js": ("node.js", "nodejs", "node"),
    # Case-sensitive (see CASE_SENSITIVE): "der Rest" is not an API style.
    "REST": ("REST", "REST-API", "REST-APIs", "REST API", "REST APIs", "RESTful",
             "Restful", "REST-Schnittstelle", "REST-Schnittstellen"),
    "API": ("api", "apis", "schnittstellen"),
    "GraphQL": ("graphql",),
    "OpenAPI": ("openapi", "swagger"),
    "SQL": ("sql",),
    "PostgreSQL": ("postgresql", "postgres"),
    "MySQL": ("mysql", "mariadb"),
    "SQLite": ("sqlite",),
    "MongoDB": ("mongodb", "mongo"),
    "Redis": ("redis",),
    "Elasticsearch": ("elasticsearch",),
    "Docker": ("docker",),
    "Kubernetes": ("kubernetes", "k8s"),
    "CI/CD": ("ci/cd", "ci-cd", "continuous integration", "continuous delivery"),
    "GitHub Actions": ("github actions",),
    "GitLab": ("gitlab",),
    "Git": ("git",),
    "Linux": ("linux",),
    "AWS": ("aws", "amazon web services"),
    "Azure": ("azure",),
    "GCP": ("gcp", "google cloud"),
    "Cloud": ("cloud",),
    "Terraform": ("terraform",),
    "Ansible": ("ansible",),
    "RabbitMQ": ("rabbitmq",),
    "Kafka": ("kafka",),
    "Celery": ("celery",),
    "Microservices": ("microservices", "microservice", "microservice-architektur"),
    "Pytest": ("pytest",),
    "JUnit": ("junit",),
    "Unit Tests": ("unit tests", "unit-tests", "unittests", "unit test"),
    "Testcontainers": ("testcontainers",),
    "TDD": ("tdd", "test-driven"),
    "Clean Code": ("clean code",),
    "SOLID": ("solid",),
    "Design Patterns": ("design patterns", "entwurfsmuster"),
    "OOP": ("oop", "objektorientiert", "objektorientierte", "object-oriented"),
    "Scrum": ("scrum",),
    "Agil": ("agile", "agiles", "agil", "agilen", "agiler"),
    "Kanban": ("kanban",),
    "Jira": ("jira",),
    "Confluence": ("confluence",),
    "JWT": ("jwt",),
    "OAuth": ("oauth", "oauth2"),
    "WebSockets": ("websockets", "websocket"),
    "HTML": ("html",),
    "CSS": ("css",),
    "Code Review": ("code review", "code reviews", "code-reviews", "codereviews"),
    "Debugging": ("debugging", "bugfixing", "fehleranalyse"),
    "Logging": ("logging",),
    "Monitoring": ("monitoring",),
    "Dokumentation": ("dokumentation", "documentation", "dokumentieren"),
    "Support": ("support", "second-level", "2nd-level", "third-level"),
    "Ticketsystem": ("ticketsystem", "ticket-system", "tickets"),
    "Datenbanken": ("datenbanken", "datenbank", "database", "databases"),
    "Backend": ("backend", "back-end"),
    "Frontend": ("frontend", "front-end"),
    "Fullstack": ("fullstack", "full-stack", "full stack"),
    "LLM": ("llm", "llms", "large language model", "ki-integration"),
    "Machine Learning": ("machine learning", "maschinelles lernen"),
    "Englisch": ("englisch", "english"),
    "Deutsch": ("deutsch", "german"),
    "Fachinformatiker": ("fachinformatiker", "fachinformatikerin"),
    "Ausbildung": ("ausbildung",),
    "Studium": ("studium", "bachelor", "master", "informatik"),
}

_WORD_CHARS = r"A-Za-z0-9ÄÖÜäöüß"
# Terms whose lower-case form is an ordinary German word.
CASE_SENSITIVE = frozenset({"REST"})


def _compile(term: str, variants: tuple[str, ...]) -> re.Pattern:
    alts = "|".join(re.escape(v) for v in sorted(variants, key=len, reverse=True))
    flags = 0 if term in CASE_SENSITIVE else re.I
    return re.compile(rf"(?<![{_WORD_CHARS}])(?:{alts})(?![{_WORD_CHARS}])", flags)


_TERM_RES = {term: _compile(term, variants)
             for term, variants in TERM_VARIANTS.items()}
# Every word of every variant, lower-cased, for telling a term list from prose.
_VOCAB_WORDS = frozenset(
    w for variants in TERM_VARIANTS.values() for v in variants
    for w in re.findall(rf"[{_WORD_CHARS}]+", v.lower()))


def terms_in(text: str) -> list[str]:
    """The vocabulary terms `text` carries, in order of first appearance.

    A term found only INSIDE a longer term's match is not a second term:
    "Django REST Framework" is one thing the advert asks for, not "Django"
    and "Django REST Framework" both."""
    spans = []
    for term, pattern in _TERM_RES.items():
        for match in pattern.finditer(text or ""):
            spans.append((match.start(), match.end(), term))
    first: dict[str, int] = {}
    for start, end, term in spans:
        inside = any(s <= start and end <= e and (s, e) != (start, end)
                     for s, e, other in spans if other != term)
        if inside:
            continue
        if term not in first or start < first[term]:
            first[term] = start
    return [term for term, _pos in sorted(first.items(), key=lambda kv: kv[1])]


@dataclasses.dataclass(frozen=True)
class Coverage:
    """Which of the posting's terms the letter and the CV carry.

    `cv_known` is False when no CV text could be read at all: the line then
    counts the letter alone and says so, instead of stating as fact that the
    CV lacks every term."""

    terms: tuple[str, ...]
    in_letter: tuple[str, ...]
    in_cv: tuple[str, ...]
    cv_known: bool = True

    @property
    def missing(self) -> tuple[str, ...]:
        carried = set(self.in_letter) | (set(self.in_cv) if self.cv_known else set())
        return tuple(t for t in self.terms if t not in carried)

    def line(self) -> str:
        """The sentence the Postausgang prints. Empty when the posting
        names no term this vocabulary knows — a count of zero of zero says
        nothing. The letter argues with three or four terms; the CV carries
        the rest, which is why both counts stand side by side."""
        if not self.terms:
            return ""
        n = len(self.terms)
        text = f"Begriffe aus der Anzeige: {len(self.in_letter)} von {n} im Brief"
        if not self.cv_known:
            if self.missing:
                text += " · nicht im Brief: " + ", ".join(self.missing)
            return text + " · Lebenslauf nicht lesbar"
        text += f" · {len(self.in_cv)} im Lebenslauf"
        if self.missing:
            text += (" · weder im Brief noch im Lebenslauf: "
                     + ", ".join(self.missing))
        return text


def coverage(posting: str, letter: str, cv_text: str = "") -> Coverage:
    terms = terms_in(posting)
    letter_terms = set(terms_in(letter))
    cv_terms = set(terms_in(cv_text))
    return Coverage(
        terms=tuple(terms),
        in_letter=tuple(t for t in terms if t in letter_terms),
        in_cv=tuple(t for t in terms if t in cv_terms),
        cv_known=bool((cv_text or "").strip()),
    )


# The phrases German recruiters name when asked how they spot a generated
# letter (HR Praxis 2026, t3n, onapply): polished, interchangeable, saying
# nothing about this candidate. Matched case-insensitively as substrings.
FLOSKELN: tuple[str, ...] = (
    "in der heutigen dynamischen arbeitswelt",
    "in der heutigen digitalen welt",
    "in der heutigen zeit",
    "teamfähig, flexibel",
    "flexibel, kommunikativ",
    "teamfähig und flexibel",
    "reizt mich besonders",
    "reizt mich sehr",
    "hiermit bewerbe ich mich",
    "mit großem interesse habe ich",
    "mit großem interesse",
    "ich bin überzeugt, dass ich",
    "ich bin davon überzeugt, dass ich",
    "genau die kombination, die sie suchen",
    "entspricht meinem verständnis von",
    "für den ich mich besonders begeistere",
    "ich freue mich auf die herausforderung",
    "spannende herausforderung",
    "leidenschaft für",
    "mit leidenschaft",
    "ihr renommiertes unternehmen",
    "ihr innovatives unternehmen",
    "als hochmotivierter",
    "hochmotiviert",
    "ich bin ein teamplayer",
    "meine stärken liegen in",
    "neue herausforderung",
    "neuen herausforderung",
    "hat mich sofort angesprochen",
    "mich beruflich weiterentwickeln",
    # "Sie suchen … ich biete" as a pair; "Sie suchen" alone and "bringe ich
    # mit" alone are ordinary German and must not cost a re-roll.
    "sie suchen einen",
    "sie suchen eine",
)

# The subjunctive close in every one of its shapes ("würde mich freuen",
# "würde ich mich sehr freuen", "würde mich über eine Einladung freuen"), and
# the adjective list ("teamfähig, flexibel und belastbar") that names nothing
# the candidate did. Both are the class, not one spelling of it.
_WUERDE_RE = re.compile(r"würde\w*\s+(?:ich\s+)?mich\b[^.!?]{0,60}?\bfreuen", re.I)
_ADJECTIVES = ("teamfähig", "flexibel", "belastbar", "kommunikativ",
               "zuverlässig", "motiviert", "engagiert")
_ADJ = r"\b(?:" + "|".join(_ADJECTIVES) + r")\w*\b"
_ADJECTIVE_RE = re.compile(_ADJ + r"(?:[^.!?]{0,40}?" + _ADJ + r")+", re.I)


def _flat(text: str) -> str:
    """One line, one space between words — a phrase must not hide behind a
    line-wrap inside a paragraph."""
    return " ".join((text or "").split())


def floskeln(text: str) -> list[str]:
    """The stock phrases `text` carries, in the letter's own words and in
    the order they appear. A phrase inside a longer found phrase is not
    reported twice."""
    flat = _flat(text)
    lowered = flat.lower()
    hits: list[tuple[int, int]] = []
    for phrase in FLOSKELN:
        pos = lowered.find(phrase)
        if pos >= 0:
            hits.append((pos, pos + len(phrase)))
    for pattern in (_WUERDE_RE, _ADJECTIVE_RE):
        for match in pattern.finditer(flat):
            hits.append((match.start(), match.end()))
    hits = [(a, b) for a, b in hits
            if not any((c, d) != (a, b) and c <= a and b <= d for c, d in hits)]
    return [flat[a:b] for a, b in sorted(set(hits))]


_TOKEN_RE = re.compile(rf"[{_WORD_CHARS}]+")


def _tokens(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


def copied_spans(text: str, posting: str, n: int = 8,
                 allowed: str = "") -> list[str]:
    """Runs of `n` or more words the letter shares verbatim with the advert,
    quoted in the letter's own words.

    Mirroring the advert's TERMS is wanted; mirroring its SENTENCES is the
    second tell recruiters name. Punctuation and case are ignored for the
    comparison so a comma moved does not hide the copy. `allowed` is text
    that may be repeated — the job title, which the letter names the way
    the advert does and which "(m/w/d)" alone stretches to three words."""
    flat = _flat(text)
    letter = [(m.start(), m.end(), m.group(0).lower())
              for m in _TOKEN_RE.finditer(flat)]
    advert = _tokens(posting)
    if len(letter) < n or len(advert) < n:
        return []
    grams = {tuple(advert[i:i + n]) for i in range(len(advert) - n + 1)}
    allowed_tokens = _tokens(allowed)
    words = [w for _s, _e, w in letter]
    spans: list[str] = []
    i = 0
    while i <= len(letter) - n:
        if tuple(words[i:i + n]) in grams:
            j = i + n
            while j < len(letter) and tuple(words[j - n + 1:j + 1]) in grams:
                j += 1
            span_words = words[i:j]
            title_repeat = allowed_tokens and _contains(allowed_tokens, span_words)
            # A list of the advert's TERMS in the advert's order is the
            # mirroring the prompt asks for, not a copied sentence: only
            # what is left once the vocabulary is taken out counts as prose.
            prose = [w for w in span_words if w not in _VOCAB_WORDS]
            if not title_repeat and len(prose) >= 4:
                spans.append(flat[letter[i][0]:letter[j - 1][1]])
            i = j
        else:
            i += 1
    return spans


def _contains(haystack: list[str], needle: list[str]) -> bool:
    return any(haystack[k:k + len(needle)] == needle
               for k in range(len(haystack) - len(needle) + 1))


def opening(text: str, words: int = 6) -> str:
    """The first `words` of the first paragraph after the Anrede."""
    paragraphs = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    body = paragraphs[1] if len(paragraphs) > 1 else (paragraphs[0] if paragraphs else "")
    return " ".join(_tokens(body)[:words])


def repeats_an_opening(text: str, previous: list[str], words: int = 6) -> bool:
    """Whether this letter opens the way one of `previous` did — the tell
    a recruiter who received both letters sees at once."""
    mine = opening(text, words)
    if not mine:
        return False
    return any(opening(p, words) == mine for p in previous)


@dataclasses.dataclass(frozen=True)
class Note:
    """One thing a reader would notice, and the sentence that says it."""

    kind: str
    text: str


def notes(letter: str, posting: str, previous: list[str] = (),
          title: str = "") -> list[Note]:
    """What a person would hold against this letter, in German, for the
    Postausgang. Empty when nothing was found. `title` is the posting's
    Stellenbezeichnung, which the letter may repeat verbatim."""
    out: list[Note] = []
    stock = floskeln(letter)
    if stock:
        label = "Floskel" if len(stock) == 1 else "Floskeln"
        out.append(Note("floskel", f"{label}: „" + "“, „".join(stock) + "“"))
    copies = copied_spans(letter, posting, allowed=title)
    if copies:
        shown = copies[0] if len(copies[0]) <= 60 else copies[0][:57] + "…"
        out.append(Note("kopie", f"Wörtlich aus der Anzeige: „{shown}“"))
    if repeats_an_opening(letter, list(previous)):
        out.append(Note("einstieg", "Beginnt wie ein früherer Brief: „"
                        + opening_text(letter) + "“"))
    return out


def opening_text(text: str, words: int = 6) -> str:
    """The opening in the letter's own words, for the note and the hint."""
    paragraphs = [p.strip() for p in (text or "").split("\n\n") if p.strip()]
    body = paragraphs[1] if len(paragraphs) > 1 else (paragraphs[0] if paragraphs else "")
    matches = list(_TOKEN_RE.finditer(body))[:words]
    if not matches:
        return ""
    return body[matches[0].start():matches[-1].end()]


def retry_hint(found: list[Note]) -> str:
    """The English instruction appended to the prompt for the one re-roll."""
    parts = []
    for note in found:
        if note.kind == "floskel":
            parts.append("stock phrases a recruiter reads as generated text: "
                         + note.text.split(": ", 1)[1])
        elif note.kind == "kopie":
            parts.append("a sentence copied from the advert: "
                         + note.text.removeprefix("Wörtlich aus der Anzeige: "))
        elif note.kind == "einstieg":
            parts.append("the same opening as an earlier letter, "
                         + note.text.split(": ", 1)[1]
                         + " (take a different one of the three angles)")
    return ("\n\nNote: your previous draft contained " + "; ".join(parts)
            + ". Write it again without them, keeping every rule above. "
            "Say the same facts in your own words: a plain sentence about "
            "what the candidate did beats any of these phrases.")


_TAG_RE = re.compile(r"<[^>]+>")
_STYLE_RE = re.compile(r"<(style|script)\b.*?</\1>", re.S | re.I)


def text_of_html(path: pathlib.Path | None) -> str:
    """The words of a CV template, for the coverage count. '' when there is
    no readable file — the count then honestly says nothing about the CV."""
    if path is None or not path.is_file():
        return ""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    raw = _STYLE_RE.sub(" ", raw)
    return html.unescape(_TAG_RE.sub(" ", raw))
