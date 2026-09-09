"""Schema v18: the five dimensions behind a match score, stored and re-derived."""

from jobdeck import db, migrations, scoreweights


def _job(con, external_id, **over):
    values = {"source": "stub", "external_id": external_id, "title": "Dev",
              "company": f"Firma {external_id}", "description": "x"}
    values.update(over)
    return db.insert_job_if_new(con, values)


SUBS = {"role": 80, "stack": 60, "level": 100, "language": 100, "conditions": None}


def test_v17_becomes_v18_with_five_nullable_columns_and_scores_untouched(data_dir):
    con = db.connect()
    migrations.migrate(con)
    job_id = _job(con, "old")
    db.set_job_score(con, job_id, 74, "weil")
    for col in scoreweights.COLUMNS:
        con.execute(f"ALTER TABLE jobs DROP COLUMN {col}")
    con.execute("PRAGMA user_version = 17")
    con.commit()
    assert "score_role" not in [r[1] for r in con.execute("PRAGMA table_info(jobs)")]

    migrations.migrate(con)

    # the literal, not the constant: a test comparing the constant with
    # itself would stay green with the bump forgotten
    assert migrations.SCHEMA_VERSION == 18
    assert con.execute("PRAGMA user_version").fetchone()[0] == 18
    row = con.execute("SELECT * FROM jobs").fetchone()
    assert row["match_score"] == 74 and row["match_reason"] == "weil"
    assert all(row[col] is None for col in scoreweights.COLUMNS)
    migrations.migrate(con)  # idempotent with the columns present
    con.close()


def test_a_verdict_stores_its_dimensions_and_a_bare_score_clears_them(con):
    job_id = _job(con, "j1")
    db.set_job_score(con, job_id, 77, "weil", SUBS)
    row = db.get_job(con, job_id)
    assert (row["score_role"], row["score_stack"], row["score_level"],
            row["score_language"], row["score_conditions"]) == (80, 60, 100, 100, None)

    db.set_job_score(con, job_id, 50, "neu")
    row = db.get_job(con, job_id)
    assert row["match_score"] == 50
    assert all(row[col] is None for col in scoreweights.COLUMNS)


def test_weights_default_and_read_back_through_the_combining_rule(con):
    assert db.score_weights(con) == scoreweights.default_weights()
    db.set_setting(con, "score_weight_stack", "70")
    db.set_setting(con, "score_weight_role", "not a number")
    con.commit()
    assert db.score_weights(con) == {**scoreweights.default_weights(), "stack": 70}


def test_recomputing_moves_only_rows_that_hold_the_dimensions(con):
    with_dims = _job(con, "dims")
    db.set_job_score(con, with_dims, 80, "weil", SUBS)      # default weights: 80
    bare = _job(con, "bare")
    db.set_job_score(con, bare, 65, "weil")                # the model's number
    knocked = _job(con, "ko")
    db.set_job_score(con, knocked, 0, "Ausbildung", SUBS)  # a knock-out stays 0
    applied = _job(con, "applied")
    db.set_job_score(con, applied, 80, "weil", SUBS)
    db.set_job_status(con, applied, "applied")            # history is kept
    con.commit()

    stack_heavy = {**scoreweights.default_weights(), "stack": 100, "role": 0}
    changed = db.recompute_scores(con, stack_heavy)
    con.commit()

    assert changed == 1
    scores = {r["external_id"]: r["match_score"]
              for r in con.execute("SELECT external_id, match_score FROM jobs")}
    # (100*60 + 20*100 + 10*100) / 130 = 69.23 → 69
    assert scores == {"dims": 69, "bare": 65, "ko": 0, "applied": 80}
    assert db.recompute_scores(con, stack_heavy) == 0  # settled: nothing moves twice


def test_saving_weights_stores_them_parsed_and_re_derives_at_once(con):
    job_id = _job(con, "j1")
    db.set_job_score(con, job_id, 80, "weil", SUBS)
    con.commit()

    changed = db.save_score_weights(
        con, {"role": 0, "stack": 100, "level": 20, "language": "10",
              "conditions": "zehn"})
    con.commit()

    assert changed == 1
    assert db.get_job(con, job_id)["match_score"] == 69
    assert db.get_setting(con, "score_weight_conditions") == "10"  # its default
    assert db.get_setting(con, "score_weight_role") == "0"
    assert db.score_weights(con) == {"role": 0, "stack": 100, "level": 20,
                                     "language": 10, "conditions": 10}


def test_resetting_a_score_clears_its_dimensions_too(con):
    job_id = _job(con, "j1")
    db.set_job_score(con, job_id, 80, "weil", SUBS)
    con.commit()
    assert db.reset_job_scores(con, [job_id]) == 1
    row = db.get_job(con, job_id)
    assert row["match_score"] is None
    assert all(row[col] is None for col in scoreweights.COLUMNS)


def test_the_jobs_signature_moves_when_two_rows_trade_scores(con):
    a = _job(con, "a")
    b = _job(con, "b")
    db.set_job_score(con, a, 60, "weil")
    db.set_job_score(con, b, 80, "weil")
    con.commit()
    before = db.data_signature(con)
    db.set_job_score(con, a, 80, "weil")
    db.set_job_score(con, b, 60, "weil")
    con.commit()
    assert db.data_signature(con) != before
