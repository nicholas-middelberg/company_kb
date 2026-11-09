"""Pins ADR-0022's loading rules at the source level.

These are the cheapest useful tests in the suite. The rules they guard were
previously enforced by nothing but a code comment: the integration tests use a
fake provider, so adding `trust_remote_code=True` to the real one would have
left all 38 tests green.

Reading source text is a blunt instrument, but it needs no model, no network
and no GPU, so it runs on every commit -- which is the property that matters
for a rule that must never silently lapse.
"""

from pathlib import Path

PROVIDERS = Path(__file__).resolve().parent.parent / "src" / "kb" / "providers"
LOCAL = PROVIDERS / "local_embeddings.py"


def _source(path: Path) -> str:
    return path.read_text()


def test_trust_remote_code_is_never_enabled_anywhere() -> None:
    """The flag lets a model repository execute arbitrary Python on load.

    Checked across the whole package, not just the current loader, so a second
    provider cannot reintroduce it.
    """
    for path in PROVIDERS.rglob("*.py"):
        text = _source(path)
        assert "trust_remote_code=True" not in text, path.name
        assert "trust_remote_code = True" not in text, path.name
        assert '"trust_remote_code": True' not in text, path.name


def test_safetensors_is_requested_explicitly() -> None:
    """The model repository also ships a pytorch_model.bin pickle, so relying
    on loader precedence instead of asking is a silent-fallback risk."""
    assert '"use_safetensors": True' in _source(LOCAL)


def test_the_revision_is_pinned_at_load() -> None:
    """A floating branch means the weights can change underneath an eval run."""
    assert "revision=" in _source(LOCAL)


def test_downloads_are_refused_by_default() -> None:
    """Ingestion must not fetch; `kb model-warm` is the explicit exception."""
    text = _source(LOCAL)
    assert "local_files_only=not allow_download" in text
    assert "allow_download: bool = False" in text


def test_the_pinned_revision_is_a_full_commit_sha() -> None:
    """A tag or branch name would defeat the point of pinning."""
    from kb.config import EmbeddingConfig

    revision = EmbeddingConfig().revision
    assert len(revision) == 40
    assert all(c in "0123456789abcdef" for c in revision)
