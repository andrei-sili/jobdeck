"""The numbers on the Bewerbungen screen, after the funnel came off it.

He said it plainly: "unde este statistica este de neinteles, trebuie sa fie
simpla". The funnel counted postings — found, scored, opened, written to — on
a screen about applications, and every one of those figures is now stated on
Stellen, on the rows themselves. What replaced it is two groups that each add
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


def test_no_invitation_yet_is_printed_rather_than_hidden():
    """Unlike the waiting-for-a-score line on Stellen, a zero belongs here.
    The difference is what the number is about: a background worker's empty
    queue is nothing to report, and no invitations yet is the score."""
    steps = {s.key: s for s in register.answers(_apps(Absage=3))}
    assert steps["einladung"].count == 0
    assert steps["einladung"].label == "Einladungen"


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
    assert register.answer_time(few) == ("", "")
    enough = [("2026-08-01", "2026-08-05")] * register.ENOUGH_FOR_A_TIME
    assert register.answer_time(enough)[0] != ""


def test_the_sentence_says_the_middle_and_the_slowest():
    """The median and the worst case, not the average: one reply after two
    months drags a mean past anything he has experienced, and the question
    this answers is when to stop expecting one."""
    pairs = [("2026-08-01", "2026-08-05")] * 8 + [("2026-08-01", "2026-10-01")]
    sentence, over = register.answer_time(pairs)
    assert sentence == ("Im Median kam eine Antwort nach 4 Tagen, "
                        "die langsamste nach 61 Tagen.")
    assert over.startswith("Gemessen an 9 Antworten")
    assert "Eingangsbestätigungen zählen nicht mit" in over


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
    assert register.answer_time(pairs)[0] == expected


# ---------------------------------------------------------------------------
# Which clock the aggregate reads
# ---------------------------------------------------------------------------
def _answered(con, *, firma, sent, arrived, classification):
    row_id = db.add_bewerbung(con, {
        "gesendet_am": sent, "firma": firma, "kanal": "E-Mail",
        "status": "Gesendet", "email": "hr@example.invalid",
    })
    db.add_email_log(con, {
        "direction": "inbound", "gmail_message_id": f"m{row_id}",
        "gmail_thread_id": f"t{row_id}", "from_addr": "hr@example.invalid",
        "internal_date": arrived, "bewerbung_id": row_id,
        "classification": classification,
    })
    con.commit()
    return row_id


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


def test_every_decision_the_vocabulary_knows_is_measured(con):
    """Derived from `CLASSIFICATION_TO_STATUS`, not hand-listed: a new kind of
    reply that moves an application into an answered state joins this figure
    by existing, rather than by someone remembering to add it."""
    for n, kind in enumerate(DECISION_CLASSIFICATIONS):
        _answered(con, firma=f"Firma {n}", sent="2026-08-01",
                  arrived="2026-08-03T09:00:00", classification=kind)
    assert register.answer_days(db.answer_delays(con)) == \
        [2] * len(DECISION_CLASSIFICATIONS)


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
