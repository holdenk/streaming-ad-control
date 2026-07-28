"""OBS Studio script: enable/disable ad campaigns when you start/stop streaming.

Load this from OBS via **Tools → Scripts → +** and point it at your
streaming-ad-control checkout. When you go live it shells out to
``scripts/obs_title_gate.py`` (running in *your* Python venv, not OBS's), which
reads your stream title and enables the campaigns whose keywords match; when
you stop streaming it pauses every campaign.

Two nice properties:

  * Credential-free by default. The title is read from Twitch's public GQL
    endpoint, so you don't need to register a Twitch developer app — just your
    channel name. (Switch to the Helix API, or a manual title, in the props.)
  * Crash-safe teardown. With "watch" on, going live launches a detached guard
    that keeps polling and pauses the campaign when the stream actually ends —
    even if OBS is force-killed and never fires a stop event.

Why shell out instead of doing the work here: the gate uses Selenium + a
headless Chromium (for Reddit) and blocking network calls. Running those on
OBS's UI thread would freeze the OBS window. This script only ever launches a
detached subprocess and returns immediately.

Setup:
  1. Bootstrap the Reddit session and write your env/rules files (see README).
  2. In this script's properties, set the Python executable (your venv), the
     streaming-ad-control directory, and the env file
     (contrib/obs-ad-control.env.example).
  3. Go live. Watch the Script Log and the configured gate log file.
"""

import os

import obspython as obs

# Populated from the script properties in script_update().
_settings = {
    "python_exe": "",
    "repo_dir": "",
    "env_file": "",
    "title_source": "public",  # public | helix | manual
    "manual_title": "",
    "channel": "",
    "watch": True,
    "twitch_timeout": 45,
    "watch_poll": 60,
    "log_file": "",
    "log_level": "INFO",
}

# Keep references to spawned subprocesses so the interpreter doesn't garbage
# collect them; trimmed opportunistically in _spawn().
_children = []


def _log(message):
    """Print to the OBS Script Log window."""
    print("[ad-control] " + message)


def _gate_script():
    """Absolute path to the title-gate CLI inside the configured checkout."""
    if not _settings["repo_dir"]:
        return ""
    return os.path.join(_settings["repo_dir"], "scripts", "obs_title_gate.py")


def _build_command(event):
    """Return the argv list to run the gate for *event*, or None if misconfigured."""
    python_exe = _settings["python_exe"].strip()
    gate = _gate_script()

    if not python_exe or not os.path.isfile(python_exe):
        _log("Python executable is not set or does not exist: %r" % python_exe)
        return None
    if not gate or not os.path.isfile(gate):
        _log(
            "Can't find scripts/obs_title_gate.py under the configured "
            "directory (%r). Point 'streaming-ad-control directory' at your "
            "checkout." % _settings["repo_dir"]
        )
        return None

    cmd = [python_exe, gate, event, "--log-level", _settings["log_level"]]

    env_file = _settings["env_file"].strip()
    if env_file:
        cmd += ["--env-file", env_file]

    if event == "started":
        source = _settings["title_source"]
        if source == "manual":
            # Explicit title: no Twitch lookup, and no guard (nothing to detect
            # the stream ending). The stop hook handles teardown.
            cmd += ["--title", _settings["manual_title"]]
        else:
            cmd += ["--title-source", source]
            cmd += ["--twitch-timeout", str(int(_settings["twitch_timeout"]))]
            channel = _settings["channel"].strip()
            if channel:
                cmd += ["--channel", channel]
            if _settings["watch"]:
                cmd += ["--watch", "--watch-poll", str(int(_settings["watch_poll"]))]
    return cmd


def _spawn(event):
    """Launch the gate for *event* as a detached, non-blocking subprocess."""
    import subprocess

    cmd = _build_command(event)
    if cmd is None:
        return

    log_file = _settings["log_file"].strip()
    stdout = stderr = None
    log_handle = None
    if log_file:
        try:
            log_handle = open(log_file, "a", encoding="utf-8")
            log_handle.write("\n=== ad-control gate: %s ===\n" % event)
            log_handle.flush()
            stdout = stderr = log_handle
        except OSError as exc:
            _log("Could not open log file %r: %s (continuing without it)" % (log_file, exc))
            log_handle = None

    try:
        # start_new_session detaches the child from OBS's process group so a
        # long-running --watch guard survives an OBS crash / restart (POSIX).
        proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr, start_new_session=True)
    except (OSError, ValueError) as exc:
        # ValueError guards platforms where start_new_session isn't supported.
        _log("Failed to launch gate: %s" % exc)
        return
    finally:
        # Popen dup'd the fd into the child, so the parent's handle can close —
        # avoids leaking one per stream toggle.
        if log_handle is not None:
            log_handle.close()

    _children[:] = [p for p in _children if p.poll() is None] + [proc]
    _log(
        "Launched gate for '%s' (pid %s)%s."
        % (event, proc.pid, (" → " + log_file) if log_file else "")
    )


# ---------------------------------------------------------------------------
# OBS frontend event hook
# ---------------------------------------------------------------------------


def _on_event(event):
    if event == obs.OBS_FRONTEND_EVENT_STREAMING_STARTED:
        _log("Streaming started.")
        _spawn("started")
    elif event == obs.OBS_FRONTEND_EVENT_STREAMING_STOPPED:
        _log("Streaming stopped.")
        _spawn("stopped")


# ---------------------------------------------------------------------------
# OBS script lifecycle
# ---------------------------------------------------------------------------


def script_description():
    return (
        "<b>Streaming ad control</b><br>"
        "Enables your Reddit / TrafficStars campaigns when you go live with a "
        "matching stream title, and pauses them when you stop.<br><br>"
        "Reads the title from Twitch's public endpoint by default (no Twitch "
        "credentials needed). With <i>watch</i> on, the campaign is also paused "
        "if OBS crashes without a clean stop. See contrib/obs_ad_control.py for "
        "setup."
    )


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_path(
        props, "python_exe", "Python executable (your venv)", obs.OBS_PATH_FILE, "", ""
    )
    obs.obs_properties_add_path(
        props, "repo_dir", "streaming-ad-control directory", obs.OBS_PATH_DIRECTORY, "", ""
    )
    obs.obs_properties_add_path(
        props, "env_file", "Env file (credentials + RULES_FILE)", obs.OBS_PATH_FILE, "", ""
    )

    source = obs.obs_properties_add_list(
        props,
        "title_source",
        "Title source",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    obs.obs_property_list_add_string(source, "Twitch — automatic, no credentials", "public")
    obs.obs_property_list_add_string(source, "Twitch API — needs credentials", "helix")
    obs.obs_property_list_add_string(source, "Manual title (below)", "manual")

    obs.obs_properties_add_text(
        props, "channel", "Twitch channel (defaults to TWITCH_CHANNEL_LOGIN)", obs.OBS_TEXT_DEFAULT
    )
    obs.obs_properties_add_text(
        props, "manual_title", "Manual title (only used by 'Manual title' source)", obs.OBS_TEXT_DEFAULT
    )
    obs.obs_properties_add_bool(
        props, "watch", "Watch until the stream ends (crash-safe; recommended)"
    )
    obs.obs_properties_add_int(
        props, "twitch_timeout", "Seconds to wait for Twitch to report live", 5, 300, 5
    )
    obs.obs_properties_add_int(
        props, "watch_poll", "Seconds between polls while watching", 15, 600, 15
    )
    obs.obs_properties_add_path(
        props, "log_file", "Gate log file (appended)", obs.OBS_PATH_FILE_SAVE, "", ""
    )
    log_levels = obs.obs_properties_add_list(
        props, "log_level", "Log level", obs.OBS_COMBO_TYPE_LIST, obs.OBS_COMBO_FORMAT_STRING
    )
    for level in ("INFO", "DEBUG", "WARNING", "ERROR"):
        obs.obs_property_list_add_string(log_levels, level, level)
    return props


def script_defaults(settings):
    obs.obs_data_set_default_string(settings, "title_source", "public")
    obs.obs_data_set_default_bool(settings, "watch", True)
    obs.obs_data_set_default_int(settings, "twitch_timeout", 45)
    obs.obs_data_set_default_int(settings, "watch_poll", 60)
    obs.obs_data_set_default_string(settings, "log_level", "INFO")


def script_update(settings):
    _settings["python_exe"] = obs.obs_data_get_string(settings, "python_exe")
    _settings["repo_dir"] = obs.obs_data_get_string(settings, "repo_dir")
    _settings["env_file"] = obs.obs_data_get_string(settings, "env_file")
    _settings["title_source"] = obs.obs_data_get_string(settings, "title_source") or "public"
    _settings["manual_title"] = obs.obs_data_get_string(settings, "manual_title")
    _settings["channel"] = obs.obs_data_get_string(settings, "channel")
    _settings["watch"] = obs.obs_data_get_bool(settings, "watch")
    _settings["twitch_timeout"] = obs.obs_data_get_int(settings, "twitch_timeout")
    _settings["watch_poll"] = obs.obs_data_get_int(settings, "watch_poll")
    _settings["log_file"] = obs.obs_data_get_string(settings, "log_file")
    _settings["log_level"] = obs.obs_data_get_string(settings, "log_level") or "INFO"


def script_load(settings):
    obs.obs_frontend_add_event_callback(_on_event)
    _log("Loaded; listening for streaming start/stop.")


def script_unload():
    obs.obs_frontend_remove_event_callback(_on_event)
