"""Local embeddings via sentence-transformers (ADR-0031).

ADR-0022's four loading rules are expressed here as constructor arguments
rather than as discipline, which is the reason that loader was chosen:

1. `trust_remote_code` is never passed -- its default is False, so the rule is
   an absence rather than something to remember to switch off. This repository
   ships no .py files and its config has no auto_map, so there is nothing for
   the flag to load even if it were set.
2. `use_safetensors=True` is explicit, because the repository also ships a
   pytorch_model.bin pickle; being explicit makes a missing safetensors file
   fail loudly rather than silently falling back to the pickle.
3. The revision is pinned, and downloads are refused unless explicitly warmed.
4. The licence was confirmed on this model's own card (apache-2.0).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from kb.config import EmbeddingConfig
from kb.providers.base import EmbeddingPurpose

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer


class DimensionMismatchError(RuntimeError):
    """The loaded model disagrees with the configured dimension.

    Raised at load time rather than at INSERT. A silent mismatch is the
    failure ADR-0006 exists to prevent, and the vector column would reject it
    anyway -- but at that point a whole ingestion run has already been spent.
    """


class LocalEmbeddingProvider:
    """sentence-transformers, pinned and offline by default."""

    def __init__(
        self,
        config: EmbeddingConfig | None = None,
        *,
        allow_download: bool = False,
    ) -> None:
        from sentence_transformers import SentenceTransformer

        self._config = config or EmbeddingConfig()
        # Note the absence of trust_remote_code -- see rule 1 above.
        self._model: SentenceTransformer = SentenceTransformer(
            self._config.model,
            revision=self._config.revision,
            model_kwargs={"use_safetensors": True},
            local_files_only=not allow_download,
        )
        loaded = self._model.get_embedding_dimension()
        if loaded != self._config.dimension:
            msg = (
                f"{self._config.model} emits {loaded} dimensions but the "
                f"configuration says {self._config.dimension}. The database "
                f"column is fixed-width, so this must be reconciled before "
                f"ingesting (ADR-0006)."
            )
            raise DimensionMismatchError(msg)

    @property
    def model_name(self) -> str:
        return self._config.model

    @property
    def dimension(self) -> int:
        return self._config.dimension

    def embed(
        self,
        texts: Sequence[str],
        *,
        purpose: EmbeddingPurpose = "document",
    ) -> list[list[float]]:
        """Embed a batch. Normalised, so cosine distance behaves.

        `purpose` is accepted and ignored: this model's
        config_sentence_transformers.json carries an empty `prompts` map and a
        null default, so it takes raw text for queries and documents alike.
        Applying a prefix anyway would shift every vector away from what the
        model was trained to produce.
        """
        del purpose
        if not texts:
            return []
        # sentence-transformers' encode() carries overloads built from
        # untyped torch/transformers types, so pyright cannot resolve the
        # member. Suppressed here, narrowly, rather than by relaxing strict
        # mode globally (ADR-0027: a gate relaxed once stays relaxed).
        vectors: Any = self._model.encode(  # pyright: ignore[reportUnknownMemberType]
            list(texts),
            batch_size=self._config.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return [[float(value) for value in row] for row in vectors]
