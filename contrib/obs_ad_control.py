"""OBS Studio script: enable/disable ad campaigns when you start/stop streaming.

Load this from OBS via **Tools → Scripts → +** and point it at your
streaming-ad-control checkout. When you go live it shells out to
``scripts/obs_title_gate.py`` (running in *your* Python venv, not OBS's), which
reads your stream title and enables the campaigns whose keywords match; when
you stop streaming it pauses every campaign.

Why shell out instead of doing the work here: the gate uses Selenium + a
headless Chromium (for Reddit) and blocking network calls. Running those on
OBS's UI thread would freeze the OBS window. This script only ever launches a
detached subprocess and returns immediately — all the heavy lifting happens in
your venv, and its output is appended to the log file configured below.

Setup:
  1. Bootstrap the Reddit session and write your env/rules files as described
     in the project README.
  2. In this script's properties, set:
       * Python executable — the interpreter in your venv, e.g.
         /home/you/.venvs/py313/bin/python
       * streaming-ad-control directory — your checkout (contains scripts/)
       * Env file — a KEY=value file with your credentials + RULES_FILE
         (see contrib/obs-ad-control.env.example)
  3. Go live. Watch the Script Log (and the gate log file) to confirm.

Note: OBS fires the "stopped" hook on a normal stop. If OBS is force-killed it
can't fire, so the campaigns would stay active. For a guaranteed teardown, also
run the polling daemon (run.py) as a backstop — it computes the same desired
state from the same title, so the two never fight.
"""

import os

import obspython as obs

# Populated from the script properties in script_update().
_settings = {
    "python_exe": "",
    "repo_dir": "",
    "env_file": "",
    "use_twitch_title": True,
    "manual_title": "",
    "twitch_timeout": 45,
    "log_file": "",
    "log_level": "INFO",
}

# Keep references to spawned subprocesses so the interpreter doesn't garbage
# collect (and potentially kill) them before they finish. Trimmed opportunistically.
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
        cmd += ["--twitch-timeout", str(int(_settings["twitch_timeout"]))]
        if not _settings["use_twitch_title"]:
            # Manual title mode: hand the gate an explicit title (skips Twitch).
            cmd += ["--title", _settings["manual_title"]]
    return cmd


def _spawn(event):
    """Launch the gate for *event* as a detached, non-blocking subprocess."""
    # Import subprocess lazily so merely loading the script never touches it.
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
        proc = subprocess.Popen(cmd, stdout=stdout, stderr=stderr)
    except OSError as exc:
        _log("Failed to launch gate: %s" % exc)
        return
    finally:
        # Popen dup'd the fd into the child, so the parent's handle is no longer
        # needed — closing it here avoids leaking one per stream toggle.
        if log_handle is not None:
            log_handle.close()

    # Reap any finished children so the list doesn't grow unbounded across a
    # long session, then keep a reference to the new one.
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
        "Point this at your <i>streaming-ad-control</i> checkout and your venv "
        "Python. The heavy lifting runs in a detached subprocess, so OBS never "
        "blocks. See the header of contrib/obs_ad_control.py for setup."
    )


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_path(
        props,
        "python_exe",
        "Python executable (your venv)",
        obs.OBS_PATH_FILE,
        "",
        "",
    )
    obs.obs_properties_add_path(
        props,
        "repo_dir",
        "streaming-ad-control directory",
        obs.OBS_PATH_DIRECTORY,
        "",
        "",
    )
    obs.obs_properties_add_path(
        props,
        "env_file",
        "Env file (credentials + RULES_FILE)",
        obs.OBS_PATH_FILE,
        "",
        "",
    )
    obs.obs_properties_add_bool(
        props,
        "use_twitch_title",
        "Read live title from Twitch (recommended)",
    )
    obs.obs_properties_add_text(
        props,
        "manual_title",
        "Manual title (used only when the box above is off)",
        obs.OBS_TEXT_DEFAULT,
    )
    obs.obs_properties_add_int(
        props,
        "twitch_timeout",
        "Seconds to wait for Twitch to report live",
        5,
        300,
        5,
    )
    obs.obs_properties_add_path(
        props,
        "log_file",
        "Gate log file (appended)",
        obs.OBS_PATH_FILE_SAVE,
        "",
        "",
    )
    log_levels = obs.obs_properties_add_list(
        props,
        "log_level",
        "Log level",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    for level in ("INFO", "DEBUG", "WARNING", "ERROR"):
        obs.obs_property_list_add_string(log_levels, level, level)
    return props


def script_defaults(settings):
    obs.obs_data_set_default_bool(settings, "use_twitch_title", True)
    obs.obs_data_set_default_int(settings, "twitch_timeout", 45)
    obs.obs_data_set_default_string(settings, "log_level", "INFO")


def script_update(settings):
    _settings["python_exe"] = obs.obs_data_get_string(settings, "python_exe")
    _settings["repo_dir"] = obs.obs_data_get_string(settings, "repo_dir")
    _settings["env_file"] = obs.obs_data_get_string(settings, "env_file")
    _settings["use_twitch_title"] = obs.obs_data_get_bool(settings, "use_twitch_title")
    _settings["manual_title"] = obs.obs_data_get_string(settings, "manual_title")
    _settings["twitch_timeout"] = obs.obs_data_get_int(settings, "twitch_timeout")
    _settings["log_file"] = obs.obs_data_get_string(settings, "log_file")
    _settings["log_level"] = obs.obs_data_get_string(settings, "log_level") or "INFO"


def script_load(settings):
    obs.obs_frontend_add_event_callback(_on_event)
    _log("Loaded; listening for streaming start/stop.")


def script_unload():
    obs.obs_frontend_remove_event_callback(_on_event)
