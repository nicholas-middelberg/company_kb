"""Adapter chunking: boundaries, locators, and failure behaviour."""

from pathlib import Path

import pytest

from kb.adapters.base import SourceDocument, iter_files
from kb.adapters.code import CodeAdapter
from kb.adapters.doc import DocAdapter
from kb.config import DEFAULT_EXCLUDE_DIRS, ChunkConfig

CODE = '''"""Module docstring."""

import os

MAX_RETRIES = 3


def schedule(dag_id):
    return os.path.join("/runs", dag_id)


class Scheduler:
    def loop(self):
        pass
'''


def _code_doc(content: str = CODE) -> SourceDocument:
    return SourceDocument(source_type="code", source_uri="jobs/scheduler.py", content=content)


def test_code_chunks_on_definition_boundaries():
    chunks = CodeAdapter().chunk(_code_doc())
    bodies = [c.content for c in chunks]
    assert any("def schedule" in b for b in bodies)
    assert any("class Scheduler" in b for b in bodies)
    # The definitions must not land in the same chunk as each other.
    assert not any("def schedule" in b and "class Scheduler" in b for b in bodies)


def test_code_captures_module_level_code():
    """Imports and constants are where defaults live, and defaults are what
    people ask the agent about."""
    bodies = [c.content for c in CodeAdapter().chunk(_code_doc())]
    assert any("MAX_RETRIES = 3" in b for b in bodies)


def test_code_chunks_carry_citable_line_ranges():
    for chunk in CodeAdapter().chunk(_code_doc()):
        assert chunk.file_path == "jobs/scheduler.py"
        assert chunk.start_line is not None
        assert chunk.end_line is not None
        assert chunk.start_line <= chunk.end_line


def test_code_line_ranges_point_at_the_real_text():
    lines = CODE.splitlines()
    for chunk in CodeAdapter().chunk(_code_doc()):
        assert chunk.start_line is not None and chunk.end_line is not None
        excerpt = "\n".join(lines[chunk.start_line - 1 : chunk.end_line])
        # Oversized chunks get a "# in <name>" prefix; the rest must match.
        assert chunk.content.removeprefix("# in Scheduler\n") in excerpt or (
            excerpt in chunk.content
        )


def test_code_ordinals_are_dense_and_ordered():
    chunks = CodeAdapter().chunk(_code_doc())
    assert [c.ordinal for c in chunks] == list(range(len(chunks)))


def test_oversized_definition_splits_and_carries_its_signature():
    body = "\n".join(f"    value_{i} = {i}\n" for i in range(400))
    content = f"def enormous():\n{body}\n"
    chunks = CodeAdapter(ChunkConfig(target_chars=200, max_chars=400)).chunk(_code_doc(content))
    assert len(chunks) > 1
    # Without the signature the later pieces embed as anonymous code.
    assert any("# in enormous" in c.content for c in chunks[1:])


def test_unparseable_code_raises_for_the_pipeline_to_isolate():
    with pytest.raises(SyntaxError):
        CodeAdapter().chunk(_code_doc("def broken(:\n  nope\n"))


def test_doc_chunks_by_heading_with_a_nested_path():
    content = "# Top\n\nintro\n\n## Child\n\nbody\n\n### Grandchild\n\ndeep\n"
    chunks = DocAdapter().chunk(
        SourceDocument(source_type="doc", source_uri="a.md", content=content)
    )
    headings = [c.heading for c in chunks]
    assert "Top" in headings
    assert "Top > Child" in headings
    assert "Top > Child > Grandchild" in headings


def test_doc_sibling_heading_pops_the_stack():
    content = "# Top\n\na\n\n## One\n\nb\n\n## Two\n\nc\n"
    headings = [
        c.heading
        for c in DocAdapter().chunk(
            SourceDocument(source_type="doc", source_uri="a.md", content=content)
        )
    ]
    assert "Top > Two" in headings
    assert "Top > One > Two" not in headings


def test_iter_files_prunes_excluded_directories(tmp_path: Path):
    """Pointing ingest at a repo root otherwise walks into the virtualenv."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("x = 1\n")
    (tmp_path / ".venv" / "lib").mkdir(parents=True)
    (tmp_path / ".venv" / "lib" / "vendored.py").write_text("y = 2\n")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cached.py").write_text("z = 3\n")

    found = [p.name for p in iter_files(tmp_path, ".py", DEFAULT_EXCLUDE_DIRS)]
    assert found == ["real.py"]
