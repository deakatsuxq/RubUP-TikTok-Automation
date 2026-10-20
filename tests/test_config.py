from dataclasses import replace
from pathlib import Path
import pytest
from config import Config, load_config, save_config, DEFAULT_HASHTAGS


def test_yaml_roundtrip_relative_paths_and_disabled_hashtags(tmp_path):
    config = Config(disabled_hashtags=["#CS2"], keywords=["скины"])
    path = tmp_path / "config.yaml"
    save_config(config, path)
    loaded = load_config(path)
    assert "cs2" not in loaded.enabled_hashtags
    assert "rubup" in loaded.enabled_hashtags
    assert Path(loaded.db_path) == tmp_path / "data/history.sqlite3"
    save_config(loaded, path)
    assert "db_path: data/history.sqlite3" in path.read_text()
    assert loaded.keywords == ["скины"]


@pytest.mark.parametrize("updates", [{"retries": -1}, {"image_order": [1, 1, 2, 3]}, {"timeout": float("nan")}, {"publication_mode": "automatic"}, {"dry_run": "false"}, {"min_date": "2026-02-30"}, {"input_price_per_million": -1}, {"account_id": ""}])
def test_reject_bad_configuration(updates):
    with pytest.raises(ValueError):
        replace(Config(), **updates).validate()


def test_secrets_and_unknown_keys_rejected(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text("OPENAI_API_KEY: should-not-be-here\n")
    with pytest.raises(ValueError, match="Неизвестные"):
        load_config(path)


def test_exact_default_tags_and_valid_project_config():
    assert "подаркитг" in DEFAULT_HASHTAGS
    assert len(DEFAULT_HASHTAGS) == 27
    config = load_config(Path(__file__).resolve().parents[1] / "config.yaml")
    assert config.min_date == "2026-05-01"
    assert config.dry_run is False
    assert "подаркитг" in config.enabled_hashtags
