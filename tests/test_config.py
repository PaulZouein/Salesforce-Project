from __future__ import annotations

import json

import pytest

from core.config import SAFE_DEFAULTS, load_settings


def write(tmp_path, content):
    (tmp_path / "sfti.config.json").write_text(content if isinstance(content, str) else json.dumps(content),
                                               encoding="utf-8")
    return load_settings(tmp_path)


def test_defaults_without_a_file(tmp_path):
    settings = load_settings(tmp_path)
    assert settings == SAFE_DEFAULTS
    assert (settings.mode, settings.allow_developer_edition_writes, settings.allow_apex_tests_on_production) == \
        ("advise", False, False)
    assert (settings.default_row_limit, settings.max_row_limit) == (50, 200)


@pytest.mark.parametrize("content", ["{not json", "", "[1, 2]", '"apply"', "null"])
def test_unreadable_file_falls_back_to_safe_defaults(tmp_path, content):
    settings = write(tmp_path, content)
    assert settings.mode == "advise" and not settings.allow_developer_edition_writes
    assert settings.warnings and "Safe defaults" in settings.warnings[0]


def test_valid_file(tmp_path):
    settings = write(tmp_path, {"mode": "apply", "allow_developer_edition_writes": True,
                                "allow_apex_tests_on_production": True, "default_row_limit": 10,
                                "max_row_limit": 500, "_readme": "ignored"})
    assert settings.mode == "apply" and settings.allow_developer_edition_writes
    assert settings.allow_apex_tests_on_production
    assert (settings.default_row_limit, settings.max_row_limit) == (10, 500)
    assert settings.warnings == ()


@pytest.mark.parametrize("value", ["true", "yes", 1, "apply", [True], {"a": 1}])
def test_switches_must_be_real_booleans(tmp_path, value):
    settings = write(tmp_path, {"allow_developer_edition_writes": value, "allow_apex_tests_on_production": value})
    assert settings.allow_developer_edition_writes is False
    assert settings.allow_apex_tests_on_production is False
    assert len(settings.warnings) == 2


@pytest.mark.parametrize("mode", ["APPLY", "write", "", None, True, 1, "production"])
def test_unknown_mode_is_advise(tmp_path, mode):
    settings = write(tmp_path, {"mode": mode})
    assert settings.mode == "advise"
    assert settings.warnings


def test_unknown_keys_are_reported_and_cannot_unlock_anything(tmp_path):
    settings = write(tmp_path, {"allow_production_writes": True, "force": True, "production_override": "yes"})
    assert settings == load_settings(tmp_path)
    assert len(settings.warnings) == 3
    assert not hasattr(settings, "allow_production_writes")
    assert set(settings.to_dict()) == {"mode", "allow_developer_edition_writes", "allow_apex_tests_on_production",
                                       "default_row_limit", "max_row_limit", "jira_base_url", "warnings"}


@pytest.mark.parametrize("raw,expected", [
    ({"max_row_limit": 0}, (50, 200)),
    ({"max_row_limit": 2001}, (50, 200)),
    ({"max_row_limit": "300"}, (50, 200)),
    ({"max_row_limit": True}, (50, 200)),
    ({"max_row_limit": 2000}, (50, 2000)),
    ({"default_row_limit": 0}, (50, 200)),
    ({"default_row_limit": 201}, (50, 200)),
    ({"default_row_limit": 2.5}, (50, 200)),
    ({"max_row_limit": 20}, (20, 20)),
    ({"max_row_limit": 20, "default_row_limit": 5}, (5, 20)),
])
def test_limits(tmp_path, raw, expected):
    settings = write(tmp_path, raw)
    assert (settings.default_row_limit, settings.max_row_limit) == expected
    assert settings.default_row_limit <= settings.max_row_limit


def test_settings_are_reread_on_every_call(inv, project):
    assert inv.get_config()["mode"] == "advise"
    project.write_config(mode="apply")
    config = inv.get_config()
    assert config["mode"] == "apply"
    assert "always refused" in config["production"]


def test_repository_default_is_advise():
    from pathlib import Path
    raw = json.loads((Path(__file__).resolve().parent.parent / "sfti.config.json").read_text())
    assert raw["mode"] == "advise"
    assert raw["allow_developer_edition_writes"] is False
    assert raw["allow_apex_tests_on_production"] is False


@pytest.mark.parametrize("value", ["http://jira.example.com", "jira.example.com", "https://user:pw@jira.example.com",
                                   "https://jira.example.com/?token=x", "ftp://x", 7, True, ["https://x"]])
def test_jira_url_must_be_plain_https_or_the_reader_stays_off(tmp_path, value):
    settings = write(tmp_path, {"jira_base_url": value})
    assert settings.jira_base_url == "" and len(settings.warnings) == 1


def test_jira_is_off_by_default_and_accepts_an_https_url(tmp_path):
    assert load_settings(tmp_path).jira_base_url == ""
    assert write(tmp_path, {"jira_base_url": "https://jira.example.com/jira/"}).jira_base_url == \
        "https://jira.example.com/jira"
