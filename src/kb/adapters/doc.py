"""Markdown documents, chunked by heading (ADR-0002).

A heading is the author's own statement of where one idea stops and the next
begins, which makes it a better boundary than any character count.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from pathlib import Path

from kb.adapters.base import Chunk, SourceDocument, iter_files
from kb.config import DEFAULT_EXCLUDE_DIRS, ChunkConfig

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


class DocAdapter:
    """Discovers .md files and chunks them by heading section."""

    def __init__(
        self,
        chunk_config: ChunkConfig | None = None,
        exclude_dirs: Sequence[str] = DEFAULT_EXCLUDE_DIRS,
    ) -> None:
        self._config = chunk_config or ChunkConfig()
        self._exclude_dirs = exclude_dirs

    @property
    def source_type(self) -> str:
        return "doc"

    def discover(self, root: Path) -> Iterator[SourceDocument]:
        for path in iter_files(root, ".md", self._exclude_dirs):
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            yield SourceDocument(
                source_type="doc",
                source_uri=str(path.relative_to(root)),
                content=content,
                title=_first_heading(content) or path.stem,
            )

    def chunk(self, document: SourceDocument) -> Sequence[Chunk]:
        chunks: list[Chunk] = []
        for heading_path, body in _sections(document.content):
            if not body.strip():
                continue
            for piece in self._split_oversized(heading_path, body):
                chunks.append(
                    Chunk(
                        ordinal=len(chunks),
                        content=piece,
                        file_path=document.source_uri,
                        heading=heading_path or None,
                    )
                )
        return chunks

    def _split_oversized(self, heading_path: str, body: str) -> Iterator[str]:
        """Split on paragraphs, repeating the heading path for context."""
        if len(body) <= self._config.max_chars:
            yield body
            return

        prefix = f"{heading_path}\n\n" if heading_path else ""
        current: list[str] = []
        for paragraph in body.split("\n\n"):
            candidate = "\n\n".join([*current, paragraph])
            if current and len(prefix) + len(candidate) > self._config.target_chars:
                yield prefix + "\n\n".join(current)
                current = [paragraph]
            else:
                current.append(paragraph)
        if current:
            yield prefix + "\n\n".join(current)


def _sections(content: str) -> Iterator[tuple[str, str]]:
    """Yield (heading path, body) pairs, preamble first with an empty path."""
    stack: list[str] = []
    heading_path = ""
    body: list[str] = []

    for line in content.splitlines():
        match = _HEADING.match(line)
        if match is None:
            body.append(line)
            continue
        if body:
            yield heading_path, "\n".join(body)
            body = []
        level = len(match.group(1))
        title = match.group(2).strip()
        del stack[level - 1 :]
        stack.append(title)
        heading_path = " > ".join(stack)
        body.append(line)

    if body:
        yield heading_path, "\n".join(body)


def _first_heading(content: str) -> str | None:
    for line in content.splitlines():
        match = _HEADING.match(line)
        if match is not None:
            return match.group(2).strip()
    return None
