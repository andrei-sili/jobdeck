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

# Lifecycle of a discovered job posting
JOB_STATUS = ["new", "duplicate", "skipped", "portal", "drafted", "applied"]

# Lifecycle of an AI-generated application draft
DRAFT_STATUS = ["generating", "ready", "failed", "approved", "sending", "sent", "discarded"]

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
