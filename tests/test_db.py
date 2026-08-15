import pytest

from jobdeck import db
from jobdeck.dedupe import duplicates_for_jobs


def _add_app(con, firma="Testfirma GmbH", email="jobs@testfirma.de", status="Gesendet"):
    return db.add_bewerbung(
        con,
        {"gesendet_am": "2026-07-01", "firma": firma, "email": email,
         "kanal": "E-Mail", "status": status},
    )


def _add_job(con, **over):
    values = {
        "source": "arbeitsagentur",
        "external_id": over.pop("external_id", "REF-1"),
        "title": "Python Entwickler (m/w/d)",
        "company": "Neue Firma GmbH",
        "location": "Aachen",
        "url": "https://example.org/job/1",
        "contact_email": "hr@neuefirma.de",
    }
    values.update(over)
    return db.insert_job_if_new(con, values)


def test_set_status_writes_history(con):
    app_id = _add_app(con)
    assert db.set_status(con, app_id, "Absage", source="user")
    history = db.list_status_history(con, app_id)
    # creation entry + change entry
    assert [h["new_status"] for h in history] == ["Absage", "Gesendet"]
    assert db.get_bewerbung(con, app_id)["status"] == "Absage"


def test_automatic_source_cannot_downgrade(con):
    app_id = _add_app(con, status="Einladung")
    # a late confirmation e-mail must not overwrite the invitation
    assert not db.set_status(con, app_id, "In Bearbeitung", source="reply_rule")
    assert db.get_bewerbung(con, app_id)["status"] == "Einladung"


def test_manual_change_can_downgrade(con):
    app_id = _add_app(con, status="Einladung")
    assert db.set_status(con, app_id, "In Bearbeitung", source="user")
    assert db.get_bewerbung(con, app_id)["status"] == "In Bearbeitung"


def test_insert_job_if_new_is_idempotent(con):
    assert _add_job(con) is not None
    assert _add_job(con) is None  # same (source, external_id)
    assert _add_job(con, external_id="REF-2") is not None


def test_insert_derives_published_on_and_keeps_the_raw_value(con):
    # the epoch string is what Arbeitnow sends; the derived column is what SQL
    # can order on, and the raw one stays readable for a re-derivation later
    job_id = _add_job(con, source="arbeitnow", published_at="1785897635")
    job = db.get_job(con, job_id)
    assert job["published_at"] == "1785897635"
    assert job["published_on"] == "2026-08-05"
    unknown = _add_job(con, external_id="REF-9", published_at="irgendwann")
    assert db.get_job(con, unknown)["published_on"] == ""


def test_apply_job_creates_application_and_links(con):
    job_id = _add_job(con)
    bewerbung_id = db.apply_job(con, job_id, kanal="Online-Portal")
    assert bewerbung_id is not None
    job = db.get_job(con, job_id)
    assert job["status"] == "applied" and job["bewerbung_id"] == bewerbung_id
    app = db.get_bewerbung(con, bewerbung_id)
    assert app["firma"] == "Neue Firma GmbH"
    assert app["email"] == "hr@neuefirma.de"
    assert app["kanal"] == "Online-Portal"


def test_apply_job_blocks_duplicates(con):
    _add_app(con, firma="Neue Firma GmbH", email="")
    job_id = _add_job(con)
    assert db.apply_job(con, job_id, kanal="E-Mail") is None
    job = db.get_job(con, job_id)
    assert job["status"] == "duplicate" and job["duplicate_of"] is not None


def test_settings_roundtrip(con):
    assert db.get_setting(con, "missing", "fallback") == "fallback"
    db.set_setting(con, "daily_send_cap", "15")
    db.set_setting(con, "daily_send_cap", "20")
    assert db.get_setting(con, "daily_send_cap") == "20"


def test_profiles_crud(con):
    pid = db.add_profile(
        con, {"name": "Python bundesweit", "keywords": "Python Entwickler"}
    )
    profiles = db.list_profiles(con)
    assert len(profiles) == 1 and profiles[0]["auto_send"] == 0
    db.update_profile(
        con, pid,
        {"name": "Python DE", "keywords": "Python", "active": 0},
    )
    assert db.list_profiles(con, active_only=True) == []
    db.delete_profile(con, pid)
    assert db.list_profiles(con) == []


def test_profile_match_criteria_roundtrip(con):
    pid = db.add_profile(
        con,
        {"name": "Backend DE", "keywords": "Python Backend",
         "hard_tags": "#backend\n#münchen", "soft_preferences": "Gehalt 45000 @80%",
         "strictness": 70},
    )
    row = db.get_profile(con, pid)
    assert row["hard_tags"] == "#backend\n#münchen"
    assert row["soft_preferences"] == "Gehalt 45000 @80%"
    assert row["strictness"] == 70

    db.update_profile(
        con, pid,
        {"name": "Backend DE", "keywords": "Python Backend", "hard_tags": "#remote"},
    )
    row = db.get_profile(con, pid)
    assert row["hard_tags"] == "#remote"
    assert row["strictness"] == 50  # unset fields fall back to defaults

    assert db.get_profile(con, 99999) is None


def test_list_jobs_hides_and_counts_mismatches(con):
    ok = _add_job(con, external_id="ok")
    mismatch = _add_job(con, external_id="mismatch")
    unscored = _add_job(con, external_id="unscored")
    db.set_job_score(con, ok, 70, "Passt.")
    db.set_job_score(con, mismatch, 0, "Verstößt gegen #backend.")

    visible = db.list_jobs(con, status="new", mismatches="exclude")
    assert [r["id"] for r in visible] == [ok, unscored]  # NULL score stays visible

    everything = db.list_jobs(con, status="new")
    assert {r["id"] for r in everything} == {ok, mismatch, unscored}

    # the hidden pile stays reachable even when better rows fill the limit
    assert [r["id"] for r in db.list_jobs(con, status="new", limit=1,
                                          mismatches="only")] == [mismatch]

    assert db.count_mismatches(con, status="new") == 1
    assert db.count_mismatches(con, status="applied") == 0
    assert db.count_mismatches(con) == 1

    # the all-statuses view filters too (id DESC, exact rows)
    assert [r["id"] for r in db.list_jobs(con, mismatches="exclude")] \
        == [unscored, ok]


def test_list_jobs_sorts_by_score_then_newest(con):
    """'Score sorts' is the core product rule — pin direction and tiebreak."""
    low = _add_job(con, external_id="low")
    high_old = _add_job(con, external_id="high-old")
    high_new = _add_job(con, external_id="high-new")
    unscored = _add_job(con, external_id="unscored")
    db.set_job_score(con, low, 30, "Teilweise.")
    db.set_job_score(con, high_old, 90, "Sehr gut.")
    db.set_job_score(con, high_new, 90, "Sehr gut.")

    rows = db.list_jobs(con, status="new")
    # best score first, newer id wins the tie, unscored (NULL) last
    assert [r["id"] for r in rows] == [high_new, high_old, low, unscored]


def test_set_job_contacts_fills_only_empty_columns(con):
    job_id = _add_job(con)  # source already provides contact_email
    db.set_job_contacts(con, job_id, {
        "ansprechpartner": " Frau Muster ",
        "contact_email": "extracted@other.de",   # must NOT clobber source data
        "contact_phone": "+49 241 123456",
        "refnr": "REF-2026-42",
        "bogus_column": "ignored",
        "contact_strasse": "   ",                # whitespace-only → skipped
    })
    job = db.get_job(con, job_id)
    assert job["ansprechpartner"] == "Frau Muster"
    assert job["contact_email"] == "hr@neuefirma.de"  # source data wins
    assert job["contact_phone"] == "+49 241 123456"
    assert job["refnr"] == "REF-2026-42"
    assert job["contact_strasse"] == ""
    assert job["contact_source"] == "posting"

    # a second extraction never overwrites what is already there
    db.set_job_contacts(con, job_id, {"ansprechpartner": "Herr Anders"})
    assert db.get_job(con, job_id)["ansprechpartner"] == "Frau Muster"

    # nothing to fill → no contact_source stamp
    empty_job = _add_job(con, external_id="REF-EMPTY")
    db.set_job_contacts(con, empty_job, {"ansprechpartner": ""})
    assert db.get_job(con, empty_job)["contact_source"] == ""

    # an existing source stamp (e.g. future web enrichment) is never rewritten
    preset = _add_job(con, external_id="REF-PRESET")
    con.execute("UPDATE jobs SET contact_source='web' WHERE id=?", (preset,))
    db.set_job_contacts(con, preset, {"ansprechpartner": "Frau Neu"})
    assert db.get_job(con, preset)["contact_source"] == "web"


def test_upsert_draft_keeps_one_row_per_job(con):
    job_id = _add_job(con)
    draft_id = db.upsert_draft(con, job_id, {
        "status": "ready", "recipient": "hr@neuefirma.de",
        "betreff": "Bewerbung als Python Entwickler – Max Muster",
        "email_body": "Sehr geehrte Damen und Herren, ...",
        "anschreiben_body": "Absatz 1\n\nAbsatz 2", "llm_model": "claude-haiku-4-5",
    })
    row = db.get_draft(con, draft_id)
    assert row["status"] == "ready"
    assert row["betreff"].startswith("Bewerbung als")

    # re-draft updates the same row instead of stacking a second one
    again = db.upsert_draft(con, job_id, {"status": "ready", "betreff": "Neu"})
    assert again == draft_id
    assert con.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
    assert db.get_draft_by_job(con, job_id)["betreff"] == "Neu"

    assert db.get_draft_by_job(con, 99999) is None


def test_reset_job_scores_makes_them_re_scorable(con):
    """When the criteria change, the old verdict answered a different
    question — clearing it is the one sanctioned way to re-ask."""
    a = _add_job(con, external_id="a")
    b = _add_job(con, external_id="b")
    c = _add_job(con, external_id="c")
    for job_id in (a, b, c):
        db.set_job_score(con, job_id, 85, "Perfekter Match")

    assert db.list_unscored_jobs(con) == []
    assert db.reset_job_scores(con, [a, b]) == 2

    unscored = [r["id"] for r in db.list_unscored_jobs(con)]
    assert unscored == [a, b]  # oldest first, and c keeps its verdict
    assert db.get_job(con, a)["match_score"] is None
    assert db.get_job(con, a)["match_reason"] == ""
    assert db.get_job(con, c)["match_score"] == 85


def test_reset_job_scores_spares_postings_already_acted_on(con):
    """A posting that was applied to or skipped keeps its history — only
    the inbox's still-open rows may be re-asked."""
    applied = _add_job(con, external_id="applied")
    skipped = _add_job(con, external_id="skipped")
    for job_id in (applied, skipped):
        db.set_job_score(con, job_id, 85, "Match")
    db.set_job_status(con, applied, "applied")
    db.set_job_status(con, skipped, "skipped")

    assert db.reset_job_scores(con, [applied, skipped]) == 0
    assert db.get_job(con, applied)["match_score"] == 85
    assert db.get_job(con, skipped)["match_score"] == 85


def test_reset_job_scores_with_no_ids_is_a_noop(con):
    assert db.reset_job_scores(con, []) == 0


def test_delete_bewerbung_clears_references(con):
    job_id = _add_job(con)
    bewerbung_id = db.apply_job(con, job_id, kanal="E-Mail")
    db.delete_bewerbung(con, bewerbung_id)
    assert db.get_bewerbung(con, bewerbung_id) is None
    assert db.get_job(con, job_id)["bewerbung_id"] is None


def test_delete_bewerbung_clears_the_link_a_send_wrote(con):
    """record_send is the first writer of drafts.bewerbung_id — without
    clearing it, deleting any sent application hits the FK constraint."""
    job_id = _add_job(con)
    draft_id = db.upsert_draft(con, job_id, {"status": "ready"})
    bewerbung_id = db.apply_job(con, job_id, kanal="E-Mail")
    db.record_send(con, draft_id, "m-1", "t-1", bewerbung_id)
    assert db.get_draft(con, draft_id)["bewerbung_id"] == bewerbung_id

    db.delete_bewerbung(con, bewerbung_id)
    assert db.get_bewerbung(con, bewerbung_id) is None
    assert db.get_draft(con, draft_id)["bewerbung_id"] is None
    assert db.get_draft(con, draft_id)["status"] == "sent"  # history survives


def _gone_job(con, ext, liveness, score=80):
    job_id = db.insert_job_if_new(con, {
        "source": "arbeitnow", "external_id": ext, "title": "Dev",
        "company": "Firma", "url": f"https://www.arbeitnow.com/jobs/x/{ext}",
    })
    con.execute("UPDATE jobs SET match_score=?, liveness=? WHERE id=?",
                (score, liveness, job_id))
    return job_id


def test_list_jobs_filters_the_two_piles_independently(con):
    live = _gone_job(con, "live", "alive")
    dead = _gone_job(con, "dead", "gone")
    both = _gone_job(con, "both", "gone", score=0)
    mismatch = _gone_job(con, "mismatch", "", score=0)

    def ids(**kw):
        return sorted(r["id"] for r in db.list_jobs(con, status="new", **kw))

    assert ids() == sorted([live, dead, both, mismatch])   # both default to include
    assert ids(mismatches="exclude", gone="exclude") == [live]
    assert ids(gone="only") == sorted([dead, both])
    assert ids(mismatches="only") == sorted([both, mismatch])
    # a row in both piles is reachable from either view, and hidden by default
    assert both in ids(gone="only") and both in ids(mismatches="only")


def test_an_unknown_filter_value_raises_instead_of_showing_a_hidden_pile(con):
    import pytest
    _gone_job(con, "hidden", "gone")
    for bad in ({"mismatches": "excluded"}, {"gone": "yes"}, {"gone": ""}):
        with pytest.raises(ValueError):
            db.list_jobs(con, status="new", **bad)
        with pytest.raises(ValueError):
            db.count_jobs(con, status="new", **bad)
        with pytest.raises(ValueError):
            db.count_job_groups(con, status="new", **bad)


# ---------------------------------------------------------------------------
# "A stored e-mail settles the channel" — the rule that only the resolver knew
# ---------------------------------------------------------------------------
def test_a_posting_that_arrives_with_an_email_applies_by_email(con):
    """Nothing outside the resolver applied `classify`'s first rule, so 81 of
    his Arbeitsagentur postings held an application address and still read as
    unresolved form jobs — in an app whose whole pain is form jobs."""
    job_id = _add_job(con, contact_email="bewerbung@firma.de")
    assert db.get_job(con, job_id)["apply_channel"] == "direct_email"


def test_a_posting_without_an_email_is_left_for_the_resolver(con):
    job_id = _add_job(con, contact_email="")
    assert db.get_job(con, job_id)["apply_channel"] == ""


def test_an_adopted_email_converts_a_form_job(con):
    """The web lookup's whole purpose: the posting stops being a form job the
    moment he confirms an address."""
    job_id = _add_job(con, contact_email="")
    db.set_apply_channel(con, job_id, "company_site", "", "https://firma.de/x")

    db.set_contact_email(con, job_id, "bewerbung@firma.de", "web_lookup")

    row = db.get_job(con, job_id)
    assert row["apply_channel"] == "direct_email"
    # the resolved URL stays: it is still true, and the row still offers to
    # open the posting with it
    assert row["apply_url"] == "https://firma.de/x"


def test_the_stock_is_converted_once_the_rule_exists(con):
    """A posting whose e-mail was harvested before this rule existed heals on
    the next start, from data the row already holds."""
    job_id = _add_job(con, contact_email="bewerbung@firma.de")
    con.execute("UPDATE jobs SET apply_channel='' WHERE id=?", (job_id,))

    assert db.resolve_email_channels(con) == 1
    assert db.get_job(con, job_id)["apply_channel"] == "direct_email"
    assert db.resolve_email_channels(con) == 0, "not idempotent"


def test_a_finished_posting_keeps_the_channel_it_was_applied_through(con):
    """Rewriting how one WOULD have applied to a posting already sent, skipped
    or filed as a duplicate changes nothing and only rewrites history."""
    job_id = _add_job(con, contact_email="bewerbung@firma.de")
    db.set_apply_channel(con, job_id, "ats_form", "JOIN", "https://join.com/x")
    db.set_job_status(con, job_id, "applied")

    assert db.resolve_email_channels(con) == 0
    assert db.get_job(con, job_id)["apply_channel"] == "ats_form"


# ---------------------------------------------------------------------------
# Facts a source states about a posting
# ---------------------------------------------------------------------------
def test_set_job_facts_stores_what_the_source_stated(con):
    job_id = _add_job(con)

    written = db.set_job_facts(con, job_id, {
        "work_strasse": "Musterstraße 26", "work_plz_ort": "54321 Beispielstadt",
        "salary_from": "37000", "salary_to": "47000",
        "salary_period": "Jahresgehalt", "temp_agency": 1})

    row = db.get_job(con, job_id)
    assert written == 6
    assert row["work_strasse"] == "Musterstraße 26"
    assert row["work_plz_ort"] == "54321 Beispielstadt"
    assert (row["salary_from"], row["salary_to"]) == ("37000", "47000")
    assert row["salary_period"] == "Jahresgehalt"
    assert row["temp_agency"] == 1


def test_a_silent_payload_never_erases_what_an_earlier_one_said(con):
    """The same columns are filled by discovery and by the daily liveness
    probe; a payload that omits a field must not delete it."""
    job_id = _add_job(con)
    db.set_job_facts(con, job_id, {"work_plz_ort": "12345 Musterstadt"})

    db.set_job_facts(con, job_id, {"work_plz_ort": "", "salary_from": "40000"})

    row = db.get_job(con, job_id)
    assert row["work_plz_ort"] == "12345 Musterstadt"
    assert row["salary_from"] == "40000"


def test_a_fact_nobody_stores_is_refused_rather_than_dropped(con):
    job_id = _add_job(con)
    with pytest.raises(ValueError, match="homeofficetyp"):
        db.set_job_facts(con, job_id, {"homeofficetyp": "teilweise"})


def test_a_corrected_temp_agency_flag_can_go_back_to_zero(con):
    job_id = _add_job(con)
    db.set_job_facts(con, job_id, {"temp_agency": 1})

    db.set_job_facts(con, job_id, {"temp_agency": 0})

    assert db.get_job(con, job_id)["temp_agency"] == 0


def test_bootstrap_reclassifies_the_stock_it_finds(con, data_dir):
    """The startup self-heal is what turned 82 of his postings from apparent
    form jobs into e-mail applications; without a test the call itself could be
    dropped with the suite green."""
    job_id = _add_job(con, contact_email="bewerbung@firma.de")
    con.execute("UPDATE jobs SET apply_channel='' WHERE id=?", (job_id,))
    con.commit()

    db.bootstrap()

    assert db.get_job(con, job_id)["apply_channel"] == "direct_email"


def test_a_posting_he_opened_as_a_form_still_converts(con):
    """A posting with its form open is precisely the row an address arriving
    later should rescue — it is the one he is still working on."""
    job_id = _add_job(con, contact_email="bewerbung@firma.de")
    con.execute("UPDATE jobs SET apply_channel='' WHERE id=?", (job_id,))
    db.mark_form_opened(con, job_id)

    assert db.resolve_email_channels(con) == 1
    assert db.get_job(con, job_id)["apply_channel"] == "direct_email"


def test_resolving_a_channel_alone_changes_the_data_signature(con):
    """The resolve-channels batch writes nothing but apply_channel, so that
    term is the only thing that can tell an open page the batch happened."""
    job_id = _add_job(con, contact_email="")
    con.commit()
    before = db.data_signature(con)

    db.set_apply_channel(con, job_id, "ats_form", "JOIN", "https://join.com/x")
    con.commit()

    assert db.data_signature(con) != before


def test_the_hidden_old_count_answers_for_one_view_at_a_time(con):
    """The count sits next to the list and must describe the same view: an old
    posting he already skipped is not hidden FROM the 'new' inbox."""
    import datetime
    old = (datetime.date.today() - datetime.timedelta(days=90)).isoformat()
    fresh_id = _add_job(con, external_id="a")
    skipped_id = _add_job(con, external_id="b")
    for job_id in (fresh_id, skipped_id):
        con.execute("UPDATE jobs SET published_on=? WHERE id=?", (old, job_id))
    con.execute("UPDATE jobs SET status='skipped' WHERE id=?", (skipped_id,))
    con.commit()

    assert db.count_old_jobs(con, "new", 45) == 1
    assert db.count_old_jobs(con, None, 45) == 2


def test_the_cockpit_signature_follows_the_posting_not_only_its_draft(con):
    """The cockpit watches one posting because the AD can die and the
    application can be recorded elsewhere while he is at the form."""
    job_id = _add_job(con)
    con.commit()
    seen = db.job_signature(con, job_id)

    for write in (
        lambda: db.set_job_liveness(con, job_id, "gone"),
        lambda: db.set_job_status(con, job_id, "portal"),
        lambda: db.set_contact_email(con, job_id, "neu@firma.de", "web_lookup"),
        lambda: db.set_apply_channel(con, job_id, "ats_form", "JOIN", "https://x"),
    ):
        write()
        con.commit()
        current = db.job_signature(con, job_id)
        assert current != seen, "the cockpit would not notice this"
        seen = current


def test_the_cockpit_signature_of_a_vanished_posting_is_nothing(con):
    assert db.job_signature(con, 999999) is None


def test_the_posting_signature_covers_the_contact_block_it_is_asked_about(con):
    """Both the cockpit and the letter preview PRINT the Ansprechpartner and
    the postal address, and contact resolution fills them in the background.
    A signature blind to them leaves a screen reading "none named" beside a
    name the app already holds."""
    job_id = _add_job(con)
    con.commit()
    seen = db.job_signature(con, job_id)

    for write in (
        lambda: db.set_job_contacts(con, job_id, {"ansprechpartner": "Frau Weber"}),
        lambda: db.set_job_contacts(con, job_id, {"contact_strasse": "Weg 1"}),
        lambda: db.set_job_contacts(con, job_id, {"contact_plz_ort": "10115 Berlin"}),
        lambda: db.set_job_contacts(con, job_id, {"refnr": "K-17"}),
        lambda: con.execute("UPDATE jobs SET work_plz_ort='10115 Berlin' "
                            "WHERE id=?", (job_id,)),
        lambda: con.execute("UPDATE jobs SET temp_agency=1 WHERE id=?", (job_id,)),
        lambda: con.execute("UPDATE jobs SET title='Anderer Titel' WHERE id=?",
                            (job_id,)),
    ):
        write()
        con.commit()
        current = db.job_signature(con, job_id)
        assert current != seen, "a screen stating this would not notice"
        seen = current


def test_two_unresolved_postings_never_sign_the_same(con):
    """A caller may CHOOSE which posting to sign — the letter preview signs
    whichever currently tops the working list. Two fresh postings with the
    same scraped title have every other column empty and identical, so without
    the id and the company the preview goes on naming a firm whose posting has
    just been skipped, applied to or outranked."""
    alpha = _add_job(con, external_id="A", company="Alpha GmbH",
                     title="Softwareentwickler (m/w/d)")
    beta = _add_job(con, external_id="B", company="Beta AG",
                    title="Softwareentwickler (m/w/d)")
    con.commit()

    assert db.job_signature(con, alpha) != db.job_signature(con, beta)


# ---------------------------------------------------------------------------
# Vorgemerkt — a posting he sets aside by hand (schema v8)
# ---------------------------------------------------------------------------
def test_a_posting_can_be_set_aside_and_released_again(con):
    job_id = _add_job(con)
    assert db.set_bookmark(con, job_id, True) is True
    assert con.execute(
        "SELECT bookmarked_at FROM jobs WHERE id=?", (job_id,)
    ).fetchone()[0] != ""

    assert db.set_bookmark(con, job_id, False) is False
    assert con.execute(
        "SELECT bookmarked_at FROM jobs WHERE id=?", (job_id,)
    ).fetchone()[0] == ""


def test_marking_an_already_marked_posting_does_not_move_it(con, monkeypatch):
    """The pile is ordered by when he set each posting aside, so a second press
    on a row already in it must not jump that row to the front."""
    job_id = _add_job(con)
    monkeypatch.setattr(db, "_now", lambda: "2026-08-01T09:00:00")
    db.set_bookmark(con, job_id, True)
    monkeypatch.setattr(db, "_now", lambda: "2026-08-12T18:00:00")
    db.set_bookmark(con, job_id, True)

    assert con.execute(
        "SELECT bookmarked_at FROM jobs WHERE id=?", (job_id,)
    ).fetchone()[0] == "2026-08-01T09:00:00"


def test_the_mark_is_independent_of_what_he_did_with_the_posting(con):
    """Setting one aside is not acting on it: the mark outlives a status
    change, so the pile keeps a posting he later applied to or skipped."""
    job_id = _add_job(con)
    db.set_bookmark(con, job_id, True)
    db.set_job_status(con, job_id, "skipped")
    con.commit()

    assert db.count_bookmarked_jobs(con) == 1
    rows = db.list_jobs(con, "skipped", bookmarked="only")
    assert [r["id"] for r in rows] == [job_id]


def test_the_bookmark_filter_splits_the_list_in_two(con):
    marked = _add_job(con, external_id="A")
    plain = _add_job(con, external_id="B", company="Andere GmbH")
    db.set_bookmark(con, marked, True)
    con.commit()

    assert [r["id"] for r in db.list_jobs(con, "new", bookmarked="only")] == [marked]
    assert [r["id"] for r in db.list_jobs(con, "new", bookmarked="exclude")] == [plain]
    assert {r["id"] for r in db.list_jobs(con, "new")} == {marked, plain}


def test_the_bookmark_filter_reaches_the_grouped_view_too(con):
    """Grouping by company runs its own query; a filter that only reached the
    flat list would show a pile the grouped page said was empty."""
    marked = _add_job(con, external_id="A", company="Eine GmbH")
    _add_job(con, external_id="B", company="Andere GmbH")
    db.set_bookmark(con, marked, True)
    con.commit()

    assert db.count_job_groups(con, "new", bookmarked="only") == 1
    groups = db.list_job_groups(con, "new", bookmarked="only")
    assert [r["id"] for r in groups] == [marked]


def test_an_unknown_bookmark_filter_value_raises(con):
    with pytest.raises(ValueError, match="bookmarked"):
        db.list_jobs(con, "new", bookmarked="maybe")


def test_counting_what_he_set_aside_ignores_status(con):
    first = _add_job(con, external_id="A")
    second = _add_job(con, external_id="B", company="Andere GmbH")
    _add_job(con, external_id="C", company="Dritte GmbH")
    db.set_bookmark(con, first, True)
    db.set_bookmark(con, second, True)
    db.set_job_status(con, second, "applied")
    con.commit()

    assert db.count_bookmarked_jobs(con) == 2


# ---------------------------------------------------------------------------
# Neu — the half of the list he has not looked at yet (schema v8)
# ---------------------------------------------------------------------------
def test_opening_a_posting_records_that_he_read_it(con):
    job_id = _add_job(con)
    assert con.execute(
        "SELECT opened_at FROM jobs WHERE id=?", (job_id,)
    ).fetchone()[0] == ""

    db.mark_job_opened(con, job_id)
    assert con.execute(
        "SELECT opened_at FROM jobs WHERE id=?", (job_id,)
    ).fetchone()[0] != ""


def test_reading_a_posting_again_does_not_restamp_it(con, monkeypatch):
    """The list asks "have I looked at this", not "when last" — re-stamping
    would make the order move under him as he reads down it."""
    job_id = _add_job(con)
    monkeypatch.setattr(db, "_now", lambda: "2026-08-01T09:00:00")
    db.mark_job_opened(con, job_id)
    monkeypatch.setattr(db, "_now", lambda: "2026-08-12T18:00:00")
    db.mark_job_opened(con, job_id)

    assert con.execute(
        "SELECT opened_at FROM jobs WHERE id=?", (job_id,)
    ).fetchone()[0] == "2026-08-01T09:00:00"


def test_the_unread_filter_splits_the_list_in_two(con):
    read = _add_job(con, external_id="A")
    unread = _add_job(con, external_id="B", company="Andere GmbH")
    db.mark_job_opened(con, read)
    con.commit()

    assert [r["id"] for r in db.list_jobs(con, "new", opened="only")] == [read]
    assert [r["id"] for r in db.list_jobs(con, "new", opened="exclude")] == [unread]
    assert db.count_jobs(con, "new", opened="exclude") == 1


def test_the_unread_filter_reaches_the_grouped_view_too(con):
    read = _add_job(con, external_id="A", company="Eine GmbH")
    unread = _add_job(con, external_id="B", company="Andere GmbH")
    db.mark_job_opened(con, read)
    con.commit()

    assert db.count_job_groups(con, "new", opened="exclude") == 1
    assert [r["id"] for r in
            db.list_job_groups(con, "new", opened="exclude")] == [unread]


def test_an_unknown_opened_filter_value_raises(con):
    with pytest.raises(ValueError, match="opened"):
        db.list_jobs(con, "new", opened="perhaps")


# ---------------------------------------------------------------------------
# In Arbeit — a posting whose application is under way
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", db.OPEN_DRAFT_STATUSES)
def test_a_draft_still_going_puts_its_posting_in_arbeit(con, status):
    job_id = _add_job(con)
    db.upsert_draft(con, job_id, {"status": status})
    con.commit()

    assert [r["id"] for r in db.list_jobs(con, None, in_progress="only")] == [job_id]
    assert db.count_jobs(con, None, in_progress="exclude") == 0


@pytest.mark.parametrize("status", ["discarded", "sent"])
def test_a_finished_or_discarded_draft_is_no_longer_in_arbeit(con, status):
    """Thrown away or already gone: either way there is nothing left to do
    about it here, and a view that kept promising work would never empty."""
    job_id = _add_job(con)
    db.upsert_draft(con, job_id, {"status": status})
    con.commit()

    assert db.list_jobs(con, None, in_progress="only") == []
    assert [r["id"] for r in db.list_jobs(con, None, in_progress="exclude")] == [job_id]


def test_a_posting_with_no_draft_at_all_is_not_in_arbeit(con):
    job_id = _add_job(con)
    con.commit()
    assert db.list_jobs(con, None, in_progress="only") == []
    assert [r["id"] for r in db.list_jobs(con, None, in_progress="exclude")] == [job_id]


def test_the_newest_draft_is_not_the_one_that_decides(con):
    """`job_id` has no UNIQUE constraint, so a posting can carry a discarded
    draft AND a live one — it is in Arbeit if ANY draft is still going."""
    job_id = _add_job(con)
    for status in ("ready", "discarded"):
        con.execute(
            "INSERT INTO drafts (job_id, status, created_at, updated_at) "
            "VALUES (?, ?, '2026-08-12T10:00:00', '2026-08-12T10:00:00')",
            (job_id, status),
        )
    con.commit()

    assert [r["id"] for r in db.list_jobs(con, None, in_progress="only")] == [job_id]


def test_in_arbeit_reaches_the_grouped_view_too(con):
    working = _add_job(con, external_id="A", company="Eine GmbH")
    _add_job(con, external_id="B", company="Andere GmbH")
    db.upsert_draft(con, working, {"status": "ready"})
    con.commit()

    assert db.count_job_groups(con, None, in_progress="only") == 1
    assert [r["id"] for r in db.list_job_groups(con, None, in_progress="only")] == [working]


def test_an_unknown_drafting_filter_value_raises(con):
    with pytest.raises(ValueError, match="in_progress"):
        db.list_jobs(con, "new", in_progress="sometimes")


def test_a_posting_he_just_read_can_be_held_in_the_unread_view(con):
    """Reading a posting in an unread-only view would otherwise drop it out of
    the list under his cursor on the next refresh, taking the reading pane with
    it. The rows named here stay put; everything else obeys the filter."""
    read = _add_job(con, external_id="A")
    other_read = _add_job(con, external_id="B", company="Andere GmbH")
    unread = _add_job(con, external_id="C", company="Dritte GmbH")
    db.mark_job_opened(con, read)
    db.mark_job_opened(con, other_read)
    con.commit()

    assert [r["id"] for r in db.list_jobs(con, "new", opened="exclude")] == [unread]
    held = db.list_jobs(con, "new", opened="exclude", keep_ids=(read,))
    assert {r["id"] for r in held} == {read, unread}, \
        "the one he is reading stays; the one he read earlier does not"
    assert db.count_jobs(con, "new", opened="exclude", keep_ids=(read,)) == 2


def test_holding_a_row_does_not_smuggle_it_past_the_other_filters(con):
    """It is an exception to ONE arm. A posting he read is still hidden if it
    also violates a hard requirement — that is a fact about the posting."""
    read_mismatch = _add_job(con, external_id="A")
    db.set_job_score(con, read_mismatch, 0, "harte Anforderung verletzt")
    db.mark_job_opened(con, read_mismatch)
    con.commit()

    assert db.list_jobs(con, "new", opened="exclude", mismatches="exclude",
                        keep_ids=(read_mismatch,)) == []

def test_a_posting_is_not_warned_about_its_own_application(con):
    """Every row of the "Beworben" view carried a red "you already applied
    here" about the application that row itself produced, which reads as a
    duplicate-send error rather than as the record he opened."""
    job_id = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "e1", "title": "Dev",
        "company": "Eine GmbH"})
    bewerbung_id = db.apply_job(con, job_id, kanal="E-Mail")
    con.commit()

    rows = [dict(r) for r in db.list_jobs(con, "applied")]
    assert rows and rows[0]["bewerbung_id"] == bewerbung_id
    assert duplicates_for_jobs(con, rows) == {}


def test_another_posting_at_that_company_is_still_warned(con):
    """The gate itself is untouched: a SECOND posting at the same company can
    never become an application and still says so."""
    first = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "e1", "title": "Dev",
        "company": "Eine GmbH"})
    db.apply_job(con, first, kanal="E-Mail")
    second = db.insert_job_if_new(con, {
        "source": "stub", "external_id": "e2", "title": "Andere Rolle",
        "company": "Eine GmbH"})
    con.commit()

    rows = [dict(r) for r in db.list_jobs(con, "new")]
    assert list(duplicates_for_jobs(con, rows)) == [second]
