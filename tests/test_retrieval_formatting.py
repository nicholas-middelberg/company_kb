"""Citation formatting and log shape. No database needed."""

from datetime import UTC, datetime
from uuid import uuid4

from kb.retrieval.hybrid import RetrievedChunk


def _chunk(**overrides: object) -> RetrievedChunk:
    defaults: dict[str, object] = {
        "chunk_id": uuid4(),
        "document_id": uuid4(),
        "content": "body",
        "source_type": "code",
        "source_uri": "airflow/jobs/scheduler_job_runner.py",
        "content_hash": "abc123",
        "title": None,
        "author": None,
        "created_at": None,
        "file_path": "airflow/jobs/scheduler_job_runner.py",
        "start_line": 400,
        "end_line": 420,
        "heading": None,
        "rrf_score": 0.03,
        "keyword_rank": 1,
        "vector_rank": 2,
    }
    return RetrievedChunk(**{**defaults, **overrides})  # type: ignore[arg-type]


def test_code_citation_carries_a_line_range() -> None:
    assert _chunk().citation == "[airflow/jobs/scheduler_job_runner.py:400-420]"


def test_single_line_code_citation_is_not_a_range() -> None:
    chunk = _chunk(start_line=412, end_line=412)
    assert chunk.citation == "[airflow/jobs/scheduler_job_runner.py:412]"


def test_doc_citation_uses_the_heading_path() -> None:
    chunk = _chunk(
        source_type="doc",
        source_uri="AIP-15.md",
        file_path="AIP-15.md",
        start_line=None,
        end_line=None,
        heading="Scheduler HA",
    )
    assert chunk.citation == "[AIP-15.md § Scheduler HA]"


def test_email_citation_carries_subject_and_date() -> None:
    chunk = _chunk(
        source_type="email",
        source_uri="thread/1",
        file_path=None,
        start_line=None,
        end_line=None,
        title="Scheduler HA and the mini scheduler",
        created_at=datetime(2023, 3, 14, tzinfo=UTC),
    )
    assert chunk.citation == '[dev@ "Scheduler HA and the mini scheduler" (2023-03-14)]'


def test_undated_email_says_so_rather_than_inventing_a_date() -> None:
    chunk = _chunk(
        source_type="email",
        source_uri="t/1",
        file_path=None,
        start_line=None,
        end_line=None,
        title="Subject",
        created_at=None,
    )
    assert "(undated)" in chunk.citation


def test_ticket_citation_does_not_double_the_hash() -> None:
    assert _chunk(source_type="ticket", source_uri="#28149").citation == "[#28149]"
    assert _chunk(source_type="ticket", source_uri="28149").citation == "[#28149]"


def test_found_by_reports_which_arms_contributed() -> None:
    assert _chunk().found_by == "keyword+vector"
    assert _chunk(vector_rank=None).found_by == "keyword"
    assert _chunk(keyword_rank=None).found_by == "vector"


def test_log_entry_records_the_version_identity() -> None:
    """content_hash answers "which version produced that answer" (ADR-0023)."""
    entry = _chunk().as_log_entry()
    assert entry["content_hash"] == "abc123"
    assert set(entry) == {
        "chunk_id",
        "document_id",
        "content_hash",
        "keyword_rank",
        "vector_rank",
        "rrf_score",
    }
