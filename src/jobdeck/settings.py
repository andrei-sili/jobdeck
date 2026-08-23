"""Typed access to the string-backed ``app_settings`` table.

All stored values remain strings for compatibility. Readers use the helpers in
this module so malformed or hand-edited values have one deterministic policy
instead of raising from a UI page or background worker.
"""

from __future__ import annotations

import math
import sqlite3


def text(con: sqlite3.Connection, key: str, default: str = "") -> str:
    row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    return str(row["value"]) if row and row["value"] is not None else default


def parse_bool(raw: object, default: bool = False) -> bool:
    value = str(raw).strip()
    if value == "1":
        return True
    if value == "0":
        return False
    return default


def boolean(
    con: sqlite3.Connection, key: str, default: bool = False
) -> bool:
    return parse_bool(text(con, key, "1" if default else "0"), default)


def parse_int(
    raw: object,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    allow_decimal: bool = False,
    clamp: bool = True,
) -> int:
    """Parse a finite integer and clamp it to optional inclusive bounds."""
    try:
        stripped = str(raw).strip()
        value = int(float(stripped)) if allow_decimal else int(stripped)
    except (TypeError, ValueError, OverflowError):
        return default
    if minimum is not None and value < minimum:
        if not clamp:
            return default
        value = minimum
    if maximum is not None and value > maximum:
        if not clamp:
            return default
        value = maximum
    return value


def integer(
    con: sqlite3.Connection,
    key: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
    allow_decimal: bool = False,
    clamp: bool = True,
) -> int:
    return parse_int(
        text(con, key, ""),
        default,
        minimum=minimum,
        maximum=maximum,
        allow_decimal=allow_decimal,
        clamp=clamp,
    )


def parse_float(
    raw: object,
    default: float,
    *,
    minimum_exclusive: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if not math.isfinite(value):
        return default
    if minimum_exclusive is not None and value <= minimum_exclusive:
        return default
    if maximum is not None and value > maximum:
        return default
    return value


def floating(
    con: sqlite3.Connection,
    key: str,
    default: float,
    *,
    minimum_exclusive: float | None = None,
    maximum: float | None = None,
) -> float:
    return parse_float(
        text(con, key, ""),
        default,
        minimum_exclusive=minimum_exclusive,
        maximum=maximum,
    )
