"""No real personal data in the tracked tree — this repository is public.

Fixtures used to be seeded from live data, so the tree carried the maintainer's
home town, real German postcodes, companies he had really applied to, and the
Referenznummern of their postings. This gate is what keeps them out.

It has two halves, and they run in different places:

* The **denylist half** runs everywhere, CI included. It reads
  `tests/personal_data_denylist.json`, which holds SHA-256 digests and no clear
  text, so the values it forbids are not readable in the repository. It covers
  the maintainer's town, postcode, street, phone and private mailboxes, plus
  every employer name and posting id this clean-up removed — the exact set that
  a revert or a copy-paste could bring back.

* The **register half** runs only on a machine that has his data directory. It
  reads the company names out of his own register at run time, so an employer he
  applies to tomorrow is covered without anyone writing the name down. On CI
  there is no data directory and this half skips, saying so.

The register half opens his database strictly read-only and never writes: it is
the live application's data, and a test must not touch it.

Both halves compare NORMALISED text — casefolded, with every run of non-word
characters collapsed to a single space — so `Musterstadt`, `MUSTERSTADT` and
`muster-stadt` are one value, and a name broken across two lines is still found.
What normalisation does not see through is encoding: a base64 or percent-encoded
copy of a forbidden value reads as a different string and passes.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import subprocess
from contextlib import closing
from pathlib import Path
from urllib.parse import quote

import pytest

from jobdeck import config

ROOT = Path(__file__).resolve().parents[1]
DENYLIST_PATH = Path(__file__).with_name("personal_data_denylist.json")

_NON_WORD = re.compile(r"[\W_]+", re.UNICODE)

# Words that identify nobody, so a company name made only of them is no key at
# all. Legal forms and the handful of nouns half the Handelsregister shares.
_GENERIC = frozenset({
    "ag", "co", "das", "der", "deutschland", "die", "e", "für", "gbr",
    "germany", "gesellschaft", "gmbh", "group", "gruppe", "holding", "inc",
    "kg", "kgaa", "llc", "ltd", "mbh", "ohg", "se", "ug", "und", "v", "von",
})


def normalise(text: str) -> str:
    """Casefolded text with every run of non-word characters as one space."""
    return " ".join(_NON_WORD.sub(" ", (text or "").casefold()).split())


def _digest(key: str) -> bytes:
    return hashlib.sha256(key.encode("utf-8")).digest()


def _where(path: Path, line: int) -> str:
    """A finding's location, repo-relative when the file is inside the repo."""
    try:
        return f"{path.relative_to(ROOT)}:{line}"
    except ValueError:      # a planted file in tmp_path, from this module's own tests
        return f"{path}:{line}"


def tracked_files() -> list[Path]:
    """Every file git tracks, as absolute paths."""
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [ROOT / name.decode() for name in raw.split(b"\0") if name]


def token_stream(path: Path) -> tuple[list[str], list[int]]:
    """The file as normalised tokens, plus the source line each token came from.

    Two empty lists for anything that is not text: a binary file has no words to
    compare, and decoding one would only produce noise.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return [], []
    if b"\0" in raw:
        return [], []
    tokens: list[str] = []
    lines: list[int] = []
    for number, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
        for token in _NON_WORD.sub(" ", line.casefold()).split():
            tokens.append(token)
            lines.append(number)
    return tokens, lines


def denied_digests() -> tuple[dict[bytes, str], int]:
    """The forbidden digests by kind, and the widest window they need."""
    document = json.loads(DENYLIST_PATH.read_text(encoding="utf-8"))
    entries = document["entries"]
    wanted = {bytes.fromhex(entry["sha256"]): entry["kind"] for entry in entries}
    return wanted, max(entry["tokens"] for entry in entries)


def find_denied(paths, wanted: dict[bytes, str], width: int) -> list[str]:
    """Where a forbidden digest appears, as `path:line — kind` lines.

    Only the location and the kind are reported. Whenever this fires the value
    itself is already in the tree at that line, so printing it would add nothing
    but a second copy, in a build log.
    """
    findings = []
    for path in paths:
        tokens, lines = token_stream(path)
        for start in range(len(tokens)):
            key = ""
            for token in tokens[start:start + width]:
                key = f"{key} {token}" if key else token
                kind = wanted.get(_digest(key))
                if kind is not None:
                    findings.append(f"{_where(path, lines[start])} — {kind}")
    return sorted(set(findings))


def connect_read_only(db_path: Path) -> sqlite3.Connection:
    """A connection that cannot write. This is the live application's database.

    The path is percent-escaped because this is a URI, not a filename: a data
    directory with a space or a `?` in it would otherwise open the wrong file,
    or silently open a new empty one.
    """
    return sqlite3.connect(f"file:{quote(str(db_path))}?mode=ro", uri=True)


def register_keys(db_path: Path) -> set[str]:
    """Normalised company names from the register, as search keys.

    A key is a whole name, or the name with its legal form removed — never a
    single word out of the middle of one, because "Solutions" or "Institut" name
    nobody and every prose line using one would then read as a leak. Two tokens
    is the floor for the same reason.
    """
    with closing(connect_read_only(db_path)) as con:
        names = [row[0] for row in con.execute(
            "SELECT DISTINCT firma FROM bewerbungen WHERE firma IS NOT NULL")]
    keys = set()
    for name in names:
        tokens = normalise(name).split()
        core = [token for token in tokens if token not in _GENERIC]
        if len(tokens) >= 2 and core:
            keys.add(" ".join(tokens))
        if len(core) >= 2:
            keys.add(" ".join(core))
    return keys


def find_keys(paths, keys: set[str]) -> list[str]:
    """Where any of `keys` appears, as `path:line — employer` lines."""
    findings = []
    for path in paths:
        tokens, lines = token_stream(path)
        if not tokens:
            continue
        # Padded with a space at both ends so `find` anchors on word breaks: a
        # key must meet whole tokens, never the tail of a longer word.
        stream = f" {' '.join(tokens)} "
        for key in keys:
            found = stream.find(f" {key} ")
            if found >= 0:
                # every token is one space from the next, so the spaces before
                # the match are exactly the tokens before it
                index = stream.count(" ", 0, found)
                findings.append(
                    f"{_where(path, lines[index])} — employer from the register")
    return sorted(set(findings))


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------

def test_the_tracked_tree_carries_no_denied_value():
    """Runs everywhere, CI included — the denylist travels with the repository."""
    wanted, width = denied_digests()

    findings = find_denied(tracked_files(), wanted, width)

    assert not findings, (
        "personal data is back in the tracked tree:\n  "
        + "\n  ".join(findings)
        + "\n\nReplace it with the repo's placeholders (Beispiel GmbH, "
          "Erika Muster, 12345 Musterstadt) — never weaken this test."
    )


def test_the_tracked_tree_names_no_employer_from_the_register():
    """Runs only where his data directory is — on CI there is none, so it skips."""
    if not config.DB_PATH.exists():
        pytest.skip(f"no data directory at {config.DATA_DIR}: nothing to compare")
    try:
        keys = register_keys(config.DB_PATH)
    except sqlite3.Error as exc:
        # The live database, mid-WAL or mid-migration, is the app's to own. A
        # gate that reddens because he happens to have JobDeck open would just
        # teach him to ignore it — name the reason and stand down instead.
        pytest.skip(f"cannot read the register read-only: {exc}")

    findings = find_keys(tracked_files(), keys)

    assert not findings, (
        "a company from the register is named in the tracked tree:\n  "
        + "\n  ".join(findings)
    )


# --------------------------------------------------------------------------
# The gate's own gate: a scanner that cannot fail guards nothing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("text, expected", [
    ("Musterstadt", "musterstadt"),
    ("MUSTER-STADT", "muster stadt"),
    ("Muster_Stadt", "muster stadt"),
    ("Musterstraße", "musterstrasse"),          # casefold expands ß
    ("+49 170 1234567", "49 170 1234567"),
    ("erika@example.org", "erika example org"),
    ("  spaced   out  ", "spaced out"),
    ("", ""),
])
def test_normalisation_reduces_a_value_to_its_words(text, expected):
    assert normalise(text) == expected


def test_a_planted_value_is_found(tmp_path):
    planted = tmp_path / "fixture.py"
    planted.write_text('COMPANY = "Beispiel-Sonderfall GmbH"\n', encoding="utf-8")

    findings = find_denied([planted], {_digest("beispiel sonderfall"): "employer"}, 2)

    assert findings == [f"{planted}:1 — employer"]


def test_a_value_broken_across_lines_is_still_found(tmp_path):
    """A wrapped string literal must not be a way through."""
    planted = tmp_path / "fixture.py"
    planted.write_text('NAME = ("Beispiel "\n        "Sonderfall")\n', encoding="utf-8")

    findings = find_denied([planted], {_digest("beispiel sonderfall"): "employer"}, 2)

    assert findings == [f"{planted}:1 — employer"]


def test_a_clean_file_produces_no_finding(tmp_path):
    clean = tmp_path / "fixture.py"
    clean.write_text('COMPANY = "Firma Beispiel GmbH"\n', encoding="utf-8")

    assert find_denied([clean], {_digest("beispiel sonderfall"): "employer"}, 2) == []


def test_a_key_must_meet_whole_words(tmp_path):
    """`sonderfall` must not be found inside `sonderfallgruppe`."""
    planted = tmp_path / "fixture.py"
    planted.write_text('X = "Sonderfallgruppe"\n', encoding="utf-8")

    assert find_denied([planted], {_digest("sonderfall"): "employer"}, 1) == []


def test_a_binary_file_is_skipped_rather_than_decoded(tmp_path):
    blob = tmp_path / "logo.bin"
    blob.write_bytes(b"\x89PNG\x00beispiel sonderfall\x00")

    assert find_denied([blob], {_digest("beispiel sonderfall"): "employer"}, 2) == []


def test_the_denylist_holds_hashes_and_nothing_else():
    """The file itself must never become the leak it exists to prevent."""
    document = json.loads(DENYLIST_PATH.read_text(encoding="utf-8"))
    assert set(document) == {"_comment", "entries"}
    kinds = {"home town", "home postcode", "home street", "private phone",
             "private mailbox", "employer", "posting id"}
    for entry in document["entries"]:
        assert set(entry) == {"kind", "tokens", "sha256"}, entry
        assert entry["kind"] in kinds, entry
        assert re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]), entry
        assert 1 <= entry["tokens"] <= 8, entry
    digests = [entry["sha256"] for entry in document["entries"]]
    assert len(set(digests)) == len(digests), "duplicate entry"


def test_the_denylist_covers_every_kind_the_gate_promises():
    """The module docstring names what this gate protects; the list must hold it."""
    document = json.loads(DENYLIST_PATH.read_text(encoding="utf-8"))
    present = {entry["kind"] for entry in document["entries"]}
    assert {"home town", "home postcode", "private phone", "employer"} <= present


# --- the register half, proved without touching his database ----------------

def _register_db(path: Path, names: list[str]) -> None:
    with closing(sqlite3.connect(path)) as con:
        con.execute("CREATE TABLE bewerbungen (id INTEGER PRIMARY KEY, firma TEXT)")
        con.executemany("INSERT INTO bewerbungen (firma) VALUES (?)",
                        [(name,) for name in names])
        con.commit()


def test_the_register_connection_refuses_to_write(tmp_path):
    """It is the running application's data. Read-only is not a convention here."""
    path = tmp_path / "jobdeck.db"
    _register_db(path, ["Beispiel GmbH"])

    with closing(connect_read_only(path)) as con:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("UPDATE bewerbungen SET firma = 'x'")
        assert con.execute("SELECT firma FROM bewerbungen").fetchone()[0] \
            == "Beispiel GmbH"


def test_a_data_directory_with_uri_punctuation_still_opens(tmp_path):
    """`JOBDECK_DATA_DIR` is a path, not a URI. Escaping is what keeps it one."""
    awkward = tmp_path / "my data?dir#1"
    awkward.mkdir()
    path = awkward / "jobdeck.db"
    _register_db(path, ["Sonderfall Technik GmbH"])

    assert "sonderfall technik gmbh" in register_keys(path)


def test_an_unreadable_register_raises_rather_than_reading_nothing(tmp_path):
    """The gate skips on this error. It must be an error, not an empty answer:
    a silent empty register would pass the gate while checking nothing."""
    not_a_database = tmp_path / "jobdeck.db"
    not_a_database.write_bytes(b"this is not sqlite")

    with pytest.raises(sqlite3.Error):
        register_keys(not_a_database)


def test_register_keys_skip_a_name_that_is_only_a_legal_form(tmp_path):
    path = tmp_path / "jobdeck.db"
    _register_db(path, ["GmbH & Co. KG", "Sonderfall GmbH", "Alpha Beta GmbH"])

    keys = register_keys(path)

    assert "gmbh co kg" not in keys          # names nobody
    assert "sonderfall gmbh" in keys         # the whole name
    assert "sonderfall" not in keys          # never a lone word out of a name
    assert "alpha beta" in keys              # the name without its legal form


def test_the_register_half_finds_a_planted_company(tmp_path):
    path = tmp_path / "jobdeck.db"
    _register_db(path, ["Sonderfall Technik GmbH"])
    planted = tmp_path / "fixture.py"
    planted.write_text('A = "x"\nB = "y"\nC = "Sonderfall Technik GmbH"\n',
                       encoding="utf-8")

    findings = find_keys([planted], register_keys(path))

    assert findings == [f"{planted}:3 — employer from the register"]


def test_the_register_half_leaves_a_clean_fixture_alone(tmp_path):
    path = tmp_path / "jobdeck.db"
    _register_db(path, ["Sonderfall Technik GmbH"])
    clean = tmp_path / "fixture.py"
    clean.write_text('COMPANY = "Firma Beispiel GmbH"\n', encoding="utf-8")

    assert find_keys([clean], register_keys(path)) == []
