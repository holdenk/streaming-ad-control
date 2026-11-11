"""Tests for the guided setup and its follow-on scripts."""

import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml

from stream_ad_monitor.config import Config


ROOT = Path(__file__).resolve().parent.parent


def _load_script(name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


setup = _load_script("setup_wizard", "scripts/setup.py")
gate = _load_script("setup_test_title_gate", "scripts/obs_title_gate.py")
bootstrap = _load_script("reddit_bootstrap", "scripts/bootstrap_reddit_session.py")
announce_check = _load_script("announce_check", "scripts/test_announce.py")


def test_render_env_round_trips_shell_special_values(tmp_path):
    expected = {
        "TRIGGER_KEYWORD": "Fight Health Insurance $HOME 'quoted'",
        "META_ACCESS_TOKEN": "token with spaces \\ and $dollars",
        "TWITCH_CHANNEL_LOGIN": "holden",
    }
    env_path = tmp_path / "generated.env"
    env_path.write_text(setup.render_env(expected))

    with patch.dict(os.environ, {}, clear=True):
        assert gate._load_env_file(str(env_path)) == len(expected)
        for key, value in expected.items():
            assert os.environ[key] == value


def test_rules_yaml_escapes_names_and_keywords(tmp_path, monkeypatch):
    answers = iter([
        str(tmp_path / "rules.yaml"),
        'Topic "quoted"',
        'Fight "Health" Insurance',
        "exclude $HOME",
        "",
    ])
    monkeypatch.setattr(setup, "ask", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr(setup, "_ask_ids", lambda *args, **kwargs: ["123456"])
    env = {}
    state = {}

    setup._collect_rules_yaml(["meta"], env, state)

    loaded = yaml.safe_load(state["rules_file_content"])
    rule = loaded["rules"][0]
    assert rule["name"] == 'Topic "quoted"'
    assert rule["keywords"] == ['Fight "Health" Insurance']
    assert rule["exclude_keywords"] == ["exclude $HOME"]
    assert rule["meta_campaign_ids"] == ["123456"]
    assert env["RULES_FILE"] == str(tmp_path / "rules.yaml")


def test_wizard_obs_only_generates_loadable_config(tmp_path, monkeypatch, capsys):
    token = "meta token $HOME 'quoted' \\ path"
    keyword = 'Fight "Health" $HOME'
    answers = iter([
        "2",             # OBS integration
        "n", "n", "n", "n", "y",  # Meta only
        "holden",       # Twitch channel
        "n",             # no optional Helix credentials
        "n",             # single rule in env, not YAML
        keyword,
        "",              # no exclude keywords
        "123456789012345",
        "n",             # no announcements
        "",              # confirm write
        "",              # keep --env-file destination
        "n",             # do not install during this test
        "n",             # do not toggle the real campaign
    ])
    env_path = tmp_path / "setup.env"
    monkeypatch.setattr(setup, "_read", lambda prompt: next(answers))
    monkeypatch.setattr(setup, "getpass", lambda prompt: token)
    monkeypatch.setattr(sys, "argv", ["setup.py", "--env-file", str(env_path)])

    assert setup.main() == 0
    assert env_path.stat().st_mode & 0o777 == 0o600

    with patch.dict(os.environ, {}, clear=True):
        gate._load_env_file(str(env_path))
        config = Config(require_twitch=False)

    assert config.twitch_channel_login == "holden"
    assert config.meta_access_token == token
    assert config.rules[0].keywords == [keyword]
    assert config.rules[0].meta_campaign_ids == ["123456789012345"]
    output = capsys.readouterr().out
    assert "https://developers.facebook.com/apps/" in output
    assert "Tools → Scripts → +" in output
    assert str(env_path) in output


def test_failed_dependency_setup_is_reported_and_skips_live_checks(monkeypatch):
    run = MagicMock(return_value=1)
    monkeypatch.setattr(setup, "_run", run)
    monkeypatch.setattr(setup, "yes_no", lambda *args, **kwargs: True)

    _, failures = setup.offer_followups(
        {"META_API_TOKEN": "token"},
        {"verify_ids": {"meta": "123456"}},
        ["meta"],
    )

    assert failures == ["virtual environment creation"]
    assert run.call_count == 1


def _fake_bootstrap_client(restored: bool) -> MagicMock:
    client = MagicMock()
    client._restore_session.return_value = restored
    client._save_session.return_value = True
    client._is_logged_in.return_value = True
    client.ads_account_id = "acct123"
    return client


def test_bootstrap_prompts_for_credentials_after_session_expiry(tmp_path, monkeypatch):
    client = _fake_bootstrap_client(restored=False)
    client_cls = MagicMock(return_value=client)
    monkeypatch.setattr(bootstrap, "RedditAdClient", client_cls)
    monkeypatch.setattr("builtins.input", lambda prompt: "reddit_user")
    monkeypatch.setattr(bootstrap, "getpass", lambda prompt: "reddit_password")
    env = {"REDDIT_COOKIE_JAR": str(tmp_path / "reddit.json")}

    with patch.dict(os.environ, env, clear=True):
        assert bootstrap.main() == 0

    assert client_cls.call_args.kwargs["username"] == ""
    assert client.username == "reddit_user"
    assert client.password == "reddit_password"
    client._login_via_form.assert_called_once_with()
    client._save_session.assert_called_once_with()
    client.close.assert_called_once_with()


def test_bootstrap_reuses_live_session_without_prompting(tmp_path, monkeypatch):
    client = _fake_bootstrap_client(restored=True)
    monkeypatch.setattr(bootstrap, "RedditAdClient", MagicMock(return_value=client))
    monkeypatch.setattr(
        bootstrap,
        "getpass",
        lambda prompt: (_ for _ in ()).throw(AssertionError("must not prompt")),
    )
    env = {"REDDIT_COOKIE_JAR": str(tmp_path / "reddit.json")}

    with patch.dict(os.environ, env, clear=True):
        assert bootstrap.main() == 0

    client._login_via_form.assert_not_called()
    client._save_session.assert_called_once_with()
    client.close.assert_called_once_with()


def test_announcement_check_does_not_require_twitch_api_credentials(monkeypatch):
    config_cls = MagicMock()
    monkeypatch.setattr(announce_check, "Config", config_cls)
    monkeypatch.setattr(announce_check, "build_announcer", MagicMock(return_value=None))
    monkeypatch.setattr(sys, "argv", ["test_announce.py"])

    assert announce_check.main() == 2
    config_cls.assert_called_once_with(require_twitch=False)
