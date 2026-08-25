"""The numbers on the Bewerbungen screen, after the funnel came off it.

His verdict on the old one was that the statistic was unintelligible and
should be simple. The funnel counted postings — found, scored, opened,
written to — on a screen about applications, and every one of those figures
is now stated on Stellen, on the rows themselves. What replaced it is two groups that each add
up, and one sentence about how long an answer took.

The properties here are arithmetic and honesty: the parts must agree with the
whole they hang under, and no sentence may claim a promptness the clock behind
it cannot support.
"""

import datetime

import pytest

from jobdeck import db
from jobdeck.constants import DECISION_CLASSIFICATIONS
from jobdeck.services import register


def _apps(**counts) -> list[dict]:
    """A register holding exactly the given statuses."""
    rows = []
    for status, many in counts.items():
        label = status.replace("_", " ")
        rows += [{"id": len(rows) + n, "status": label, "firma": f"F{n}",
                  "kanal": "E-Mail", "gesendet_am": "2026-08-01"}
                 for n in range(many)]
    return rows


# ---------------------------------------------------------------------------
# The two groups have to agree
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("counts", [
    {"Gesendet": 61, "Absage": 35, "In_Bearbeitung": 24, "Keine_Antwort": 19,
     "Antwort_erhalten": 2},                       # his register, 2026-08-25
    {"Absage": 1},
    {"Gesendet": 3},                               # nothing answered at all
    {"Einladung": 2, "Absage": 1, "Antwort_erhalten": 4},
])
def test_the_answers_always_add_up_to_the_line_they_hang_under(counts):
    """The one property this panel exists for. Two groups are drawn one above
    the other and read against each other; if the three answers ever summed to
    anything but "beantwortet", the screen would be doing exactly what he
    rejected the old one for."""
    apps = _apps(**counts)
    view = {"apps": apps, "applied": 0}
    answered = {step.key: step for step in register.ledger(view)}["beantwortet"]
    assert sum(step.count for step in register.answers(apps)) == answered.count


@pytest.mark.parametrize("counts", [
    {"Zurückgezogen": 2, "Gesendet": 1},
    {"Zurückgezogen": 1},
    {"": 3, "Absage": 1},                          # add_bewerbung's own default
    {"Gesendet": 61, "Absage": 35, "In_Bearbeitung": 24, "Keine_Antwort": 19,
     "Antwort_erhalten": 2},
])
def test_the_register_always_splits_into_parts_that_sum_to_it(counts):
    """"Zurückgezogen" is in STATUS_OPTIONS, in none of the three sets the
    ledger knew, and offered by the edit dialog on this very screen — so two
    presses left the card printing "im Register 4" over parts summing to 2.
    A blank status does the same, and `db.add_bewerbung` writes one whenever
    the caller omits it.

    The remainder is computed, not enumerated: a fifth status added to the
    vocabulary tomorrow cannot break this."""
    apps = _apps(**counts)
    steps = {s.key: s for s in register.ledger({"apps": apps, "applied": 0})}
    parts = sum(s.count for k, s in steps.items() if k != "register")
    assert parts == steps["register"].count == len(apps)


def test_no_invitation_yet_is_printed_rather_than_hidden():
    """Unlike the waiting-for-a-score line on Stellen, a zero belongs here.
    The difference is what the number is about: a background worker's empty
    queue is nothing to report, and no invitations yet is the score."""
    steps = {s.key: s for s in register.answers(_apps(Absage=3))}
    assert steps["einladung"].count == 0
    assert steps["einladung"].label == "Einladungen"


def test_each_kind_of_answer_is_counted_as_itself():
    """The add-up differential holds under any COORDINATED pair of edits, so
    it was the only property asserted and both figures could be stuck: hard-
    code the invitation arm to nought and widen "sonstige" to everything but
    a rejection, and the sum still equals "beantwortet" while the one number
    he is working toward can never become non-zero."""
    steps = {s.key: s for s in register.answers(
        _apps(Einladung=2, Absage=7, Antwort_erhalten=3, Gesendet=5))}
    assert steps["einladung"].count == 2
    assert steps["absage"].count == 7
    assert steps["sonstige"].count == 3


def test_an_invitation_that_was_a_classifier_error_is_not_counted():
    """Measured on the real register: exactly one application ever reached
    "Einladung", and it was written automatically after the classifier read a
    quoted line out of his OWN application. It was corrected to "Antwort
    erhalten". Counting invitations ever REACHED would have resurrected it —
    a figure whose only non-zero value is a fixed bug."""
    corrected = _apps(Antwort_erhalten=1)
    steps = {s.key: s for s in register.answers(corrected)}
    assert steps["einladung"].count == 0
    assert steps["sonstige"].count == 1


def test_every_bar_on_the_card_is_measured_against_the_same_whole():
    """Both groups draw through one row helper into sibling grids whose bar
    tracks line up in a single visual column. Measured against their own
    group, "Absagen 35" drew at 95 % of the width two rows under
    "beantwortet 37" at 26 % — a smaller figure with a bar three and a half
    times longer than the whole containing it.

    Also the only test that reads a share at all: the two that did were
    deleted with the funnel, after which `_share` could be replaced by
    `return 0.0` and every bar on this card would render as a stub with the
    suite green."""
    apps = _apps(Gesendet=61, Absage=35, In_Bearbeitung=24, Keine_Antwort=19,
                 Antwort_erhalten=2)
    view = {"apps": apps, "applied": 0}
    register_steps = {s.key: s for s in register.ledger(view)}
    answer_steps = {s.key: s for s in register.answers(apps)}

    assert register_steps["register"].share == 1.0
    assert round(register_steps["beantwortet"].share, 4) == round(37 / 141, 4)
    assert round(answer_steps["absage"].share, 4) == round(35 / 141, 4)
    # The property, stated as the reader experiences it: a part never draws
    # longer than the figure it is a part of.
    assert answer_steps["absage"].share < register_steps["beantwortet"].share
    assert sum(s.share for s in answer_steps.values()) == \
        pytest.approx(register_steps["beantwortet"].share)


def test_an_open_application_is_in_no_answer_group():
    """"Gesendet" is not an answer, and a breakdown that swept it into
    "sonstige" would report replies nobody sent."""
    assert sum(s.count for s in register.answers(
        _apps(Gesendet=5, In_Bearbeitung=2, Keine_Antwort=3))) == 0


# ---------------------------------------------------------------------------
# How long an answer took
# ---------------------------------------------------------------------------
def test_a_span_that_cannot_be_read_is_dropped_not_guessed():
    """An imported row carries no date at all, and a reply threaded onto the
    wrong application can predate it. Folding either into the middle of the
    distribution would quietly pull the answer down."""
    assert register.answer_days([
        ("2026-08-01", "2026-08-05"),
        ("", "2026-08-05"),                 # no send date
        ("2026-08-01", ""),                 # no answer date
        ("nicht ein Datum", "2026-08-05"),
        ("2026-08-10", "2026-08-01"),       # answered before it was sent
    ]) == [4]


def test_the_days_come_back_sorted_so_the_middle_is_the_middle():
    assert register.answer_days([
        ("2026-08-01", "2026-08-09"), ("2026-08-01", "2026-08-02"),
        ("2026-08-01", "2026-08-05"),
    ]) == [1, 4, 8]


def test_too_few_answers_state_nothing_at_all():
    """A median over three replies is a coincidence, not a finding — and a
    screen whose whole value is that its numbers are honest may not print a
    figure it cannot stand behind."""
    few = [("2026-08-01", "2026-08-05")] * (register.ENOUGH_FOR_A_TIME - 1)
    assert register.answer_time(few, len(few)) == ("", "")
    enough = [("2026-08-01", "2026-08-05")] * register.ENOUGH_FOR_A_TIME
    assert register.answer_time(enough, len(enough))[0] != ""


def test_the_sentence_says_the_middle_and_the_slowest():
    """The median and the worst case, not the average: one reply after two
    months drags a mean past anything he has experienced, and the question
    this answers is when to stop expecting one."""
    pairs = [("2026-08-01", "2026-08-05")] * 8 + [("2026-08-01", "2026-10-01")]
    sentence, over = register.answer_time(pairs, 9)
    assert sentence == ("Im Median kam eine Antwort nach 4 Tagen, "
                        "die langsamste nach 61 Tagen.")
    assert over == ("Gemessen an 9 der 9 beantworteten Bewerbungen — "
                    "Eingangsbestätigungen zählen nicht mit.")


def test_the_median_is_the_middle_and_not_the_smallest():
    """Every corpus in these tests used to be degenerate — median == min ==
    max — so `median → min` passed the whole suite and the headline figure of
    the sentence was pinned by nothing. A realistic spread would then have
    printed "nach einem Tag" where the middle is fourteen."""
    spread = [1, 1, 2, 3, 14, 20, 30, 40, 61]
    pairs = [("2026-08-01",
              (datetime.date(2026, 8, 1) + datetime.timedelta(days=d)).isoformat())
             for d in spread]
    assert register.answer_time(pairs, len(pairs))[0] == (
        "Im Median kam eine Antwort nach 14 Tagen, die langsamste nach 61 Tagen.")


def test_the_middle_is_rounded_and_not_floored():
    """`int()` floors, and it floors in the flattering direction every time:
    a median of 4.5 days printed as 4 understates the wait on the one card
    whose whole claim is that its figures are honest."""
    pairs = [("2026-08-01",
              (datetime.date(2026, 8, 1) + datetime.timedelta(days=d)).isoformat())
             for d in (1, 2, 3, 4, 5, 6, 7, 60)]
    assert "nach 5 Tagen" in register.answer_time(pairs, len(pairs))[0]


def test_the_line_names_the_population_it_actually_measured():
    """Two numbers that are easy to conflate and mean different things: how
    many applications the register calls answered, and how many of those
    carried a readable answer mail. A row he marked "Absage" by hand carries
    no mail at all, and 44 of his rows predate this app."""
    pairs = [("2026-08-01", "2026-08-05")] * 8 + [("", "2026-08-05")] * 3
    _, over = register.answer_time(pairs, 20)
    assert over == ("Gemessen an 8 der 20 beantworteten Bewerbungen — bei den "
                    "übrigen 12 kam die Antwort nicht per E-Mail an.")


@pytest.mark.parametrize("gap, expected", [
    (0, "Im Median kam eine Antwort noch am selben Tag."),
    (1, "Im Median kam eine Antwort nach einem Tag."),
    (9, "Im Median kam eine Antwort nach 9 Tagen."),
])
def test_the_german_inflects_and_drops_a_clause_that_repeats_itself(
        gap, expected):
    """Three things at once: "nach 0 Tagen" is arithmetic showing through the
    German, "nach 1 Tag" is what a form letter writes, and naming the slowest
    when every answer took the same time states one fact twice."""
    sent = datetime.date(2026, 8, 1)
    pairs = [(sent.isoformat(), (sent + datetime.timedelta(days=gap)).isoformat())] \
        * register.ENOUGH_FOR_A_TIME
    assert register.answer_time(pairs, len(pairs))[0] == expected


# ---------------------------------------------------------------------------
# Which clock the aggregate reads
# ---------------------------------------------------------------------------
def _answered(con, *, firma, sent, arrived, classification,
              status="Absage", needs_review=0, message="m"):
    """An application and one reply to it.

    `status` defaults to an ANSWERED one because that is what the aggregate
    measures over — the register's own verdict, not the classifier's. The
    parameter exists so a test can build the other shape too: a reply
    classified and parked on the review shelf while the application is still
    open."""
    row_id = db.add_bewerbung(con, {
        "gesendet_am": sent, "firma": firma, "kanal": "E-Mail",
        "status": status, "email": "hr@example.invalid",
    })
    db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": f"{message}{row_id}",
        "gmail_thread_id": f"t{row_id}", "from_addr": "hr@example.invalid",
        "internal_date": arrived, "bewerbung_id": row_id,
        "classification": classification, "needs_review": needs_review,
    })
    con.commit()
    return row_id


def test_the_signature_sees_the_two_values_the_sentence_is_made_of(con):
    """The house rule, on the newest claim: the page's refresh signature must
    move whenever anything the page STATES changes.

    Two paths that moved nothing. Pressing "Korrigieren" on an already filed
    reply rewrites `classification` between two non-empty values while
    `needs_review` stays 0 and `set_status` short-circuits on an unchanged
    status — every term of the old email tuple held still. And correcting a
    send date through the edit dialog on this very screen moved neither
    COUNT, MAX(id) nor status_history, though the span is computed from it.
    """
    row_id = _answered(con, firma="Eine GmbH", sent="2026-08-01",
                       arrived="2026-08-06T09:00:00", classification="absage")

    before = register.signature(con)
    con.execute("UPDATE email_log SET classification='eingang' "
                "WHERE bewerbung_id=?", (row_id,))
    con.commit()
    assert register.signature(con) != before, "a reclassification has to move it"

    before = register.signature(con)
    con.execute("UPDATE bewerbungen SET gesendet_am='2026-07-01' WHERE id=?",
                (row_id,))
    con.commit()
    assert register.signature(con) != before, "a corrected send date too"


def test_a_reply_still_on_the_review_shelf_is_not_measured(con):
    """The panel's critical finding, as a test.

    A reply is CLASSIFIED the moment it is ingested; its status is written
    only when the verdict was safe enough. A name-arm match, an address
    without DMARC, or a rank the anti-downgrade guard refused all leave the
    mail classified with the application still open. The aggregate counted
    those, so the card printed "beantwortet 0" and, four lines lower,
    "Gemessen an 9 Antworten" — nine answers under a figure that had just
    sworn none arrived."""
    for n in range(9):
        _answered(con, firma=f"Ungeprüfte GmbH {n}", sent="2026-08-01",
                  arrived="2026-08-06T09:00:00", classification="absage",
                  status="Gesendet", needs_review=1, message=f"s{n}")

    apps = [dict(r) for r in db.list_bewerbungen(con)]
    beantwortet = {s.key: s for s in register.ledger(
        {"apps": apps, "applied": 0})}["beantwortet"].count
    assert beantwortet == 0, "the register calls none of them answered"
    assert register.answer_days(db.answer_delays(con)) == []
    assert register.answer_time(db.answer_delays(con), beantwortet) == ("", "")


def test_an_undated_reply_does_not_take_its_application_with_it(con):
    """`internal_date` is TEXT defaulting to '', and '' sorts before every ISO
    date. Under MIN() one undated decision shadowed every dated sibling, and
    because '' cannot be parsed the whole application then left the statistic
    — silently, and only for the applications that got TWO replies."""
    row_id = _answered(con, firma="Zweimal GmbH", sent="2026-08-01",
                       arrived="", classification="absage", message="undated")
    db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": "dated",
        "internal_date": "2026-08-15T09:00:00", "bewerbung_id": row_id,
        "classification": "absage",
    })
    con.commit()
    assert register.answer_days(db.answer_delays(con)) == [14]


def test_a_receipt_is_not_an_answer(con):
    """The trap this statistic exists to avoid. An Eingangsbestätigung is a
    robot replying in the same hour: on the real register its median is nought
    days, and letting it in would report a promptness no employer showed."""
    _answered(con, firma="Schnelle GmbH", sent="2026-08-01",
              arrived="2026-08-01T09:00:00", classification="eingang")
    _answered(con, firma="Automat GmbH", sent="2026-08-01",
              arrived="2026-08-01T09:05:00", classification="auto")
    _answered(con, firma="Entscheidende GmbH", sent="2026-08-01",
              arrived="2026-08-15T09:00:00", classification="absage")

    assert register.answer_days(db.answer_delays(con)) == [14]


def test_the_vocabulary_names_exactly_the_replies_a_person_wrote():
    """The derivation itself, stated. The version of this test that looped
    over `DECISION_CLASSIFICATIONS` and asserted a list of that length proved
    only that the query uses whatever the constant says — never that the
    constant says the right thing, which is the claim the docstring makes."""
    assert DECISION_CLASSIFICATIONS == ("absage", "einladung", "sonstige")
    assert "eingang" not in DECISION_CLASSIFICATIONS, "a robot, in the same hour"
    assert "auto" not in DECISION_CLASSIFICATIONS, "an out-of-office"


def test_every_decision_the_vocabulary_knows_is_measured(con):
    """And each of those three really does reach the statistic, so a fourth
    kind joining the vocabulary tomorrow joins this figure by existing."""
    for n, kind in enumerate(DECISION_CLASSIFICATIONS):
        _answered(con, firma=f"Firma {n}", sent="2026-08-01",
                  arrived="2026-08-03T09:00:00", classification=kind,
                  message=f"k{n}")
    assert register.answer_days(db.answer_delays(con)) == \
        [2] * len(DECISION_CLASSIFICATIONS)


def test_the_first_answer_is_the_one_measured(con):
    """An employer that acknowledges, then rejects, then writes again leaves
    three decision mails on one application. The span is to the FIRST of
    them — how long he waited — so `MIN → MAX` has to be visible."""
    row_id = _answered(con, firma="Mehrfach GmbH", sent="2026-08-01",
                       arrived="2026-08-04T09:00:00", classification="absage",
                       message="first")
    for day, name in (("2026-08-20T09:00:00", "second"),
                      ("2026-09-01T09:00:00", "third")):
        db.add_email_log(con, {
            "direction": "inbound", "gmail_message_id": name,
            "internal_date": day, "bewerbung_id": row_id,
            "classification": "sonstige",
        })
    con.commit()
    assert register.answer_days(db.answer_delays(con)) == [3]


def test_the_aggregate_reads_the_mail_date_not_the_moment_it_was_filed(con):
    """The two clocks, and why the aggregate needs its own.

    The per-row column uses `first_answer_dates` — the moment JobDeck recorded
    the status — deliberately, because for an imported row that is the only
    date there is. But an application sent in June whose rejection was first
    READ by the August pass would enter the aggregate at ~67 days under that
    clock and at its true span under this one. Measured on the real register
    the 90th percentile moves from 67 days to 12."""
    row_id = _answered(con, firma="Juni GmbH", sent="2026-06-10",
                       arrived="2026-06-14T09:00:00", classification="absage")
    # Filed much later, exactly as the first Gmail pass filed the June replies.
    db.set_status(con, row_id, "Absage", source="reply_auto")
    con.commit()

    assert register.answer_days(db.answer_delays(con)) == [4]
    filed = db.first_answer_dates(con)[row_id][:10]
    assert filed != "2026-06-14", "the recording clock is a different date"
    assert (datetime.date.fromisoformat(filed)
            - datetime.date(2026, 6, 10)).days > 4
