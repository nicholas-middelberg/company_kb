"""Provider interfaces: the seam that makes a model swap a config change.

ADR-0003 and ADR-0006. No provider SDK call appears outside this package --
that boundary is the only reason the Milestone 7 comparison is configuration
rather than a rewrite.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol


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

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...
