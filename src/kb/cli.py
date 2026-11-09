"""The kb command line.

Milestone 1 surface only: ingest, status, and a one-off model warm-up.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from kb.adapters.base import Adapter
from kb.adapters.code import CodeAdapter
from kb.adapters.doc import DocAdapter
from kb.config import Config
from kb.db import queries
from kb.db.connection import connect
from kb.ingest.pipeline import ingest

DEFAULT_CONFIG_PATH = Path("kb.toml")


def _adapters(config: Config) -> list[Adapter]:
    """Adapters active in Milestone 1.

    email and ticket are specified but not implemented; they need mbox and API
    parsing respectively, and the pipeline shape is already proven by two.
    """
    return [
        CodeAdapter(config.chunk, config.exclude_dirs),
        DocAdapter(config.chunk, config.exclude_dirs),
    ]


def _resolve_config(args: argparse.Namespace) -> Config:
    """Config, with --dataset overriding the file (SPECS command surface)."""
    config = Config.load(DEFAULT_CONFIG_PATH)
    dataset: str | None = getattr(args, "dataset", None)
    return replace(config, dataset=dataset) if dataset else config


def _cmd_ingest(args: argparse.Namespace) -> int:
    from kb.providers.local_embeddings import LocalEmbeddingProvider

    config = _resolve_config(args)
    root = Path(args.root).resolve()
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    provider = LocalEmbeddingProvider(config.embedding)
    print(f"dataset {config.dataset}  model {provider.model_name}")
    with connect(config.database_url) as conn:
        report = ingest(conn, config, _adapters(config), root, provider)
    print(report.render())
    return 1 if report.failed else 0


def _cmd_status(args: argparse.Namespace) -> int:
    config = _resolve_config(args)
    with connect(config.database_url) as conn:
        counts = queries.status_counts(conn, config.dataset)
        chunks = queries.chunk_count(conn, config.dataset)
        models = queries.embedding_models(conn, config.dataset)

    print(f"dataset {config.dataset}")
    for status in ("queued", "processing", "indexed", "failed"):
        print(f"  {status:<11} {counts.get(status, 0)}")
    print(f"  chunks      {chunks}")
    print(f"  models      {', '.join(models) if models else '-'}")
    if len(models) > 1:
        # Not fatal, but retrieval must filter on embedding_model or it will
        # compare vectors from different models and return nonsense (ADR-0006).
        print("  WARNING: more than one embedding model present")
    return 0


def _cmd_model_warm(_: argparse.Namespace) -> int:
    """Download the pinned model once, so ingestion can stay offline.

    Separate command because ADR-0022 requires the serving path to refuse
    downloads. Warming is the explicit, auditable exception rather than a
    silent fetch on first use.
    """
    from kb.providers.local_embeddings import LocalEmbeddingProvider

    config = Config.load(DEFAULT_CONFIG_PATH)
    print(f"fetching {config.embedding.model} at {config.embedding.revision[:12]}")
    provider = LocalEmbeddingProvider(config.embedding, allow_download=True)
    print(f"  ready: {provider.model_name}, {provider.dimension} dimensions")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kb", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="run the ingestion pipeline")
    p_ingest.add_argument("root", help="directory to ingest")
    p_ingest.add_argument("--dataset", help="override the configured dataset")
    p_ingest.set_defaults(func=_cmd_ingest)

    p_status = sub.add_parser("status", help="document counts by status")
    p_status.add_argument("--dataset", help="override the configured dataset")
    p_status.set_defaults(func=_cmd_status)
    sub.add_parser("model-warm", help="download the pinned embedding model once").set_defaults(
        func=_cmd_model_warm
    )

    args = parser.parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
