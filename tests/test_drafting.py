import pytest

from jobdeck import config, db
from jobdeck.ai import drafting as ai_drafting
from jobdeck.ai import llm
from jobdeck.services import drafting


# -- betreff (built in code, never by the LLM) --------------------------------
def test_build_betreff_full_and_partial():
    assert ai_drafting.build_betreff(
        "Python Entwickler (m/w/d)", "K-2026-17", "Max Muster"
    ) == "Bewerbung als Python Entwickler (m/w/d), K-2026-17 – Max Muster"
    assert ai_drafting.build_betreff("Dev", "", "Max Muster") \
        == "Bewerbung als Dev – Max Muster"
    assert ai_drafting.build_betreff("Dev", "K-1") == "Bewerbung als Dev, K-1"
    assert ai_drafting.build_betreff(" Dev ") == "Bewerbung als Dev"


def test_append_signature_puts_the_contact_block_under_the_closing():
    """Built in code, never by the LLM: one mistyped character in a URL or
    phone number costs a reply and no reviewer reliably spots it."""
    body = "Guten Tag,\n\nanbei meine Bewerbung.\n\nMit freundlichen Grüßen\nMax Muster"
    sig = "linkedin.com/in/max\ngithub.com/max\nmax.example\n+49 111 222"
    out = ai_drafting.append_signature(body, sig)
    assert out == body + "\n\n" + sig
    assert out.index("Mit freundlichen Grüßen") < out.index("linkedin.com")


@pytest.mark.parametrize("sig", ["", "   ", "\n\n", None])
def test_append_signature_without_one_leaves_the_body_alone(sig):
    body = "Guten Tag,\n\nText.\n\nMit freundlichen Grüßen\nMax Muster"
    assert ai_drafting.append_signature(body, sig) == body


def test_append_signature_does_not_stack_blank_lines():
    out = ai_drafting.append_signature("Text.\n\n\n", "github.com/max")
    assert out == "Text.\n\ngithub.com/max"


def test_letter_betreff_drops_only_the_name_suffix():
    assert ai_drafting.letter_betreff(
        "Bewerbung als Python Entwickler (m/w/d), K-17 – Max Muster", "Max Muster"
    ) == "Bewerbung als Python Entwickler (m/w/d), K-17"
    # a user-corrected subject survives verbatim
    assert ai_drafting.letter_betreff("Bewerbung als Dev, K-99 – Max Muster",
                                      "Max Muster") == "Bewerbung als Dev, K-99"
    # no name configured, or a subject that never carried it
    assert ai_drafting.letter_betreff("Bewerbung als Dev", "") \
        == "Bewerbung als Dev"
    assert ai_drafting.letter_betreff("Bewerbung als Dev", "Max Muster") \
        == "Bewerbung als Dev"
    # the name must only be stripped as the trailing suffix
    assert ai_drafting.letter_betreff("Bewerbung als Max Muster Nachfolge",
                                      "Max Muster") \
        == "Bewerbung als Max Muster Nachfolge"


def test_deckblatt_rolle_cannot_contradict_the_letter_subject():
    """Cover sheet and Betreff come from ONE string: page 1 naming a
    different Stelle than page 2 is the classic copy-paste tell."""
    betreff = "Bewerbung als Full-Stack Entwickler m/w/d, K-17 – Max Muster"
    assert ai_drafting.deckblatt_rolle(betreff, "Max Muster") \
        == "als Full-Stack Entwickler m/w/d, K-17"
    # whatever the user corrects in the subject follows onto the cover sheet
    corrected = "Bewerbung als Backend Entwickler, K-99 – Max Muster"
    assert ai_drafting.deckblatt_rolle(corrected, "Max Muster") \
        == "als Backend Entwickler, K-99"
    # the role always matches the letter's own Betreff, minus the lead-in
    for subject in (betreff, corrected):
        letter = ai_drafting.letter_betreff(subject, "Max Muster")
        assert letter.removeprefix("Bewerbung ") == \
            ai_drafting.deckblatt_rolle(subject, "Max Muster")


def test_resolve_refnr_prefers_extraction_then_arbeitsagentur_id():
    assert drafting.resolve_refnr(
        {"refnr": "K-9", "source": "arbeitsagentur", "external_id": "10001-X"}
    ) == "K-9"
    assert drafting.resolve_refnr(
        {"refnr": "", "source": "arbeitsagentur", "external_id": "10001-X"}
    ) == "10001-X"
    assert drafting.resolve_refnr(
        {"refnr": "", "source": "jooble", "external_id": "12345"}
    ) == ""


# -- drafting prompt -----------------------------------------------------------
def test_system_prompt_keeps_the_attribution_fidelity_contract():
    """Deletion tripwire, NOT a behavioural proof — a stub cannot exercise
    the model, so a real letter's fidelity is checked by the live smoke.

    The fix for the misattribution class (a true skill welded onto the
    wrong project — "Django in zwei Praktika", Alembic under the Java
    project) lives entirely in these prompt rules, so guard them against a
    silent drop in a future rewrite: the binding rule, its skill-level
    escape hatch, the count-inflation guard, and the posting-wall clause
    (no candidate facts come from the untrusted posting) must all survive."""
    prompt = " ".join(ai_drafting.SYSTEM_PROMPT.lower().split())  # wrap-robust
    assert "attribution fidelity" in prompt
    assert "skill level" in prompt  # the escape hatch for an unbound skill
    assert "from one project into a sentence about another" in prompt
    assert "one occurrence into" in prompt  # the count-inflation guard
    assert "never supplies new facts about the candidate" in prompt


def test_system_prompt_analyses_first_and_positions_for_the_role():
    """The Sonnet rewrite must keep the analysis-first + role-positioning +
    clean-Stellenbezeichnung + flawless-German contract, not only the
    attribution guards — a silent drop of any of these is the regression."""
    prompt = " ".join(ai_drafting.SYSTEM_PROMPT.lower().split())
    assert "analysis" in prompt and "stellenbezeichnung" in prompt
    assert "leading with the competences the posting weights most" in prompt
    assert "flawless german" in prompt  # the anti-typo instruction


# -- drafting module -----------------------------------------------------------
def _job(**over):
    values = dict(
        title="Python Developer", company="Firma GmbH", location="Berlin",
        remote=0, description="Python, FastAPI, pytest", refnr="K-17",
        ansprechpartner="Frau Weber",
    )
    values.update(over)
    return values


def test_build_user_content_fences_posting_and_names_contact():
    job = _job(description="x" * (ai_drafting.MAX_DESCRIPTION_CHARS + 100))
    content = ai_drafting.build_user_content(
        job, "my profile", refnr="K-17", applicant_name="Erika Muster"
    )
    assert "my profile" in content
    assert "Name: Erika Muster" in content
    assert "Referenznummer: K-17" in content  # the resolved one, as in the Betreff
    assert "Ansprechpartner: Frau Weber" in content
    assert content.count("x") == ai_drafting.MAX_DESCRIPTION_CHARS
    assert content.index("<<<POSTING START>>>") < content.index("x" * 10)
    assert content.rstrip().endswith("<<<POSTING END>>>")


def test_a_letter_written_from_no_advert_is_told_there_is_none():
    """The system prompt opens with "analyse the posting" and asks first which
    competences THIS posting prioritises. With no advert that step has nothing
    to work on, and the letter it produced on a real posting answered not one
    requirement of the role while reading as though it had. 27 stored postings
    hold no advert and 21 of them already carry a letter."""
    content = ai_drafting.build_user_content(
        _job(description=""), "my profile", refnr="K-17",
        applicant_name="Erika Muster")
    assert "NO advert text is available" in content
    assert "Do not describe requirements, tasks or priorities" in content
    # The format spec it has to live beside says "roughly half a page
    # (150-220 words)", "3-4 paragraphs" and "open on why this role at this
    # company fits". Two of those are impossible with no advert, so the note
    # must say WHICH it overrides — an unresolved contradiction in a prompt is
    # a coin flip, and the losing side is either the pretence coming back or a
    # letter too short for the page it is printed on. A conspicuously short
    # Anschreiben reads to German HR as disinterest.
    assert "OVERRIDES the parts of the format spec" in content
    assert "keep the stated length" in content
    assert "shorter letter" not in content
    # …and the e-mail's mandatory hook, which the spec ties to "the domain the
    # posting foregrounds" — nothing an absent advert can supply.
    assert "The e-mail's hook sentence must rest on the role title" in content
    assert "leave the hook out" in content
    # after the fence, never inside it: a note the posting could forge would
    # be an instruction from untrusted text
    assert content.index("<<<POSTING END>>>") < content.index("NO advert text")
    assert "SEARCH-RESULT SNIPPET" not in content


def test_a_letter_written_from_a_fragment_is_told_it_is_one():
    """The elided half is not an absence to answer for. 178 stored postings
    are a search fragment, and the letter path had no caution at all."""
    content = ai_drafting.build_user_content(
        _job(description="Deine Mission - Du entwickelst das Herz unser..."),
        "my profile", refnr="K-17", applicant_name="Erika Muster")
    assert "SEARCH-RESULT SNIPPET" in content
    assert "do not treat the missing part as a requirement" in content
    assert "NO advert text is available" not in content
    assert content.index("<<<POSTING END>>>") < content.index("SEARCH-RESULT")


def test_a_whole_advert_adds_no_note_and_the_prompt_ends_at_the_fence():
    content = ai_drafting.build_user_content(
        _job(description="Wir suchen eine Entwicklerin. " * 40),
        "my profile", refnr="K-17", applicant_name="Erika Muster")
    assert "SEARCH-RESULT SNIPPET" not in content
    assert "NO advert text is available" not in content
    assert content.rstrip().endswith("<<<POSTING END>>>")


def test_build_betreff_collapses_smuggled_whitespace():
    # posting-derived title must not inject newlines into a subject line
    assert ai_drafting.build_betreff("Dev\nX-Evil: 1", "K\n1", "Max\tMuster") \
        == "Bewerbung als Dev X-Evil: 1, K 1 – Max Muster"


def test_clean_title_strips_board_noise_but_keeps_the_role():
    # the exact Stretta-style title that leaked junk into a real Betreff
    assert ai_drafting.clean_title(
        "Ab sofort: Fullstack-Entwickler Python/Django mit Frontend-Fokus "
        "(m/w/d)Vollzeit"
    ) == "Fullstack-Entwickler Python/Django mit Frontend-Fokus (m/w/d)"
    # a clean title (and its (m/w/d) marker) is left untouched
    assert ai_drafting.clean_title("Full-Stack Entwickler m/w/d") \
        == "Full-Stack Entwickler m/w/d"
    # employment-type token dropped whether glued or spaced
    assert ai_drafting.clean_title("Backend Developer (m/w/d) Vollzeit") \
        == "Backend Developer (m/w/d)"
    assert ai_drafting.clean_title("Neu: Python Entwickler in Teilzeit") \
        == "Python Entwickler"
    # all-noise input collapses to empty (the service then falls back to the
    # raw title); None/empty are safe
    assert ai_drafting.clean_title("Ab sofort: Vollzeit") == ""
    assert ai_drafting.clean_title("") == ""
    assert ai_drafting.clean_title(None) == ""


def test_build_betreff_cleans_board_noise_from_the_title():
    # even the raw-title fallback path yields a clean subject line
    assert ai_drafting.build_betreff(
        "Ab sofort: Fullstack-Entwickler (m/w/d)Vollzeit", "K-9", "Max Muster"
    ) == "Bewerbung als Fullstack-Entwickler (m/w/d), K-9 – Max Muster"


def _draft_text(analysis="notes", stellenbezeichnung="Dev",
                anschreiben_body="Anrede,\n\nText.",
                email_body="Guten Tag,\n\nMit freundlichen Grüßen\nX"):
    """A marker-delimited drafting response — the plain-text shape Sonnet
    returns. Drafting uses NO JSON schema: constrained decoding degraded the
    long German prose fields, so sections are delimited by markers instead."""
    return (
        f"===ANALYSIS===\n{analysis}\n"
        f"===STELLENBEZEICHNUNG===\n{stellenbezeichnung}\n"
        f"===ANSCHREIBEN_BODY===\n{anschreiben_body}\n"
        f"===EMAIL_BODY===\n{email_body}\n"
        f"===END==="
    )


def test_draft_application_parses_and_strips(monkeypatch):
    captured = {}

    def fake_complete(**kwargs):
        captured.update(kwargs)
        # surrounding whitespace on every section proves the parser strips it
        return llm.LLMResult(
            text=_draft_text(
                analysis="internal reasoning",
                stellenbezeichnung=" Backend Developer (m/w/d) ",
                anschreiben_body=" Sehr geehrte Frau Weber,\n\nAbsatz. ",
                email_body=" Guten Tag,\n\nMit freundlichen Grüßen\nMax ",
            ),
            model="m", input_tokens=5, output_tokens=5, cost_usd=0.0,
        )

    monkeypatch.setattr(llm, "complete", fake_complete)
    anschreiben, email_body, stellenbezeichnung, usage = ai_drafting.draft_application(
        _job(), "profil"
    )
    assert anschreiben.startswith("Sehr geehrte Frau Weber,")
    assert email_body.endswith("Mit freundlichen Grüßen\nMax")  # stripped
    assert stellenbezeichnung == "Backend Developer (m/w/d)"  # stripped
    assert usage.input_tokens == 5
    # drafting runs on the stronger drafting model, not the scoring default
    assert captured["model"] == config.anthropic_drafting_model()
    assert captured["timeout"] == ai_drafting.DRAFT_TIMEOUT_S
    # drafting must NOT constrain decoding with a JSON schema (the whole point
    # of the plain-text format) — guard against a structured-output regression
    assert "output_schema" not in captured and "output_config" not in captured


@pytest.mark.parametrize("text", [
    "no section markers here at all",                    # unparseable
    _draft_text(anschreiben_body="", email_body="x"),    # parses, but empty body
])
def test_draft_application_rejects_unusable_response(monkeypatch, text):
    calls = []

    def fake_complete(**kwargs):
        calls.append(1)
        return llm.LLMResult(
            text=text, model="m", input_tokens=1, output_tokens=1, cost_usd=0.001,
        )

    monkeypatch.setattr(llm, "complete", fake_complete)
    with pytest.raises(llm.LLMError) as excinfo:
        ai_drafting.draft_application(_job(), "profil")
    # every attempt is retried up to the cap and metered in full
    assert len(calls) == ai_drafting.DRAFT_ATTEMPTS
    assert excinfo.value.usage is not None
    assert excinfo.value.usage.cost_usd == pytest.approx(0.001 * ai_drafting.DRAFT_ATTEMPTS)


def test_a_truncated_attempt_is_retried_with_MORE_ROOM(monkeypatch):
    """max_tokens bounds adaptive thinking plus the letter, and the letter is
    the small half — one real posting produced a finished ~1000-token draft
    after ~8300 tokens of thinking. So a truncation means the budget was too
    small, and the retry has to raise it: re-rolling the identical request at
    the identical cap is the one retry guaranteed to fail again.

    Every billed attempt is still metered so the cost is not under-reported."""
    good = _draft_text()
    caps = []

    def fake_complete(**kwargs):
        caps.append(kwargs["max_tokens"])
        if len(caps) == 1:
            raise llm.LLMError(
                "response truncated at max_tokens=12000",
                usage=llm.LLMResult(text="", model="m", input_tokens=100,
                                    output_tokens=12000, cost_usd=0.12),
                truncated=True,
            )
        return llm.LLMResult(text=good, model="m", input_tokens=100,
                             output_tokens=300, cost_usd=0.006)

    monkeypatch.setattr(llm, "complete", fake_complete)
    anschreiben, _, stellen, usage = ai_drafting.draft_application(_job(), "profil")
    assert anschreiben.startswith("Anrede,") and stellen == "Dev"
    assert caps == [ai_drafting.DRAFT_MAX_TOKENS, ai_drafting.DRAFT_MAX_TOKENS * 2]
    assert usage.output_tokens == 12000 + 300
    assert usage.cost_usd == pytest.approx(0.12 + 0.006)


def test_a_failure_a_fresh_sample_CAN_fix_is_retried_at_the_same_cap(monkeypatch):
    """The other half of the rule: an unparseable response is randomness, not
    the cap, so it gets another roll at the same budget rather than a bigger
    bill."""
    caps = []

    def fake_complete(**kwargs):
        caps.append(kwargs["max_tokens"])
        if len(caps) == 1:
            return llm.LLMResult(text="kein Marker", model="m", input_tokens=10,
                                 output_tokens=20, cost_usd=0.001)
        return llm.LLMResult(text=_draft_text(), model="m", input_tokens=10,
                             output_tokens=20, cost_usd=0.001)

    monkeypatch.setattr(llm, "complete", fake_complete)
    ai_drafting.draft_application(_job(), "profil")
    assert caps == [ai_drafting.DRAFT_MAX_TOKENS, ai_drafting.DRAFT_MAX_TOKENS]


def test_repeated_truncation_stops_at_the_ceiling_instead_of_burning_attempts(
    monkeypatch
):
    """A one-page letter that will not fit in the ceiling is pathological, and
    paying twice more to confirm it is the bug being fixed: one real posting
    burned 4 attempts, 225 s and $0.3955 producing nothing — more than the five
    successful drafts of that run cost together. It fails closed, having
    escalated once, and every billed attempt is still metered."""
    caps = []

    def fake_complete(**kwargs):
        caps.append(kwargs["max_tokens"])
        raise llm.LLMError(
            f"response truncated at max_tokens={kwargs['max_tokens']}",
            usage=llm.LLMResult(text="", model="m", input_tokens=50,
                                output_tokens=5000, cost_usd=0.08),
            truncated=True,
        )

    monkeypatch.setattr(llm, "complete", fake_complete)
    with pytest.raises(llm.LLMError) as excinfo:
        ai_drafting.draft_application(_job(), "profil")
    assert caps == [ai_drafting.DRAFT_MAX_TOKENS, ai_drafting.DRAFT_MAX_TOKENS_CEILING]
    assert len(caps) < ai_drafting.DRAFT_ATTEMPTS  # stopped early, on purpose
    assert "after" in str(excinfo.value)
    assert excinfo.value.usage.output_tokens == 5000 * len(caps)
    assert excinfo.value.usage.cost_usd == pytest.approx(0.08 * len(caps))


def test_draft_application_retries_an_email_without_a_closing(monkeypatch):
    """A garbled/cut-off e-mail (parseable sections but no 'Mit freundlichen
    Grüßen') is rejected and retried — Sonnet's other degeneration mode."""
    calls = []
    bad = _draft_text(email_body="Guten Tag,\n\nanbei meine Bewerbung (")  # cut off
    good = _draft_text()  # complete, signs off with "Mit freundlichen Grüßen"

    def fake_complete(**kwargs):
        calls.append(1)
        text = bad if len(calls) == 1 else good
        return llm.LLMResult(text=text, model="m", input_tokens=10,
                             output_tokens=100, cost_usd=0.005)

    monkeypatch.setattr(llm, "complete", fake_complete)
    _, email_body, _, _ = ai_drafting.draft_application(_job(), "profil")
    assert "Grüßen" in email_body
    assert len(calls) == 2  # the incomplete e-mail was retried


# -- plain-text parser hardening (regression guards from the review panel) -----
def test_parse_draft_sections_requires_every_content_marker():
    """A response missing ANY one of the four content markers is unparseable —
    pins all() (not any()): under any(), a 3-of-4 response would return a
    partial dict and draft_application would ship it or raise a raw KeyError."""
    full = _draft_text()
    assert ai_drafting.parse_draft_sections(full) is not None  # control
    for marker in ("===ANALYSIS===", "===STELLENBEZEICHNUNG===",
                   "===ANSCHREIBEN_BODY===", "===EMAIL_BODY==="):
        # drop just the marker line (its body stays) — a real 3-of-4 sample
        partial = "\n".join(
            ln for ln in full.splitlines() if ln.strip() != marker
        )
        assert ai_drafting.parse_draft_sections(partial) is None, marker


def test_parse_draft_sections_requires_the_end_terminator():
    """Without ===END=== the e-mail body runs unbounded to the end of the
    response (trailing chatter / a code fence would leak in) — reject it."""
    no_end = _draft_text().replace("\n===END===", "")
    assert "===END===" not in no_end
    assert ai_drafting.parse_draft_sections(no_end) is None


def test_parse_draft_sections_rejects_a_duplicated_marker():
    """A marker emitted twice (a degenerate loop, or a marker echoed from the
    posting into the letter) is a bad sample — reject it, never silently resolve
    it last-wins and ship a truncated/attacker-influenced section."""
    doubled = _draft_text(anschreiben_body="Anrede,\n\n===EMAIL_BODY===\n\nText.")
    assert ai_drafting.parse_draft_sections(doubled) is None


def test_parse_draft_sections_ignores_a_stray_single_equals_line():
    """The tightened marker (>=3 '=') means a prose line like '= EMAIL_BODY ='
    is NOT a delimiter — it stays visible body text, never a silent truncation."""
    body = "Anrede,\n\n= EMAIL_BODY =\n\nText."
    sections = ai_drafting.parse_draft_sections(_draft_text(anschreiben_body=body))
    assert sections is not None
    assert sections["anschreiben_body"] == body  # nothing was cut at the fake line


def test_parse_draft_sections_bounds_email_body_at_the_end_terminator():
    """===END=== bounds the e-mail: a whole-output code fence (its closing ```
    lands after ===END===) does not leak a stray ``` into the sent e-mail."""
    fenced = "```\n" + _draft_text() + "\n```"
    sections = ai_drafting.parse_draft_sections(fenced)
    assert sections is not None
    assert "`" not in sections["email_body"]
    assert sections["email_body"] == "Guten Tag,\n\nMit freundlichen Grüßen\nX"


@pytest.mark.parametrize("dropped", ["===EMAIL_BODY===", "===END==="])
def test_draft_application_retries_a_partial_response(monkeypatch, dropped):
    """A 3-of-4 / missing-terminator response is retried to exhaustion — it must
    NEVER raise a raw KeyError out of draft_application nor ship a partial."""
    full = _draft_text()
    partial = "\n".join(ln for ln in full.splitlines() if ln.strip() != dropped)
    calls = []

    def fake_complete(**kwargs):
        calls.append(1)
        return llm.LLMResult(text=partial, model="m", input_tokens=1,
                             output_tokens=1, cost_usd=0.001)

    monkeypatch.setattr(llm, "complete", fake_complete)
    with pytest.raises(llm.LLMError):
        ai_drafting.draft_application(_job(), "profil")
    assert len(calls) == ai_drafting.DRAFT_ATTEMPTS


@pytest.mark.parametrize("anschreiben", ["", "   \n  "])
def test_draft_application_retries_an_empty_anschreiben(monkeypatch, anschreiben):
    """An empty/whitespace-only Anschreiben with an otherwise valid e-mail is
    rejected by the empty-body guard and retried — the guard is exercised here
    (the e-mail carries a proper closing, so the grüßen check does not mask it)."""
    text = _draft_text(anschreiben_body=anschreiben)  # valid grüßen-bearing email
    calls = []

    def fake_complete(**kwargs):
        calls.append(1)
        return llm.LLMResult(text=text, model="m", input_tokens=1,
                             output_tokens=1, cost_usd=0.001)

    monkeypatch.setattr(llm, "complete", fake_complete)
    with pytest.raises(llm.LLMError):
        ai_drafting.draft_application(_job(), "profil")
    assert len(calls) == ai_drafting.DRAFT_ATTEMPTS


# -- drafting service ----------------------------------------------------------
def _insert_job(con, **over):
    values = dict(
        source="stub", external_id=over.pop("external_id", "j1"),
        title="Python Dev", company="Firma", description="desc",
        contact_email="hr@firma.de",
    )
    values.update(over)
    return db.insert_job_if_new(con, values)


def _usage(cost=0.002):
    return llm.LLMResult(
        text="", model="claude-haiku-4-5",
        input_tokens=100, output_tokens=200, cost_usd=cost,
    )


@pytest.fixture()
def ai_on(con):
    db.set_setting(con, "ai_enabled", "1")
    con.commit()


@pytest.fixture()
def applicant(con):
    db.set_setting(con, "applicant_name", "Max Muster")
    con.commit()


@pytest.fixture()
def profile_file(data_dir):
    config.PROFILE_PATH.write_text("Python developer, 3 years", encoding="utf-8")


def _must_not_be_called(job, profile_text, refnr="", applicant_name="", previous_letters=None):
    raise AssertionError("LLM called although a gate should have fired")


async def test_gates_fire_in_order_without_spend(con, monkeypatch):
    monkeypatch.setattr("jobdeck.ai.drafting.draft_application", _must_not_be_called)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()

    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and "AI is disabled" in result["error"]

    db.set_setting(con, "ai_enabled", "1")
    con.commit()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and "ANTHROPIC_API_KEY" in result["error"]

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    result = await drafting.draft_for_job(job_id)  # no profile.md yet
    assert not result["ok"] and "profile" in result["error"]

    config.PROFILE_PATH.write_text("profil", encoding="utf-8")
    result = await drafting.draft_for_job(job_id)  # no applicant name yet
    assert not result["ok"] and "applicant name" in result["error"]

    assert db.get_setting(con, "llm_calls", "0") == "0"
    assert db.get_draft_by_job(con, job_id) is None


async def test_successful_draft_is_persisted_and_metered(
    con, ai_on, applicant, profile_file, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    db.set_job_contacts(  # the real flow: extraction filled these
        con, job_id, {"refnr": "K-17", "ansprechpartner": "Frau Weber"}
    )
    con.commit()

    monkeypatch.setattr(
        "jobdeck.ai.drafting.draft_application",
        lambda job, profile_text, refnr="", applicant_name="", previous_letters=None:
            ("Sehr geehrte Frau Weber,\n\nAbsatz.",
             "Guten Tag,\n\nanbei meine Bewerbung.\n\n"
             "Mit freundlichen Grüßen\nMax Muster",
             "Backend Developer", _usage()),
    )

    result = await drafting.draft_for_job(job_id)
    assert result["ok"], result["error"]
    draft = result["draft"]
    assert draft["status"] == "ready"
    # the Betreff is built from the LLM's clean Stellenbezeichnung (not the raw
    # job title "Python Dev"); the Refnr + name are code-supplied
    assert draft["betreff"] == "Bewerbung als Backend Developer, K-17 – Max Muster"
    assert draft["recipient"] == "hr@firma.de"
    assert draft["anschreiben_body"].startswith("Sehr geehrte Frau Weber,")
    assert draft["llm_model"] == "claude-haiku-4-5"

    assert db.get_setting(con, "llm_calls") == "1"
    assert float(db.get_setting(con, "llm_cost_usd")) == pytest.approx(0.002)
    assert db.get_draft_by_job(con, job_id)["status"] == "ready"


async def test_drafted_email_carries_the_configured_signature(
    con, ai_on, applicant, profile_file, monkeypatch
):
    """The contact block must reach the draft, so the review queue shows
    exactly what will be sent."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    db.set_setting(con, "email_signature",
                   "linkedin.com/in/max\ngithub.com/max\n+49 111 222")
    job_id = _insert_job(con)
    con.commit()
    monkeypatch.setattr(
        "jobdeck.ai.drafting.draft_application",
        lambda job, profile_text, refnr="", applicant_name="", previous_letters=None:
            ("Anrede,\n\nText.", "Guten Tag,\n\nanbei meine Bewerbung.\n\n"
                                 "Mit freundlichen Grüßen\nMax Muster", "", _usage()),
    )

    result = await drafting.draft_for_job(job_id)
    body = result["draft"]["email_body"]
    assert body.endswith("linkedin.com/in/max\ngithub.com/max\n+49 111 222")
    assert body.index("Mit freundlichen Grüßen") < body.index("linkedin.com")


async def test_no_signature_configured_leaves_the_email_untouched(
    con, ai_on, applicant, profile_file, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()
    monkeypatch.setattr(
        "jobdeck.ai.drafting.draft_application",
        lambda job, profile_text, refnr="", applicant_name="", previous_letters=None:
            ("Anrede,\n\nText.", "Guten Tag,\n\nMit freundlichen Grüßen\nMax",
             "", _usage()),
    )

    result = await drafting.draft_for_job(job_id)
    assert result["draft"]["email_body"] == "Guten Tag,\n\nMit freundlichen Grüßen\nMax"


async def test_empty_stellenbezeichnung_falls_back_to_the_cleaned_job_title(
    con, ai_on, applicant, profile_file, monkeypatch
):
    """No Stellenbezeichnung from the LLM → Betreff built from the raw job
    title (board noise cleaned), never an empty 'Bewerbung als '."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con, title="Ab sofort: Python Dev (m/w/d)Vollzeit")
    db.set_job_contacts(con, job_id, {"refnr": "K-9"})
    con.commit()
    monkeypatch.setattr(
        "jobdeck.ai.drafting.draft_application",
        lambda job, profile_text, refnr="", applicant_name="", previous_letters=None:
            ("Anrede,\n\nText.", "Mail.", "", _usage()),  # empty stellenbezeichnung
    )
    result = await drafting.draft_for_job(job_id)
    assert result["ok"], result["error"]
    assert result["draft"]["betreff"] == \
        "Bewerbung als Python Dev (m/w/d), K-9 – Max Muster"


async def test_failed_draft_is_recorded_and_metered(
    con, ai_on, applicant, profile_file, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()

    def failing(job, profile_text, refnr="", applicant_name="", previous_letters=None):
        raise llm.LLMError("unparseable", usage=_usage(cost=0.003))

    monkeypatch.setattr("jobdeck.ai.drafting.draft_application", failing)

    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and "drafting failed" in result["error"]
    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == "failed"
    assert "unparseable" in draft["error"]
    assert float(db.get_setting(con, "llm_cost_usd")) == pytest.approx(0.003)

    # a failed draft is re-claimable immediately — the user may retry
    monkeypatch.setattr(
        "jobdeck.ai.drafting.draft_application",
        lambda job, profile_text, refnr="", applicant_name="", previous_letters=None:
            ("Anrede,\n\nText.", "Mail.", "", _usage()),
    )
    result = await drafting.draft_for_job(job_id)
    assert result["ok"]
    assert db.get_draft_by_job(con, job_id)["status"] == "ready"


async def test_redraft_clears_stale_pdf_path(
    con, ai_on, applicant, profile_file, monkeypatch
):
    """A regenerated draft must not keep pointing at the OLD letter's PDF."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()
    monkeypatch.setattr(
        "jobdeck.ai.drafting.draft_application",
        lambda job, profile_text, refnr="", applicant_name="", previous_letters=None:
            ("Anrede,\n\nText.", "Mail.", "", _usage()),
    )
    assert (await drafting.draft_for_job(job_id))["ok"]
    db.upsert_draft(con, job_id, {"pdf_path": "/old/mappe.pdf"})
    con.commit()

    result = await drafting.draft_for_job(job_id)  # re-draft
    assert result["ok"]
    assert result["draft"]["pdf_path"] == ""


async def test_generating_claim_blocks_double_spend(
    con, ai_on, applicant, profile_file, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()
    db.upsert_draft(con, job_id, {"status": "generating"})
    con.commit()

    monkeypatch.setattr("jobdeck.ai.drafting.draft_application", _must_not_be_called)
    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and "already being generated" in result["error"]
    assert db.get_setting(con, "llm_calls", "0") == "0"


async def test_llm_not_configured_releases_claim_without_metering(
    con, ai_on, applicant, profile_file, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()

    def not_configured(job, profile_text, refnr="", applicant_name="", previous_letters=None):
        raise llm.LLMNotConfigured("ANTHROPIC_API_KEY is not set")

    monkeypatch.setattr("jobdeck.ai.drafting.draft_application", not_configured)
    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and "ANTHROPIC_API_KEY" in result["error"]
    assert db.get_draft_by_job(con, job_id)["status"] == "failed"
    assert db.get_setting(con, "llm_calls", "0") == "0"  # nothing was billed


async def test_recent_claim_still_blocks_just_under_the_timeout(
    con, ai_on, applicant, profile_file, monkeypatch
):
    import datetime

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()
    db.upsert_draft(con, job_id, {"status": "generating"})
    recent = (datetime.datetime.now()
              - datetime.timedelta(minutes=drafting.CLAIM_TIMEOUT_MIN - 1)
              ).isoformat(timespec="seconds")
    con.execute("UPDATE drafts SET updated_at=?", (recent,))
    con.commit()

    monkeypatch.setattr("jobdeck.ai.drafting.draft_application", _must_not_be_called)
    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and "already being generated" in result["error"]


@pytest.mark.parametrize("status,hint", [
    ("approved", "Freigabe im Postausgang zurücknehmen"),
    ("sending", "im Postausgang auflösen"),
    ("sent", "schon raus"),
    ("filed", "Bewerbung eingetragen"),
])
async def test_send_path_drafts_are_never_regenerated(
    con, ai_on, applicant, profile_file, monkeypatch, status, hint
):
    """A draft committed to the send path must survive a Draft click.

    Stealing a 'sending' claim would destroy the stuck-send evidence the
    review queue needs and open a double-send to the company."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()
    db.upsert_draft(con, job_id, {
        "status": status, "anschreiben_body": "Sehr geehrte Damen und Herren,",
        "pdf_path": "/tmp/mappe.pdf",
    })
    stale = "2020-01-01T00:00:00"  # older than CLAIM_TIMEOUT_MIN: age must not matter
    con.execute("UPDATE drafts SET updated_at=?", (stale,))
    con.commit()

    monkeypatch.setattr("jobdeck.ai.drafting.draft_application", _must_not_be_called)
    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and hint in result["error"]

    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == status  # untouched
    assert draft["pdf_path"] == "/tmp/mappe.pdf"  # not wiped
    assert draft["anschreiben_body"] == "Sehr geehrte Damen und Herren,"
    assert db.get_setting(con, "llm_calls", "0") == "0"


async def test_finish_discards_result_when_claim_was_taken_away(
    con, ai_on, applicant, profile_file, monkeypatch
):
    """A draft resolved/discarded mid-generation must not be overwritten."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()

    def steal_then_draft(job, profile_text, refnr="", applicant_name="", previous_letters=None):
        # simulates a human resolving the draft while the LLM call runs
        with db.db() as other:
            db.upsert_draft(other, job_id, {"status": "discarded"})
        return ("Anrede,\n\nText.", "Mail.", "", _usage())

    monkeypatch.setattr("jobdeck.ai.drafting.draft_application", steal_then_draft)
    result = await drafting.draft_for_job(job_id)
    assert not result["ok"] and "changed while" in result["error"]

    draft = db.get_draft_by_job(con, job_id)
    assert draft["status"] == "discarded"  # the newer state wins
    assert draft["email_body"] == ""
    assert db.get_setting(con, "llm_calls") == "1"  # the call was still billed


async def test_abandoned_claim_is_reclaimed(
    con, ai_on, applicant, profile_file, monkeypatch
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    job_id = _insert_job(con)
    con.commit()
    db.upsert_draft(con, job_id, {"status": "generating"})
    stale = "2020-01-01T00:00:00"  # far older than CLAIM_TIMEOUT_MIN
    con.execute("UPDATE drafts SET updated_at=?", (stale,))
    con.commit()

    monkeypatch.setattr(
        "jobdeck.ai.drafting.draft_application",
        lambda job, profile_text, refnr="", applicant_name="", previous_letters=None:
            ("Anrede,\n\nText.", "Mail.", "", _usage()),
    )
    result = await drafting.draft_for_job(job_id)
    assert result["ok"]
    assert db.get_draft_by_job(con, job_id)["status"] == "ready"


def test_the_drafting_budget_leaves_room_for_thinking():
    """Not a style preference — a measurement. On the posting that used to
    fail, the finished draft was ~1000 tokens and arrived after ~8300 tokens of
    thinking, for 9285 output tokens in total. The old 12000 was chosen to
    clear that with margin; the ceiling is the one escalation above it.

    Raising the cap is free: billing is on tokens actually produced, not on the
    cap, so the five drafts that already succeed at ~2000 cost exactly what
    they did before."""
    assert ai_drafting.DRAFT_MAX_TOKENS >= 10000
    assert ai_drafting.DRAFT_MAX_TOKENS_CEILING >= ai_drafting.DRAFT_MAX_TOKENS * 2


def test_a_truncation_is_typed_not_matched_on_its_message():
    """Callers must tell 'the cap bit' from 'the sample was bad' without
    string-matching an error message that is free to be reworded."""
    plain = llm.LLMError("something else")
    assert plain.truncated is False
    cut = llm.LLMError("response truncated at max_tokens=12000", truncated=True)
    assert cut.truncated is True


@pytest.mark.parametrize("body, expected, why", [
    ("Sehr geehrte Damen und Herren,\n\nText.\n\nMit freundlichen Grüßen\nMax Muster",
     "Sehr geehrte Damen und Herren,\n\nText.",
     "the real shape: the template adds the closing, so the PDF carried it twice"),
    ("Text.\n\nMit freundlichen Grüßen,\nMax Muster\n\n", "Text.",
     "a trailing comma and blank lines"),
    ("Text.\n\nBeste Grüße", "Text.", "a closing with no name under it"),
    ("Text.\n\nMit freundlichen Grüßen\n\nMax Muster", "Text.",
     "a blank line between closing and name"),
    ("Text.", "Text.", "nothing to strip"),
    ("Viele Grüße aus Aachen erreichten mich.\n\nDer Rest.",
     "Viele Grüße aus Aachen erreichten mich.\n\nDer Rest.",
     "a greeting INSIDE prose is not a sign-off and must survive"),
    ("Sehr geehrter Herr Muster,\n\nMax Muster hat mir von Ihnen erzählt.",
     "Sehr geehrter Herr Muster,\n\nMax Muster hat mir von Ihnen erzählt.",
     "the applicant's name inside a sentence is not a signature line"),
    ("", "", "an empty body"),
    (None, "", "no body at all"),
])
def test_a_closing_the_template_supplies_is_stripped_once(body, expected, why):
    assert ai_drafting.strip_letter_closing(body, "Max Muster") == expected, why


def test_the_closing_strip_survives_the_sharp_s():
    """str.casefold() expands ß to ss, so a literal containing ß can never match
    a casefolded input — the first version of this guard silently did nothing."""
    assert ai_drafting.strip_letter_closing(
        "Text.\n\nMIT FREUNDLICHEN GRÜSSEN\nMax Muster", "Max Muster") == "Text."
    assert ai_drafting.strip_letter_closing(
        "Text.\n\nmit freundlichen grüßen", "") == "Text."


def test_the_draft_path_removes_a_closing_the_model_wrote_anyway(monkeypatch):
    """The prompt forbids a closing formula in the Anschreiben because the letter
    TEMPLATE supplies one — job 41's real Mappe carried "Mit freundlichen Grüßen
    / Andrei Sili" twice on the page. The prompt asks; this proves the code
    enforces, and that the e-mail keeps the closing it is supposed to have."""
    def fake_complete(**kwargs):
        return llm.LLMResult(
            text=_draft_text(
                analysis="x",
                stellenbezeichnung="Backend Developer (m/w/d)",
                anschreiben_body=("Sehr geehrter Herr Pott,\n\nAbsatz.\n\n"
                                  "Mit freundlichen Grüßen\nAndrei Sili"),
                email_body="Guten Tag,\n\nMit freundlichen Grüßen\nAndrei Sili",
            ),
            model="m", input_tokens=1, output_tokens=1, cost_usd=0.0,
        )

    monkeypatch.setattr(llm, "complete", fake_complete)
    anschreiben, email_body, _, _ = ai_drafting.draft_application(
        _job(), "profil", applicant_name="Andrei Sili")
    assert anschreiben == "Sehr geehrter Herr Pott,\n\nAbsatz."
    assert "Grüßen" not in anschreiben
    # the E-MAIL closing is not the template's job and must stay
    assert email_body.endswith("Mit freundlichen Grüßen\nAndrei Sili")
