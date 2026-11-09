"""Config is validated, not trusted: TOML hands back arbitrary types."""

from pathlib import Path

import pytest

from kb.config import Config, ConfigError


def test_defaults_match_the_pinned_model(tmp_path: Path):
    config = Config.load(tmp_path / "absent.toml")
    assert config.embedding.model == "Alibaba-NLP/gte-modernbert-base"
    assert config.embedding.dimension == 768
    assert config.embedding.revision == "e7f32e3c00f91d699e8c43b53106206bcc72bb22"


def test_file_values_override_defaults(tmp_path: Path):
    path = tmp_path / "kb.toml"
    path.write_text('dataset = "other"\n\n[chunk]\nmax_chars = 999\n')
    config = Config.load(path)
    assert config.dataset == "other"
    assert config.chunk.max_chars == 999
    assert config.chunk.target_chars == 1500  # untouched default


def test_wrong_type_is_rejected_at_load(tmp_path: Path):
    """Better here than at a database insert several minutes into a run."""
    path = tmp_path / "kb.toml"
    path.write_text('[embedding]\ndimension = "768"\n')
    with pytest.raises(ConfigError, match="dimension"):
        Config.load(path)


def test_bool_is_not_accepted_as_an_int(tmp_path: Path):
    """bool subclasses int, so `dimension = true` would otherwise become 1."""
    path = tmp_path / "kb.toml"
    path.write_text("[embedding]\ndimension = true\n")
    with pytest.raises(ConfigError):
        Config.load(path)


def test_non_table_section_is_rejected(tmp_path: Path):
    path = tmp_path / "kb.toml"
    path.write_text('chunk = "nope"\n')
    with pytest.raises(ConfigError, match="chunk"):
        Config.load(path)


def test_database_url_comes_from_the_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """It carries a credential, so the file must never win."""
    path = tmp_path / "kb.toml"
    path.write_text('database_url = "postgresql://file/db"\n')
    monkeypatch.setenv("DATABASE_URL", "postgresql://env/db")
    assert Config.load(path).database_url == "postgresql://env/db"
