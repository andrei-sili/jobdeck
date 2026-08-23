#!/usr/bin/env python3
"""Fail when tracked files contain high-confidence credential signatures."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = {
    "private key": re.compile(
        rb"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"
    ),
    "AWS access key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{36,255}\b"),
    "GitHub fine-grained token": re.compile(rb"\bgithub_pat_[A-Za-z0-9_]{70,255}\b"),
    "Google API key": re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b"),
    "Google OAuth client secret": re.compile(rb"\bGOCSPX-[0-9A-Za-z_-]{20,}\b"),
    "Google OAuth refresh token": re.compile(rb"\b1//[0-9A-Za-z_-]{30,}\b"),
    "Slack token": re.compile(rb"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
    "Anthropic API key": re.compile(rb"\bsk-ant-[A-Za-z0-9_-]{40,}\b"),
    "OpenAI project key": re.compile(rb"\bsk-proj-[A-Za-z0-9_-]{40,}\b"),
}


def _tracked_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT
    )
    return [ROOT / raw.decode() for raw in output.split(b"\0") if raw]


def scan() -> list[str]:
    findings: list[str] = []
    for path in _tracked_files():
        try:
            content = path.read_bytes()
        except OSError as exc:
            findings.append(f"{path.relative_to(ROOT)}: cannot scan: {exc}")
            continue
        if b"\0" in content:
            continue
        for name, pattern in PATTERNS.items():
            if pattern.search(content):
                findings.append(f"{path.relative_to(ROOT)}: possible {name}")
    return findings


def main() -> int:
    findings = scan()
    if findings:
        print("\n".join(findings), file=sys.stderr)
        return 1
    print("No high-confidence credential signatures found in tracked files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
