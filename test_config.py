"""Tests for load_config() error handling in both scripts."""
import copy

import pytest
import yaml

import recommendations
import watchlist_checker
from conftest import MINIMAL_CFG

MODULES = [watchlist_checker, recommendations]


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """Point both scripts at a temp dir holding a placeholder config.yaml.

    conftest stubs yaml.safe_load, so the file's text is irrelevant; each test
    sets what safe_load returns/raises.
    """
    (tmp_path / "config.yaml").write_text("placeholder: true\n", encoding="utf-8")
    for module in MODULES:
        monkeypatch.setattr(module, "_script_dir", str(tmp_path))
    return tmp_path


@pytest.mark.parametrize("module", MODULES)
def test_valid_config_loads(module, config_dir, monkeypatch):
    monkeypatch.setattr(yaml, "safe_load", lambda f: copy.deepcopy(MINIMAL_CFG))
    assert module.load_config()["location"] == "Test City"


@pytest.mark.parametrize("module", MODULES)
def test_missing_file_names_example_config(module, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_script_dir", str(tmp_path))
    with pytest.raises(FileNotFoundError, match="config.example.yaml"):
        module.load_config()


@pytest.mark.parametrize("module", MODULES)
def test_malformed_yaml_gives_friendly_error(module, config_dir, monkeypatch):
    def boom(f):
        raise yaml.YAMLError("mapping values are not allowed here")
    monkeypatch.setattr(yaml, "safe_load", boom)
    with pytest.raises(ValueError, match=r"(?s)not valid YAML.*mapping values.*config\.example\.yaml"):
        module.load_config()


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("empty", [None, "just a string", ["a", "list"]])
def test_non_mapping_config_is_rejected(module, config_dir, monkeypatch, empty):
    monkeypatch.setattr(yaml, "safe_load", lambda f: empty)
    with pytest.raises(ValueError, match="empty or not a mapping"):
        module.load_config()


@pytest.mark.parametrize("module", MODULES)
def test_missing_email_key_is_named(module, config_dir, monkeypatch):
    cfg = copy.deepcopy(MINIMAL_CFG)
    del cfg["email"]["from"]
    monkeypatch.setattr(yaml, "safe_load", lambda f: cfg)
    with pytest.raises(ValueError, match=r"email\.from.*config.example.yaml"):
        module.load_config()


@pytest.mark.parametrize("module", MODULES)
def test_missing_whole_section_is_named(module, config_dir, monkeypatch):
    cfg = copy.deepcopy(MINIMAL_CFG)
    del cfg["letterboxd"]
    monkeypatch.setattr(yaml, "safe_load", lambda f: cfg)
    with pytest.raises(ValueError, match=r"letterboxd\.username"):
        module.load_config()


@pytest.mark.parametrize("module", MODULES)
def test_incomplete_theatre_entry_is_named(module, config_dir, monkeypatch):
    cfg = copy.deepcopy(MINIMAL_CFG)
    cfg["theatres"] = [{"name": "A", "cinemaclock_url": "u", "homepage": "h"}, {"name": "B"}]
    monkeypatch.setattr(yaml, "safe_load", lambda f: cfg)
    with pytest.raises(ValueError) as exc:
        module.load_config()
    assert "theatres[2].cinemaclock_url" in str(exc.value)
    assert "theatres[2].homepage" in str(exc.value)
    assert "theatres[1]" not in str(exc.value)


@pytest.mark.parametrize("module", MODULES)
def test_empty_theatre_list_is_allowed(module, config_dir, monkeypatch):
    monkeypatch.setattr(yaml, "safe_load", lambda f: copy.deepcopy(MINIMAL_CFG))
    assert module.load_config()["theatres"] == []


@pytest.mark.parametrize("module", MODULES)
def test_theatres_must_be_a_list(module, config_dir, monkeypatch):
    cfg = copy.deepcopy(MINIMAL_CFG)
    cfg["theatres"] = {"name": "A"}
    monkeypatch.setattr(yaml, "safe_load", lambda f: cfg)
    with pytest.raises(ValueError, match="must be a list"):
        module.load_config()
