"""Letter template rendering: fill {{TOKEN}} placeholders in the user's
personal Anschreiben HTML.

The template file lives in the user's own documents (configured via the
template_path setting) — never in this repository. The token contract:

  {{FIRMA}} {{ANSPRECHPARTNER}} {{STRASSE}} {{PLZ_ORT}}   address block
  {{ORT}}, {{DATUM}}                                       place and date
  {{DECKBLATT_ROLLE}}                                      cover-sheet role
  {{BETREFF}}                                              letter subject
  {{ANSCHREIBEN_BODY}}                                     letter body
  <!--PROFIL-->fixed profile line<!--/PROFIL-->            CV profile line

All values are HTML-escaped (LLM output and posting-derived data are
untrusted for HTML purposes). An empty address token swallows one directly
following <br> so the block does not render blank lines. The body is plain
text with blank-line-separated paragraphs; each becomes a styled <p>.

The profile line is a REGION rather than a token, because it has a fallback
the app cannot know: the two sentences under the name on the CV page are
written per posting when a draft carries them, and stay the template's own
fixed text otherwise (the specimen Mappe, a draft written before this
existed, a template without the region). Keeping the fixed text inside the
markers means the template stays a complete document on its own, and the
one-column CV template carries the same region without acquiring a token
its own build script asserts it has none of.
"""

import html
import re

# Matches the paragraph styling of the surrounding template so injected
# paragraphs are indistinguishable from the original hand-written ones.
BODY_P_STYLE = "font-size:13px;line-height:1.55;color:#2b3640;margin:0 0 10px"

SIMPLE_TOKENS = ("FIRMA", "ANSPRECHPARTNER", "STRASSE", "PLZ_ORT",
                 "ORT", "DATUM", "BETREFF", "DECKBLATT_ROLLE")


class TemplateError(ValueError):
    """The template file is missing required tokens or unusable."""


def letter_address(job) -> tuple[str, str]:
    """(STRASSE, PLZ_ORT) for the employer's address block.

    An address the posting itself states wins: it was extracted from the text
    the employer wrote, and where a posting names a postal address it is the one
    it wants applications sent to. Where there is none — 725 of his 769 postings
    state no address at all — the board's structured WORK address stands in,
    which is deterministic and beats an LLM reading prose.

    Except for Arbeitnehmerüberlassung, where it is refused: the employer there
    is a staffing firm and the work location is somebody else's site, so the
    letter would be addressed to a company that is not the recipient. Half an
    address block is better than a confidently wrong one.
    """
    strasse = str(job["contact_strasse"] or "").strip()
    plz_ort = str(job["contact_plz_ort"] or "").strip()
    if strasse or plz_ort:
        return strasse, plz_ort
    if job["temp_agency"]:
        return "", ""
    return (str(job["work_strasse"] or "").strip(),
            str(job["work_plz_ort"] or "").strip())


def body_paragraphs_html(body_text: str) -> str:
    """Plain text with blank-line paragraph breaks → styled <p> blocks."""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body_text) if p.strip()]
    return "\n".join(
        f'<p style="{BODY_P_STYLE}">{html.escape(p).replace(chr(10), "<br>")}</p>'
        for p in paragraphs
    )


_TOKEN_RE = re.compile(
    r"\{\{(" + "|".join((*SIMPLE_TOKENS, "ANSCHREIBEN_BODY")) + r")\}\}(<br>)?"
)

_TAG_RE = re.compile(r"<[^>]+>")
PROFIL_OPEN = "<!--PROFIL-->"
PROFIL_CLOSE = "<!--/PROFIL-->"
_PROFIL_RE = re.compile(re.escape(PROFIL_OPEN) + r"(.*?)" + re.escape(PROFIL_CLOSE),
                        re.S)


def profil_default(template_html: str) -> str:
    """The fixed profile line a template carries between its PROFIL markers,
    as plain text; '' when the template has no such region."""
    match = _PROFIL_RE.search(template_html)
    if match is None:
        return ""
    return " ".join(html.unescape(_TAG_RE.sub(" ", match.group(1))).split())


def strip_profil(template_html: str) -> str:
    """The template without its PROFIL regions, markers and text — for
    counting the CV's words apart from the line a draft replaces."""
    return _PROFIL_RE.sub(" ", template_html)


def fill_profil(template_html: str, profil: str | None) -> str:
    """The template with every PROFIL region carrying `profil` instead of
    the fixed text (a template may print the line on more than one page).
    An empty `profil` or a template without the region leaves the document
    exactly as it is — the fixed line IS the fallback.

    The value is HTML-escaped and flattened to one paragraph: it is LLM
    output, and the region sits inside a single <p>."""
    text = " ".join((profil or "").split())
    if not text:
        return template_html
    replacement = PROFIL_OPEN + html.escape(text) + PROFIL_CLOSE
    return _PROFIL_RE.sub(lambda _m: replacement, template_html)


def render_letter(template_html: str, values: dict) -> str:
    """Fill the token contract. `values` keys are lowercase token names
    (firma, ansprechpartner, ..., betreff, anschreiben_body, profil).

    Substitution is a single pass over the template: a substituted value
    that itself contains token-shaped text stays literal instead of being
    re-substituted (values are posting/LLM-derived and untrusted). The
    profile region is filled AFTER that pass for the same reason in the
    other direction: a profile line containing token-shaped text must not
    meet the token pass, and every token value is escaped before the region
    pass, so neither can open the other."""
    if "{{ANSCHREIBEN_BODY}}" not in template_html:
        raise TemplateError(
            "template has no {{ANSCHREIBEN_BODY}} token — re-run the "
            "tokenization step on it"
        )
    body_html = body_paragraphs_html(str(values.get("anschreiben_body", "") or ""))

    def fill(match: re.Match) -> str:
        token, br = match.group(1), match.group(2) or ""
        if token == "ANSCHREIBEN_BODY":
            return body_html + br
        value = html.escape(str(values.get(token.lower(), "") or "").strip())
        # An empty value also removes one <br> right after the token, so
        # empty address lines collapse instead of leaving gaps.
        return value + br if value else ""

    return fill_profil(_TOKEN_RE.sub(fill, template_html), values.get("profil"))
