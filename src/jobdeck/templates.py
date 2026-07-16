"""Letter template rendering: fill {{TOKEN}} placeholders in the user's
personal Anschreiben HTML.

The template file lives in the user's own documents (configured via the
template_path setting) — never in this repository. The token contract:

  {{FIRMA}} {{ANSPRECHPARTNER}} {{STRASSE}} {{PLZ_ORT}}   address block
  {{ORT}}, {{DATUM}}                                       place and date
  {{BETREFF}}                                              letter subject
  {{ANSCHREIBEN_BODY}}                                     letter body

All values are HTML-escaped (LLM output and posting-derived data are
untrusted for HTML purposes). An empty address token swallows one directly
following <br> so the block does not render blank lines. The body is plain
text with blank-line-separated paragraphs; each becomes a styled <p>.
"""

import html
import re

# Matches the paragraph styling of the surrounding template so injected
# paragraphs are indistinguishable from the original hand-written ones.
BODY_P_STYLE = "font-size:13px;line-height:1.55;color:#2b3640;margin:0 0 10px"

SIMPLE_TOKENS = ("FIRMA", "ANSPRECHPARTNER", "STRASSE", "PLZ_ORT",
                 "ORT", "DATUM", "BETREFF")


class TemplateError(ValueError):
    """The template file is missing required tokens or unusable."""


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


def render_letter(template_html: str, values: dict) -> str:
    """Fill the token contract. `values` keys are lowercase token names
    (firma, ansprechpartner, ..., betreff, anschreiben_body).

    Substitution is a single pass over the template: a substituted value
    that itself contains token-shaped text stays literal instead of being
    re-substituted (values are posting/LLM-derived and untrusted)."""
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

    return _TOKEN_RE.sub(fill, template_html)
