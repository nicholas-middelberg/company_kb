"""Python source, chunked by function and class (ADR-0002).

Chunking code by character count cuts functions in half and produces chunks
that cite a line range spanning two unrelated things. Parsing instead means a
chunk is a unit someone could actually read, and its line range points at
something openable.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator, Sequence
from pathlib import Path

from kb.adapters.base import Chunk, SourceDocument, iter_files
from kb.config import DEFAULT_EXCLUDE_DIRS, ChunkConfig

_DEFINITION_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


class CodeAdapter:
    """Discovers .py files and chunks them along definition boundaries."""

    def __init__(
        self,
        chunk_config: ChunkConfig | None = None,
        exclude_dirs: Sequence[str] = DEFAULT_EXCLUDE_DIRS,
    ) -> None:
        self._config = chunk_config or ChunkConfig()
        self._exclude_dirs = exclude_dirs

    @property
    def source_type(self) -> str:
        return "code"

    def discover(self, root: Path) -> Iterator[SourceDocument]:
        for path in iter_files(root, ".py", self._exclude_dirs):
            try:
                content = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                # Discovery must not abort the run for one unreadable file.
                # A file that cannot even be read is skipped rather than
                # recorded as failed, because there is no document to record.
                continue
            yield SourceDocument(
                source_type="code",
                source_uri=str(path.relative_to(root)),
                content=content,
            )

    def chunk(self, document: SourceDocument) -> Sequence[Chunk]:
        """Split into definition-sized units.

        Raises SyntaxError on an unparseable file. The pipeline catches it and
        marks that one document failed (SPECS: per-document failure isolation).
        """
        tree = ast.parse(document.content)
        lines = document.content.splitlines()
        spans = _definition_spans(tree, total_lines=len(lines))

        chunks: list[Chunk] = []
        for start, end, qualname in spans:
            body = "\n".join(lines[start - 1 : end])
            if not body.strip():
                continue
            for piece, piece_start, piece_end in self._split_oversized(body, start, end, qualname):
                chunks.append(
                    Chunk(
                        ordinal=len(chunks),
                        content=piece,
                        file_path=document.source_uri,
                        start_line=piece_start,
                        end_line=piece_end,
                    )
                )
        return chunks

    def _split_oversized(
        self, body: str, start: int, end: int, qualname: str | None
    ) -> Iterator[tuple[str, int, int]]:
        """Split a unit that exceeds the cap, carrying its signature along.

        Without the signature prefix, the second half of a long function
        embeds as anonymous code with no indication of what it belongs to --
        retrievable in theory, useless in a citation.
        """
        if len(body) <= self._config.max_chars:
            yield body, start, end
            return

        prefix = f"# in {qualname}\n" if qualname else ""
        lines = body.splitlines()
        current: list[str] = []
        current_start = start

        for offset, line in enumerate(lines):
            current.append(line)
            joined = "\n".join(current)
            at_boundary = not line.strip()
            if at_boundary and len(joined) >= self._config.target_chars:
                yield prefix + joined, current_start, start + offset
                current = []
                current_start = start + offset + 1

        if current:
            joined = "\n".join(current)
            if joined.strip():
                yield prefix + joined, current_start, end


def _definition_spans(tree: ast.Module, *, total_lines: int) -> list[tuple[int, int, str | None]]:
    """Cover the whole file: each top-level definition, plus the gaps.

    The gaps matter. Module-level code -- imports, constants, the dunder-main
    block -- is where configuration defaults and magic numbers live, and those
    are exactly the things someone asks the agent about.
    """
    spans: list[tuple[int, int, str | None]] = []
    cursor = 1

    for node in tree.body:
        if not isinstance(node, _DEFINITION_NODES):
            continue
        start = min([node.lineno] + [d.lineno for d in node.decorator_list])
        end = node.end_lineno or start
        if start > cursor:
            spans.append((cursor, start - 1, None))
        spans.append((start, end, node.name))
        cursor = end + 1

    if cursor <= total_lines:
        spans.append((cursor, total_lines, None))
    return spans
