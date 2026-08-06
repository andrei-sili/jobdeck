"""How the age of a posting enters the order of the inbox.

Measured on his corpus 2026-08-05: only 5 of 287 open scored postings were
under two weeks old and 78 were over sixty days, while the inbox sorted on
`match_score` alone — so a 92 from March outranked a 78 from yesterday, and the
best-looking rows were the ones most likely to be gone. The fix is not a
cleanup ritual: age becomes a dimension of the ordering, so stale sinks on its
own and nobody has to run anything.

The penalty is a step function on purpose. It is shown to the user next to the
raw score ("match 92 → 72 · 61 Tage alt"), so he can see what age did and
disagree with it; a smooth decay curve orders just as well but explains nothing.

Two rules the shape encodes:

* an UNKNOWN date is never penalised. Absence of information is not evidence of
  staleness, and the boards that omit a date would otherwise be buried for it.
* a penalty may never manufacture a mismatch. `match_score` 0 is reserved for a
  violated hard requirement, so the effective score floors at 1 for anything
  that was scored above 0, and a 0 passes through untouched.

The SQL mirror below is generated from the same table, and a differential test
pins the two implementations equal over every age either will ever see: the
ordering must happen in SQL (it decides which rows a page contains) while the
label is rendered from the same numbers, and two hand-written copies of one
rule drift.
"""

# (age in days STRICTLY below, penalty) — first match wins, ascending.
STALENESS_STEPS = ((14, 0), (30, 5), (60, 12))
STALE_PENALTY = 20  # 60 days and older

# Whole days between a posting's publication date and today, NULL when the date
# is unknown (`julianday('')` is NULL). UTC, like every SQLite 'now': a
# one-day offset at midnight cannot move a posting across a two-week bucket.
AGE_SQL = "CAST(julianday(date('now')) - julianday(published_on) AS INTEGER)"


def penalty(age_days: int | None) -> int:
    """How many points an age costs. An unknown age costs nothing."""
    if age_days is None:
        return 0
    for limit, points in STALENESS_STEPS:
        if age_days < limit:
            return points
    return STALE_PENALTY


def effective_score(match_score: int | None, age_days: int | None) -> int | None:
    """The number the inbox sorts on: the match score, aged.

    Unscored stays unscored (it sorts last either way). A 0 passes through
    untouched and nothing else can reach 0, because that value means "violates
    a hard requirement" and age must never make that claim."""
    if match_score is None:
        return None
    if match_score <= 0:
        return match_score
    return max(1, match_score - penalty(age_days))


def penalty_sql(age_expr: str = AGE_SQL) -> str:
    """`penalty` as a SQL expression over an age expression."""
    steps = " ".join(
        f"WHEN {age_expr}<{limit} THEN {points}" for limit, points in STALENESS_STEPS
    )
    return (f"CASE WHEN {age_expr} IS NULL THEN 0 {steps} "
            f"ELSE {STALE_PENALTY} END")


def effective_score_sql(score_expr: str = "match_score",
                        age_expr: str = AGE_SQL) -> str:
    """`effective_score` as a SQL expression over score and age expressions."""
    return (f"CASE WHEN {score_expr} IS NULL THEN NULL "
            f"WHEN {score_expr}<=0 THEN {score_expr} "
            f"ELSE max(1, {score_expr} - ({penalty_sql(age_expr)})) END")
