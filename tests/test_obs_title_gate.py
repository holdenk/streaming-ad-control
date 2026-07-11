"""Tests for scripts/obs_title_gate.py (the OBS event-driven ad gate)."""

import importlib.util
import os
from unittest.mock import MagicMock, patch

import pytest

# The gate is a CLI script under scripts/, not a package module. Load it by path.
_GATE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "scripts",
    "obs_title_gate.py",
)
_spec = importlib.util.spec_from_file_location("obs_title_gate", _GATE_PATH)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)


# ---------------------------------------------------------------------------
# _load_env_file
# ---------------------------------------------------------------------------


def test_load_env_file_sets_variables(tmp_path):
    env_file = tmp_path / "gate.env"
    env_file.write_text(
        "# a comment\n"
        "\n"
        "REDDIT_CAMPAIGN_ID=camp_1\n"
        "export TRIGGER_KEYWORD=Spark\n"
        'REDDIT_COOKIE_JAR="/tmp/jar.json"\n'
    )
    with patch.dict(os.environ, {}, clear=True):
        count = gate._load_env_file(str(env_file))
        assert os.environ["REDDIT_CAMPAIGN_ID"] == "camp_1"
        assert os.environ["TRIGGER_KEYWORD"] == "Spark"  # 'export ' stripped
        assert os.environ["REDDIT_COOKIE_JAR"] == "/tmp/jar.json"  # quotes stripped
    assert count == 3


def test_load_env_file_does_not_override_existing(tmp_path):
    env_file = tmp_path / "gate.env"
    env_file.write_text("LOG_LEVEL=DEBUG\n")
    with patch.dict(os.environ, {"LOG_LEVEL": "INFO"}, clear=True):
        gate._load_env_file(str(env_file))
        assert os.environ["LOG_LEVEL"] == "INFO"  # real env wins


def test_load_env_file_skips_malformed_lines(tmp_path):
    env_file = tmp_path / "gate.env"
    env_file.write_text("this_has_no_equals\nGOOD=yes\n")
    with patch.dict(os.environ, {}, clear=True):
        count = gate._load_env_file(str(env_file))
        assert os.environ["GOOD"] == "yes"
        assert "this_has_no_equals" not in os.environ
    assert count == 1


# ---------------------------------------------------------------------------
# _wait_for_live_stream
# ---------------------------------------------------------------------------


def test_wait_returns_stream_when_live_on_first_poll():
    twitch = MagicMock()
    live = {"title": "Spark stream"}
    twitch.get_stream.return_value = live
    sleep = MagicMock()

    result = gate._wait_for_live_stream(
        twitch, "chan", timeout=45, poll_interval=3, sleep=sleep, now=lambda: 0.0
    )

    assert result is live
    sleep.assert_not_called()


def test_wait_polls_until_live():
    twitch = MagicMock()
    live = {"title": "Spark stream"}
    twitch.get_stream.side_effect = [None, None, live]
    sleep = MagicMock()

    # now() stays at 0, so the deadline (0 + 45) is never reached; the loop
    # exits only when the stream becomes live.
    result = gate._wait_for_live_stream(
        twitch, "chan", timeout=45, poll_interval=3, sleep=sleep, now=lambda: 0.0
    )

    assert result is live
    assert twitch.get_stream.call_count == 3
    assert sleep.call_count == 2


def test_wait_times_out_and_returns_none():
    twitch = MagicMock()
    twitch.get_stream.return_value = None
    sleep = MagicMock()
    # now() sequence: deadline read (0) → 0+45=45; then 0 (< 45, poll once) → 50 (>= 45, give up).
    times = iter([0.0, 0.0, 50.0])

    result = gate._wait_for_live_stream(
        twitch, "chan", timeout=45, poll_interval=3, sleep=sleep, now=lambda: next(times)
    )

    assert result is None
    assert sleep.call_count == 1


def test_wait_swallows_transient_errors():
    twitch = MagicMock()
    live = {"title": "Spark stream"}
    twitch.get_stream.side_effect = [RuntimeError("blip"), live]
    sleep = MagicMock()

    result = gate._wait_for_live_stream(
        twitch, "chan", timeout=45, poll_interval=3, sleep=sleep, now=lambda: 0.0
    )

    assert result is live
    assert sleep.call_count == 1


# ---------------------------------------------------------------------------
# main() — event dispatch (Config + StreamAdMonitor mocked out)
# ---------------------------------------------------------------------------


def _patch_gate(monitor):
    """Patch Config + StreamAdMonitor in the gate module to return mocks.

    Returns (context_manager, config_cls_mock) — the class mock reference stays
    valid after the context exits, so require_twitch assertions can run outside.
    """
    config = MagicMock()
    config.twitch_channel_login = "chan"
    config_cls = MagicMock(return_value=config)
    monitor_cls = MagicMock(return_value=monitor)
    ctx = patch.multiple(gate, Config=config_cls, StreamAdMonitor=monitor_cls)
    return ctx, config_cls


def test_main_stopped_pauses_everything_without_twitch(tmp_path):
    monitor = MagicMock()
    ctx, config_cls = _patch_gate(monitor)
    with ctx:
        rc = gate.main(["stopped", "--pidfile", str(tmp_path / "g.pid")])

    assert rc == 0
    monitor.apply.assert_called_once_with(title="", live=False)
    monitor.close.assert_called_once_with()
    # 'stopped' never needs Twitch.
    config_cls.assert_called_once_with(require_twitch=False)


def test_main_started_with_explicit_title_skips_twitch():
    monitor = MagicMock()
    ctx, config_cls = _patch_gate(monitor)
    with ctx:
        rc = gate.main(["started", "--title", "Apache Spark deep dive"])

    assert rc == 0
    monitor.apply.assert_called_once_with(title="Apache Spark deep dive", live=True)
    config_cls.assert_called_once_with(require_twitch=False)


def test_main_started_public_source_needs_no_creds():
    monitor = MagicMock()
    monitor.twitch.get_stream.return_value = {"title": "Live Spark coding"}
    ctx, config_cls = _patch_gate(monitor)
    with ctx:
        rc = gate.main(["started"])  # default source == public

    assert rc == 0
    monitor.apply.assert_called_once_with(title="Live Spark coding", live=True)
    config_cls.assert_called_once_with(require_twitch=False)  # public == no creds


def test_main_started_helix_source_requires_creds():
    monitor = MagicMock()
    monitor.twitch.get_stream.return_value = {"title": "Live Spark coding"}
    ctx, config_cls = _patch_gate(monitor)
    with ctx:
        rc = gate.main(["started", "--title-source", "helix"])

    assert rc == 0
    monitor.apply.assert_called_once_with(title="Live Spark coding", live=True)
    config_cls.assert_called_once_with(require_twitch=True)  # helix needs creds


def test_main_started_defaults_deny_when_twitch_never_live():
    monitor = MagicMock()
    monitor.twitch.get_stream.return_value = None  # never live
    ctx, _config_cls = _patch_gate(monitor)
    with ctx:
        # tiny timeout/poll so the wait resolves immediately
        rc = gate.main(["started", "--twitch-timeout", "0", "--twitch-poll", "0"])

    assert rc == 0
    monitor.apply.assert_called_once_with(title="", live=False)


def test_main_watch_with_explicit_title_is_rejected():
    # Nothing should be constructed; the conflict is caught before Config.
    rc = gate.main(["started", "--watch", "--title", "whatever"])
    assert rc == 2


def test_main_returns_2_on_bad_env_file(tmp_path):
    missing = tmp_path / "nope.env"
    rc = gate.main(["stopped", "--env-file", str(missing)])
    assert rc == 2


def test_main_returns_1_when_apply_raises(tmp_path):
    monitor = MagicMock()
    monitor.apply.side_effect = RuntimeError("toggle failed")
    ctx, _config_cls = _patch_gate(monitor)
    with ctx:
        rc = gate.main(["stopped", "--pidfile", str(tmp_path / "g.pid")])

    assert rc == 1
    monitor.close.assert_called_once_with()  # still cleaned up


def test_main_stopped_terminates_running_guard(tmp_path, monkeypatch):
    pidfile = tmp_path / "g.pid"
    pidfile.write_text("424242")  # a "running guard"
    killed = []
    monkeypatch.setattr(gate, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(gate.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    monitor = MagicMock()
    ctx, _config_cls = _patch_gate(monitor)
    with ctx:
        rc = gate.main(["stopped", "--pidfile", str(pidfile)])

    assert rc == 0
    assert (424242, gate.signal.SIGTERM) in killed
    assert not pidfile.exists()  # pidfile cleared
    monitor.apply.assert_called_once_with(title="", live=False)


# ---------------------------------------------------------------------------
# _run_guard — watch mode
# ---------------------------------------------------------------------------


def _guard_args(tmp_path, extra=None):
    argv = [
        "started", "--watch",
        "--watch-poll", "0",
        "--twitch-timeout", "0",
        "--twitch-poll", "0",
        "--offline-confirm", "2",
        "--pidfile", str(tmp_path / "g.pid"),
    ] + (extra or [])
    return gate._parse_args(argv)


def test_guard_enables_then_pauses_when_stream_ends(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.time, "sleep", lambda *_: None)
    monkeypatch.setattr(gate, "_install_guard_signal_handler", lambda: None)
    monitor = MagicMock()
    # wait consumes the first (live) read; then two offline reads confirm the end.
    monitor.twitch.get_stream.side_effect = [
        {"title": "Spark stream"},  # go-live (consumed by _wait_for_live_stream)
        None,                        # offline 1/2 -> tentative, keep enabled
        None,                        # offline 2/2 -> confirmed end
    ]
    args = _guard_args(tmp_path)

    rc = gate._run_guard(monitor, "chan", args)

    assert rc == 0
    # Enabled while live (twice, once per surviving poll), paused once at the end.
    enable_calls = [c for c in monitor.apply.call_args_list if c.kwargs.get("live") is True]
    pause_calls = [c for c in monitor.apply.call_args_list if c.kwargs.get("live") is False]
    assert len(enable_calls) == 2
    assert all(c.args[0] == "Spark stream" for c in enable_calls)
    assert len(pause_calls) == 1
    assert not (tmp_path / "g.pid").exists()  # slot released on exit


def test_guard_pauses_and_exits_when_never_live(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.time, "sleep", lambda *_: None)
    monkeypatch.setattr(gate, "_install_guard_signal_handler", lambda: None)
    monitor = MagicMock()
    monitor.twitch.get_stream.return_value = None  # never live
    args = _guard_args(tmp_path)

    rc = gate._run_guard(monitor, "chan", args)

    assert rc == 0
    monitor.apply.assert_called_once_with("", live=False)  # default-deny, then exit


def test_guard_single_read_blip_does_not_tear_down(tmp_path, monkeypatch):
    monkeypatch.setattr(gate.time, "sleep", lambda *_: None)
    monkeypatch.setattr(gate, "_install_guard_signal_handler", lambda: None)
    monitor = MagicMock()
    # live, one blip (offline 1/2), live again, then a confirmed end (2 offline).
    monitor.twitch.get_stream.side_effect = [
        {"title": "Spark"},  # go-live
        None,                 # blip 1/2
        {"title": "Spark"},  # recovered -> resets counter
        None,                 # offline 1/2
        None,                 # offline 2/2 -> end
    ]
    args = _guard_args(tmp_path)

    rc = gate._run_guard(monitor, "chan", args)

    assert rc == 0
    pause_calls = [c for c in monitor.apply.call_args_list if c.kwargs.get("live") is False]
    assert len(pause_calls) == 1  # only torn down once, at the real end


# ---------------------------------------------------------------------------
# Guard single-instance (pidfile)
# ---------------------------------------------------------------------------


def test_acquire_guard_slot_supersedes_previous(tmp_path, monkeypatch):
    pidfile = tmp_path / "g.pid"
    pidfile.write_text("424242")  # an existing live guard
    killed = []
    monkeypatch.setattr(gate, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(gate.os, "kill", lambda pid, sig: killed.append((pid, sig)))

    gate._acquire_guard_slot(str(pidfile))

    assert (424242, gate.signal.SIGTERM) in killed
    assert pidfile.read_text().strip() == str(gate.os.getpid())


def test_release_guard_slot_only_removes_own(tmp_path):
    pidfile = tmp_path / "g.pid"
    pidfile.write_text(str(gate.os.getpid()))
    gate._release_guard_slot(str(pidfile))
    assert not pidfile.exists()

    # A pidfile owned by someone else is left alone.
    pidfile.write_text("999999")
    gate._release_guard_slot(str(pidfile))
    assert pidfile.exists()
