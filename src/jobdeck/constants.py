"""Domain constants shared across the application.

Status vocabulary is carried over unchanged from the legacy tracker so the
existing application history keeps its meaning.
"""

# Application channels
KANAL_OPTIONS = ["E-Mail", "Online-Portal", "Post", "Initiativ", "Sonstiges"]

# Application statuses (German, as stored in the DB since the legacy app)
STATUS_OPTIONS = [
    "Gesendet",
    "In Bearbeitung",
    "Antwort erhalten",
    "Einladung",
    "Absage",
    "Zurückgezogen",
]

# Statuses that mean "still waiting for an answer" (follow-up reminders)
OFFENE_STATUS = {"Gesendet", "In Bearbeitung"}

# Statuses that mean the company replied (response-rate metric)
BEANTWORTET_STATUS = {"Antwort erhalten", "Einladung", "Absage"}

# Row background colors by status (visual scanning in tables)
STATUS_COLORS = {
    "Gesendet": "#ffffff",
    "In Bearbeitung": "#fff6d6",
    "Antwort erhalten": "#d9e8f7",
    "Einladung": "#d7f3d7",
    "Absage": "#f6dcdc",
    "Zurückgezogen": "#e6e6e6",
}
# Highlight for applications past the follow-up threshold without an answer
FAELLIG_COLOR = "#ffd28a"

# Rank used to prevent automatic status downgrades: a late confirmation
# e-mail must never overwrite an already-recorded invitation or rejection.
STATUS_RANK = {
    "": 0,
    "Gesendet": 1,
    "In Bearbeitung": 2,
    "Antwort erhalten": 3,
    "Einladung": 4,
    "Absage": 4,
    "Zurückgezogen": 4,
}

# Lifecycle of a discovered job posting. `portal` was removed in v10: having
# opened an employer's form is a moment (`jobs.form_opened_at`), not a state of
# the posting, and writing it as a status hid the very posting he had just
# started — every view here pins a concrete status.
JOB_STATUS = ["new", "duplicate", "skipped", "drafted", "applied"]

# What `form_opened_at` holds for a form that was already open before v10 and
# left no evidence of when. A literal rather than a date, so nothing can format
# it into a plausible age: eleven real rows carried no clue, and inventing one
# would sort them among applications started this minute.
FORM_OPENED_UNKNOWN = "unbekannt"

# What the last liveness probe observed about a posting's ad ('' = never asked).
# Here rather than in services/liveness.py because db.py builds SQL from these
# values and cannot import a service: a magic string in a query that has to
# match a Python constant elsewhere is how the two drift apart.
LIVENESS_ALIVE = "alive"
LIVENESS_GONE = "gone"

# Lifecycle of an AI-generated application draft. 'sent' means this app put it
# in an e-mail; 'filed' means it left inside the Bewerbungsmappe he uploaded to
# an employer's form. Both are terminal and both name a real application — the
# difference is which hand carried it, and the register says so.
DRAFT_STATUS = ["generating", "ready", "failed", "approved", "sending", "sent",
                "filed", "discarded"]

# A letter that has gone out, whichever way. Nothing may rewrite one of these,
# and neither waits in the Postausgang.
DRAFT_DELIVERED = ("sent", "filed")

# How many messages may leave in a day when he has never said. Stated once, in
# the layer both the send gate and the screens that draw the budget can see: a
# second copy would let a bar promise a send the gate below it refuses.
DEFAULT_DAILY_CAP = "15"

# How many tailored letters may be written in a day when he has never said.
# Since v10 a form application writes one by itself, so the form path spends
# money per press for the first time — his decision (2026-08-15) was a HARD
# cap, raised deliberately in Einstellungen, because an override always one
# press away is a speed bump rather than a limit.
DEFAULT_DAILY_DRAFT_CAP = "10"

# email_log.direction values. Test sends (real sending OFF) get their own
# direction: they must never look like a real application in the audit log,
# but still count toward the daily cap — the account's reputation does not
# care who the recipient was.
EMAIL_OUTBOUND = "outbound"
EMAIL_OUTBOUND_TEST = "outbound_test"

# Reply classifications produced by the inbox pipeline
CLASSIFICATIONS = ["eingang", "absage", "einladung", "sonstige"]

# Mapping from reply classification to application status
CLASSIFICATION_TO_STATUS = {
    "eingang": "In Bearbeitung",
    "absage": "Absage",
    "einladung": "Einladung",
    "sonstige": "Antwort erhalten",
}

# Columns of the legacy `bewerbungen` table shown in list views: (key, label)
DB_COLUMNS = [
    ("gesendet_am", "Datum"),
    ("firma", "Firma"),
    ("email", "E-Mail"),
    ("ansprechpartner", "Ansprechpartner"),
    ("strasse", "Straße"),
    ("plz_ort", "PLZ Ort"),
    ("kanal", "Kanal"),
    ("status", "Status"),
    ("notiz", "Notiz"),
]
