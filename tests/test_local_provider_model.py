"""The real embedding model, as opposed to the fake used everywhere else.

Marked `model` and skipped unless the pinned weights are already cached, so
`pytest` stays fast and offline by default. These exist because the fake
provider cannot catch a drift in the real one's contract -- the dimension it
actually emits, or whether its vectors are normalised.

Run with: uv run pytest -m model
"""

import math

import pytest

from kb.config import EmbeddingConfig
from kb.providers.local_embeddings import DimensionMismatchError, LocalEmbeddingProvider

pytestmark = pytest.mark.model


@pytest.fixture(scope="module")
def provider() -> LocalEmbeddingProvider:
    try:
        return LocalEmbeddingProvider()
    except Exception as error:
        pytest.skip(f"pinned weights not cached; run `kb model-warm` ({error})")


def test_emits_the_dimension_the_schema_was_built_for(
    provider: LocalEmbeddingProvider,
) -> None:
    """The fake asserts 768 because it is told to; this asks the model."""
    assert provider.dimension == 768
    assert len(provider.embed(["hello"])[0]) == 768


def test_vectors_are_unit_norm(provider: LocalEmbeddingProvider) -> None:
    """Pins the property the fake mirrors.

    The tolerance is 1e-3 rather than 1e-6 on purpose: the weights are
    float16, so norms land near 0.9999 rather than exactly 1.0. Anything that
    later swaps pgvector's cosine operator for inner product would be relying
    on this, and on that precision.
    """
    for vector in provider.embed(["hello world", "a second sentence"]):
        norm = math.sqrt(sum(value * value for value in vector))
        assert math.isclose(norm, 1.0, abs_tol=1e-3)


def test_purpose_is_ignored_by_this_model(
    provider: LocalEmbeddingProvider,
) -> None:
    """Documented behaviour: an empty `prompts` map means raw text both sides.

    If a future model needs prefixes, this test fails and the provider has to
    start honouring `purpose` rather than silently discarding it.
    """
    as_document = provider.embed(["scheduler locking"], purpose="document")[0]
    as_query = provider.embed(["scheduler locking"], purpose="query")[0]
    assert as_document == as_query


def test_distinct_texts_get_distinct_vectors(
    provider: LocalEmbeddingProvider,
) -> None:
    """A smoke test against a silently broken pooling or truncation path."""
    first, second = provider.embed(["row level locking", "banana bread recipe"])
    cosine = sum(a * b for a, b in zip(first, second, strict=True))
    assert cosine < 0.95


def test_a_configured_dimension_that_disagrees_is_refused_at_load() -> None:
    """Fail on construction, not several minutes into an ingestion run."""
    with pytest.raises(DimensionMismatchError, match="768"):
        LocalEmbeddingProvider(EmbeddingConfig(dimension=1024))


def test_an_uncached_revision_is_not_fetched() -> None:
    """The offline default is the enforcement behind ADR-0022's rule 3.

    A revision that cannot be in the cache must fail rather than reach out.
    """
    bogus = EmbeddingConfig(revision="0" * 40)
    with pytest.raises(Exception, match=r"(?i)offline|local|not found|connect|revision"):
        LocalEmbeddingProvider(bogus)
