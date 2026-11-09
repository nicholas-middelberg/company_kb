"""Configuration. Every knob that could move an eval score lives here.

Nothing elsewhere in the codebase reads a model name or chunk size from a
literal: a named config is the unit the eval harness compares, so a value
hard-coded in a module is a value that cannot be swept.

Values from kb.toml are validated rather than trusted. TOML gives back
arbitrary types, and a string where an int belongs would otherwise travel all
the way to a database insert before failing.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

DEFAULT_DATABASE_URL = "postgresql://kb:kb_local_dev@localhost:5432/company_kb"


class ConfigError(ValueError):
    """kb.toml contains a value of the wrong type."""


@dataclass(frozen=True)
class ChunkConfig:
    """Starting values only. ADR-0019 settles these by measurement."""

    target_chars: int = 1500
    overlap_chars: int = 200
    max_chars: int = 3000


@dataclass(frozen=True)
class EmbeddingConfig:
    """The model and its pinned revision (ADR-0022).

    `dimension` is checked against both the loaded model and the database
    column; it is recorded rather than trusted (ADR-0006).
    """

    model: str = "Alibaba-NLP/gte-modernbert-base"
    revision: str = "e7f32e3c00f91d699e8c43b53106206bcc72bb22"
    dimension: int = 768
    batch_size: int = 32


DEFAULT_EXCLUDE_DIRS = (
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "site-packages",
    ".ruff_cache",
    ".pytest_cache",
    ".mypy_cache",
    ".tox",
    "build",
    "dist",
    "pgdata",
)
"""Directories discovery never descends into.

Not optional hygiene. Pointing `kb ingest` at a repository root walks straight
into the virtualenv: this project's own .venv holds 11,867 Python files from
torch and transformers against 17 of our own, and embedding them is both
expensive and worse than useless -- the corpus would answer questions about
PyTorch internals while claiming to be about the target subsystem.
"""


@dataclass(frozen=True)
class CeilingConfig:
    """The hosted reference run that establishes the quality ceiling (ADR-0006).

    Not the serving path. Its vectors are stored separately because its
    dimension differs from the local model's, and a column holds one dimension.

    `output_dimension` is sent explicitly rather than relying on the model
    default, so the number this provider reports cannot drift from the number
    it actually produces.
    """

    model: str = "voyage-3-large"
    output_dimension: int = 1024
    batch_size: int = 128
    max_retries: int = 3


@dataclass(frozen=True)
class Config:
    dataset: str = "airflow-scheduler"
    database_url: str = DEFAULT_DATABASE_URL
    chunk: ChunkConfig = field(default_factory=ChunkConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    exclude_dirs: tuple[str, ...] = DEFAULT_EXCLUDE_DIRS
    ceiling: CeilingConfig = field(default_factory=CeilingConfig)

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """Defaults, overlaid with kb.toml if present, then DATABASE_URL.

        Environment wins for the database URL because it differs per
        environment and carries a credential, which must never sit in a
        committed file.
        """
        raw: dict[str, Any] = {}
        if path is not None and path.exists():
            raw = tomllib.loads(path.read_text())

        chunk_raw = _table(raw, "chunk")
        embed_raw = _table(raw, "embedding")
        ceiling_raw = _table(raw, "ceiling")
        chunk_defaults = ChunkConfig()
        embed_defaults = EmbeddingConfig()
        ceiling_defaults = CeilingConfig()

        return cls(
            dataset=_as_str(raw, "dataset", cls.dataset),
            database_url=os.environ.get(
                "DATABASE_URL", _as_str(raw, "database_url", DEFAULT_DATABASE_URL)
            ),
            chunk=ChunkConfig(
                target_chars=_as_int(chunk_raw, "target_chars", chunk_defaults.target_chars),
                overlap_chars=_as_int(chunk_raw, "overlap_chars", chunk_defaults.overlap_chars),
                max_chars=_as_int(chunk_raw, "max_chars", chunk_defaults.max_chars),
            ),
            exclude_dirs=_as_str_tuple(raw, "exclude_dirs", DEFAULT_EXCLUDE_DIRS),
            ceiling=CeilingConfig(
                model=_as_str(ceiling_raw, "model", ceiling_defaults.model),
                output_dimension=_as_int(
                    ceiling_raw, "output_dimension", ceiling_defaults.output_dimension
                ),
                batch_size=_as_int(ceiling_raw, "batch_size", ceiling_defaults.batch_size),
                max_retries=_as_int(ceiling_raw, "max_retries", ceiling_defaults.max_retries),
            ),
            embedding=EmbeddingConfig(
                model=_as_str(embed_raw, "model", embed_defaults.model),
                revision=_as_str(embed_raw, "revision", embed_defaults.revision),
                dimension=_as_int(embed_raw, "dimension", embed_defaults.dimension),
                batch_size=_as_int(embed_raw, "batch_size", embed_defaults.batch_size),
            ),
        )


def _table(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value: Any = raw.get(name, {})
    if not isinstance(value, dict):
        msg = f"config section [{name}] must be a table, got {type(value).__name__}"
        raise ConfigError(msg)
    # isinstance narrows Any to dict[Unknown, Unknown]; the keys are TOML
    # table names, which are always strings.
    return cast("dict[str, Any]", value)


def _as_str(raw: dict[str, Any], key: str, default: str) -> str:
    value: Any = raw.get(key, default)
    if not isinstance(value, str):
        msg = f"config key {key!r} must be a string, got {type(value).__name__}"
        raise ConfigError(msg)
    return value


def _as_int(raw: dict[str, Any], key: str, default: int) -> int:
    value: Any = raw.get(key, default)
    # bool is an int subclass, and `dimension = true` should not silently
    # become 1.
    if not isinstance(value, int) or isinstance(value, bool):
        msg = f"config key {key!r} must be an integer, got {type(value).__name__}"
        raise ConfigError(msg)
    return value


def _as_str_tuple(raw: dict[str, Any], key: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value: Any = raw.get(key)
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):  # pyright: ignore[reportUnknownVariableType]
        msg = f"config key {key!r} must be a list of strings"
        raise ConfigError(msg)
    return tuple(cast("list[str]", value))
