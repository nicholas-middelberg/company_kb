"""The Voyage ceiling provider, exercised with a stub client.

No network and no `voyageai` package: the provider accepts an injected client
precisely so these run in CI, where the optional `ceiling` extra is absent.
"""

from collections.abc import Sequence
from typing import Any

import pytest

from kb.config import CeilingConfig
from kb.providers.voyage_embeddings import (
    MAX_BATCH,
    MissingApiKeyError,
    VoyageEmbeddingProvider,
)


class StubResponse:
    def __init__(self, embeddings: list[list[float]], total_tokens: int) -> None:
        self.embeddings = embeddings
        self.total_tokens = total_tokens


class StubClient:
    """Records how it was called and returns vectors of the asked-for size."""

    def __init__(self, dimension: int = 1024) -> None:
        self.dimension = dimension
        self.calls: list[dict[str, Any]] = []

    def embed(
        self,
        texts: list[str],
        *,
        model: str,
        input_type: str | None = None,
        output_dimension: int | None = None,
        truncation: bool = True,
    ) -> StubResponse:
        self.calls.append(
            {
                "texts": texts,
                "model": model,
                "input_type": input_type,
                "output_dimension": output_dimension,
                "truncation": truncation,
            }
        )
        width = output_dimension or self.dimension
        return StubResponse([[0.1] * width for _ in texts], total_tokens=7 * len(texts))


def _provider(**kwargs: Any) -> tuple[VoyageEmbeddingProvider, StubClient]:
    client = StubClient()
    config = CeilingConfig(**kwargs) if kwargs else CeilingConfig()
    return VoyageEmbeddingProvider(config, client=client), client


def test_reports_the_configured_model_and_dimension() -> None:
    provider, _ = _provider()
    assert provider.model_name == "voyage-3-large"
    assert provider.dimension == 1024


def test_document_purpose_maps_to_voyage_input_type() -> None:
    provider, client = _provider()
    provider.embed(["a"], purpose="document")
    assert client.calls[0]["input_type"] == "document"


def test_query_purpose_maps_to_voyage_input_type() -> None:
    """The reason `purpose` exists on the interface at all.

    Voyage prepends a different instruction per type, so a ceiling run that
    embedded queries as documents would understate the hosted model and
    flatter the local one (ADR-0006)."""
    provider, client = _provider()
    provider.embed(["a"], purpose="query")
    assert client.calls[0]["input_type"] == "query"


def test_defaults_to_document_so_indexing_cannot_get_it_wrong() -> None:
    provider, client = _provider()
    provider.embed(["a"])
    assert client.calls[0]["input_type"] == "document"


def test_output_dimension_is_sent_explicitly() -> None:
    """So the reported dimension cannot drift from the produced one."""
    provider, client = _provider(output_dimension=512)
    vectors = provider.embed(["a"])
    assert client.calls[0]["output_dimension"] == 512
    assert len(vectors[0]) == 512
    assert provider.dimension == 512


def test_tokens_are_accumulated_because_voyage_bills_per_token() -> None:
    """Request count is not the cost; tokens are."""
    provider, _ = _provider()
    provider.embed(["a", "b"])
    provider.embed(["c"])
    assert provider.total_tokens == 21


def test_empty_input_makes_no_request() -> None:
    provider, client = _provider()
    assert provider.embed([]) == []
    assert client.calls == []


def test_oversized_batch_is_refused() -> None:
    """Voyage caps a request at 1000 texts; fail before the API does."""
    provider, _ = _provider()
    with pytest.raises(ValueError, match="per-request limit"):
        provider.embed(["x"] * (MAX_BATCH + 1))


def test_configured_batch_over_the_limit_is_refused_at_construction() -> None:
    with pytest.raises(ValueError, match="exceeds Voyage's limit"):
        VoyageEmbeddingProvider(CeilingConfig(batch_size=MAX_BATCH + 1), client=StubClient())


def test_unexpected_dimension_from_the_api_is_caught() -> None:
    """A silently differently-sized vector would be written to a column that
    accepts it, and compared against vectors it does not belong with."""

    class WrongWidthClient(StubClient):
        def embed(self, texts: list[str], **kwargs: Any) -> StubResponse:
            return StubResponse([[0.1] * 256 for _ in texts], total_tokens=1)

    provider = VoyageEmbeddingProvider(CeilingConfig(), client=WrongWidthClient())
    with pytest.raises(ValueError, match="returned dimensions"):
        provider.embed(["a"])


def test_missing_api_key_is_a_clear_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("VOYAGE_API_KEY", raising=False)
    pytest.importorskip("voyageai", reason="the ceiling extra is not installed")
    with pytest.raises(MissingApiKeyError, match="VOYAGE_API_KEY"):
        VoyageEmbeddingProvider(CeilingConfig())


def test_satisfies_the_embedding_provider_protocol() -> None:
    """Structural conformance, so the pipeline can take it unchanged."""
    from kb.providers.base import EmbeddingProvider

    provider, _ = _provider()
    checked: EmbeddingProvider = provider
    texts: Sequence[str] = ["a"]
    assert len(checked.embed(texts)) == 1
