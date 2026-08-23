#!/usr/bin/env python3
"""Validate canonical documentation metadata and local Markdown links."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
REQUIRED_METADATA = {
    "status",
    "owner",
    "scope",
    "last_verified",
    "supersedes",
    "superseded_by",
    "related_adrs",
}
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")


def _frontmatter(path: Path, text: str) -> set[str]:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return set()
    try:
        end = lines.index("---", 1)
    except ValueError:
        return set()
    return {
        match.group(1)
        for line in lines[1:end]
        if (match := re.match(r"^([a-z_]+):", line))
    }


def _local_target(source: Path, raw: str) -> Path | None:
    target = raw.strip().split(maxsplit=1)[0].strip("<>")
    if not target or target.startswith(("#", "https://", "http://", "mailto:")):
        return None
    path_part = unquote(target.split("#", 1)[0])
    return (source.parent / path_part).resolve()


def validate() -> list[str]:
    errors: list[str] = []
    for path in sorted(DOCS.rglob("*.md")):
        text = path.read_text(encoding="utf-8")
        missing = REQUIRED_METADATA - _frontmatter(path, text)
        if missing:
            errors.append(
                f"{path.relative_to(ROOT)}: missing metadata: {', '.join(sorted(missing))}"
            )
        for raw in LINK.findall(text):
            target = _local_target(path, raw)
            if target is not None and not target.exists():
                errors.append(
                    f"{path.relative_to(ROOT)}: missing link target {raw!r}"
                )
    return errors


def main() -> int:
    errors = validate()
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    print("Documentation metadata and relative links are valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
