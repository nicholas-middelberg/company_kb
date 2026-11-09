"""Content hashing, which is what makes re-ingestion idempotent.

The hash is taken over *normalised* content so that a reformatting-only change
does not trigger a full re-embed. Re-embedding is the expensive step
(ADR-0031), and a trailing-whitespace diff is not a change in meaning.
"""

from __future__ import annotations

import hashlib


def normalise(text: str) -> str:
    """Collapse differences that cannot change what a document means.

    Line endings, trailing whitespace per line, and leading/trailing blank
    lines. Deliberately conservative: anything that could alter meaning --
    internal blank lines, indentation -- is preserved, because a hash that
    ignores a real change is worse than one that fires too often.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "\n".join(line.rstrip() for line in lines).strip("\n")


def content_hash(text: str) -> str:
    """SHA-256 of normalised content, hex encoded."""
    return hashlib.sha256(normalise(text).encode("utf-8")).hexdigest()
