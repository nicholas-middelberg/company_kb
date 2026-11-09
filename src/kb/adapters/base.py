"""The adapter seam: engine stays general, adapters know one source type.

Per ADR-0002 the engine never learns what a mailing list is. An adapter turns
one kind of raw thing into documents and chunks, and that is all it does.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

SOURCE_TYPES = ("code", "email", "doc", "ticket")


@dataclass(frozen=True)
class SourceDocument:
    """One document as discovered, before chunking."""

    source_type: str
    source_uri: str
    content: str
    title: str | None = None
    author: str | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class Chunk:
    """One chunk, carrying enough locator metadata to cite precisely.

    The locator fields are source-specific and mostly None: code uses
    file_path/start_line/end_line, docs use heading, transcripts use speaker.
    A citation that cannot be opened is a failure, so these are not optional
    decoration.
    """

    ordinal: int
    content: str
    file_path: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    heading: str | None = None
    speaker: str | None = None
    message_id: str | None = None


class Adapter(Protocol):
    """Parse and chunk one source type."""

    @property
    def source_type(self) -> str: ...

    def discover(self, root: Path) -> Iterator[SourceDocument]:
        """Yield documents found under root. Must not raise for one bad file."""
        ...

    def chunk(self, document: SourceDocument) -> Sequence[Chunk]:
        """Split into chunks. May raise; the pipeline isolates the failure."""
        ...


def iter_files(root: Path, suffix: str, exclude_dirs: Sequence[str]) -> Iterator[Path]:
    """Walk root for files with `suffix`, skipping excluded directories.

    Prunes whole subtrees rather than filtering results, so a virtualenv or a
    .git directory is never even descended into.
    """
    excluded = set(exclude_dirs)
    stack = [root]
    found: list[Path] = []
    while stack:
        current = stack.pop()
        try:
            entries = list(current.iterdir())
        except (OSError, PermissionError):
            continue
        for entry in entries:
            if entry.is_dir():
                if entry.name not in excluded:
                    stack.append(entry)
            elif entry.suffix == suffix:
                found.append(entry)
    yield from sorted(found)
