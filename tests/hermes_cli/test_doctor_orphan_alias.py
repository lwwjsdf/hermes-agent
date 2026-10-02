from types import SimpleNamespace
from unittest.mock import patch

import pytest


@pytest.fixture()
def profile_env(tmp_path, monkeypatch):
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    default_home = tmp_path / ".hermes"
    default_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HERMES_HOME", str(default_home))
    return tmp_path


from hermes_cli.doctor_state import _check_profiles
from hermes_cli.profiles import create_profile, create_wrapper_script


def test_orphan_alias_recommends_working_remove_command(profile_env, monkeypatch, capsys):
    monkeypatch.setattr("hermes_cli.profiles.shutil.which", lambda name: "/opt/hermes/bin/hermes")
    create_profile("coder", no_alias=True)
    create_wrapper_script("gone")
    create_wrapper_script("custom", target="gone")

    _check_profiles(False)

    output = capsys.readouterr().out
    assert "Orphan alias: gone → profile 'gone' no longer exists" in output
    assert "hermes profile alias gone --remove" in output
    assert "Orphan alias: custom → profile 'gone' no longer exists" in output
    assert "hermes profile alias gone --name custom --remove" in output
