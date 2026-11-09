"""Voyage embeddings, used only to establish the quality ceiling (ADR-0006).

This is a measurement, not a serving path. It exists so that a weak `recall@k`
can be attributed: without a known upper bound, a poor score is equally
explained by the local embedding model or by the chunk parameters (ADR-0019),
and there is no way to tell which.

`voyageai` is an optional dependency for exactly that reason -- a serving
install should not carry a client it never calls. Install it with
`uv sync --extra ceiling`.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from typing import Any, Protocol, cast, runtime_checkable

from kb.config import CeilingConfig
from kb.providers.base import EmbeddingPurpose

# Voyage accepts at most 1000 texts per request. Our configured batch is well
# below it, but a caller could raise the config, so the cap is enforced here
# rather than trusted.
MAX_BATCH = 1000


class MissingApiKeyError(RuntimeError):
    """No Voyage credential available."""


class VoyageUnavailableError(RuntimeError):
    """The optional voyageai dependency is not installed."""


@runtime_checkable
class VoyageClientLike(Protocol):
    """The sliver of the Voyage client this provider uses.

    Declared so tests can inject a stub and run without the package or a
    network call -- which is what keeps these tests runnable in CI, where the
    `ceiling` extra is not installed.
    """

    def embed(
        self,
        texts: list[str],
        *,
        model: str,
        input_type: str | None = None,
        output_dimension: int | None = None,
        truncation: bool = True,
    ) -> Any: ...


class VoyageEmbeddingProvider:
    """Hosted embeddings for the ceiling run."""

    def __init__(
        self,
        config: CeilingConfig | None = None,
        *,
        client: VoyageClientLike | None = None,
        api_key: str | None = None,
    ) -> None:
        self._config = config or CeilingConfig()
        if self._config.batch_size > MAX_BATCH:
            msg = (
                f"ceiling batch_size {self._config.batch_size} exceeds Voyage's "
                f"limit of {MAX_BATCH} texts per request"
            )
            raise ValueError(msg)
        self._client = (
            client if client is not None else _build_client(api_key, self._config.max_retries)
        )
        self.total_tokens = 0
        """Accumulated billed tokens. Voyage bills per token, so this -- not
        the number of requests -- is what the run actually costs."""

    @property
    def model_name(self) -> str:
        return self._config.model

    @property
    def dimension(self) -> int:
        return self._config.output_dimension

    def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbeddingPurpose = "document",
    ) -> list[list[float]]:
        """Embed a batch, mapping `purpose` onto Voyage's `input_type`.

        This mapping is the whole reason `purpose` exists on the interface.
        Voyage prepends a different instruction for queries and documents, so
        embedding a corpus as queries would quietly degrade every search
        against it -- and in a ceiling run, understate the hosted model.
        """
        if not texts:
            return []
        if len(texts) > MAX_BATCH:
            msg = f"{len(texts)} texts exceeds Voyage's per-request limit of {MAX_BATCH}"
            raise ValueError(msg)

        response: Any = self._client.embed(
            list(texts),
            model=self._config.model,
            input_type=purpose,
            output_dimension=self._config.output_dimension,
            truncation=True,
        )
        vectors: list[list[float]] = [
            [float(value) for value in row] for row in response.embeddings
        ]
        self.total_tokens += int(getattr(response, "total_tokens", 0) or 0)

        if any(len(vector) != self.dimension for vector in vectors):
            actual = {len(vector) for vector in vectors}
            msg = (
                f"{self._config.model} returned dimensions {sorted(actual)} but "
                f"{self.dimension} was requested"
            )
            raise ValueError(msg)
        return vectors


def _build_client(api_key: str | None, max_retries: int) -> VoyageClientLike:
    # Imported from voyageai.client rather than the package root: the package
    # ships py.typed but does not re-export Client, so `voyageai.Client` is a
    # private-import usage that pyright rejects while the submodule path is
    # fully typed. The remaining suppression covers the case where the optional
    # extra is simply not installed -- the common state for a serving install.
    try:
        from voyageai.client import Client  # pyright: ignore[reportMissingImports]
    except ImportError as error:  # pragma: no cover - depends on the extra
        msg = (
            "voyageai is not installed. It is an optional dependency because "
            "the ceiling run is a measurement, not the serving path: "
            "uv sync --extra ceiling"
        )
        raise VoyageUnavailableError(msg) from error

    key = api_key or os.environ.get("VOYAGE_API_KEY")
    if not key:
        msg = "VOYAGE_API_KEY is not set; it is needed only for the ceiling run"
        raise MissingApiKeyError(msg)
    # cast at the boundary: the rest of the module types against
    # VoyageClientLike, so nothing downstream depends on the SDK's shape.
    return cast("VoyageClientLike", Client(api_key=key, max_retries=max_retries))
