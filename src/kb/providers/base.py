"""Provider interfaces: the seam that makes a model swap a config change.

ADR-0003 and ADR-0006. No provider SDK call appears outside this package --
that boundary is the only reason the Milestone 7 comparison is configuration
rather than a rewrite.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Literal, Protocol

EmbeddingPurpose = Literal["document", "query"]
"""Whether text is being indexed or used to search.

Not cosmetic, and not symmetric across providers. Voyage prepends a different
instruction for each ("Represent the query for retrieving supporting
documents: " versus "Represent the document for retrieval: "), so embedding a
query as a document measurably degrades retrieval. Models without that
convention ignore it -- gte-modernbert-base has an empty `prompts` map and
takes raw text on both sides.

The distinction exists on the interface so the ceiling comparison (ADR-0006)
is fair. A ceiling run that embedded queries as documents would understate the
hosted model and flatter the local one, which is the opposite of the point.
"""


class EmbeddingProvider(Protocol):
    """Turns text into vectors.

    `model_name` is not cosmetic: callers persist it onto every chunk, because
    vectors from two different models cannot be compared and nothing raises
    when you try (ADR-0006).
    """

    @property
    def model_name(self) -> str: ...

    @property
    def dimension(self) -> int: ...

    def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbeddingPurpose = "document",
    ) -> list[list[float]]: ...
