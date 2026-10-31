#!/usr/bin/env python3
"""Interactive, end-to-end setup wizard for streaming-ad-control.

Walks you through the whole setup once, in order, and writes a ready-to-use
env file (and optionally a rules file) at the end:

  1. Prerequisite checks (Python, Chromium for Reddit).
  2. How you'll drive it: polling daemon, OBS integration, or both.
  3. Which ad networks to toggle: any of Reddit Ads, TrafficStars, ExoClick,
     PropellerAds, and Meta (Facebook/Instagram).
  4. One rule per stream topic (e.g. FHI, Spark dev): title keywords, exclude
     keywords, and the campaigns each topic runs.
  5. Optional go-live announcements: X, Bluesky, Mastodon, YouTube.
  6. Writes the env file (chmod 600) and an optional rules.yaml.
  7. Offers to install deps, bootstrap the Reddit session, and verify the
     toggles end-to-end.

Every credential prompt tells you exactly where to click to get the value.

This script is deliberately STATIC: it asks questions, prints the official
URLs, and writes files. It talks to no network service and no language model
of its own — the only things it runs are the project's own install/bootstrap/
verify scripts, and only when you say yes.

Run it from the repo root:

    python scripts/setup.py

Nothing is written until the end, where it shows you the file and asks before
saving. Re-running is safe: an existing env file is backed up, not clobbered.
"""

import argparse
import os
import shutil
import subprocess
import sys
from datetime import datetime
from getpass import getpass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The network registry is stdlib-only at import time (clients load lazily),
# so the wizard still runs before dependencies are installed.
sys.path.insert(0, str(REPO_ROOT))
from stream_ad_monitor.networks import NETWORKS, NETWORKS_BY_KEY, Network  # noqa: E402


# --------------------------------------------------------------------------- #
# Tiny prompt toolkit (stdlib only, so this runs before deps are installed).
# --------------------------------------------------------------------------- #

class Abort(Exception):
    """Raised to unwind cleanly on Ctrl-C / EOF."""


def _read(prompt: str) -> str:
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        raise Abort()


def rule(char: str = "─") -> None:
    print(char * 72)


def heading(title: str) -> None:
    print()
    rule("━")
    print(f"  {title}")
    rule("━")


def note(text: str) -> None:
    """A wrapped informational paragraph."""
    for line in text.strip("\n").splitlines():
        print(f"  {line}")


def link(label: str, url: str) -> None:
    print(f"  → {label}\n      {url}")


def ask(prompt: str, default: str = "", *, secret: bool = False,
        allow_empty: bool = True) -> str:
    """Ask for a value. Shows the default; blank input keeps it."""
    suffix = f" [{default}]" if default else ""
    while True:
        if secret:
            # getpass can't show a default inline usefully; note it instead.
            if default:
                print("  (press ENTER to keep the current value)")
            raw = getpass(f"  {prompt}{suffix}: ")
        else:
            raw = _read(f"  {prompt}{suffix}: ")
        raw = raw.strip()
        if not raw:
            raw = default
        if raw or allow_empty:
            return raw
        print("    This value is required.")


def yes_no(prompt: str, default: bool = False) -> bool:
    hint = "[Y/n]" if default else "[y/N]"
    while True:
        raw = _read(f"  {prompt} {hint}: ").strip().lower()
        if not raw:
            return default
        if raw in ("y", "yes"):
            return True
        if raw in ("n", "no"):
            return False
        print("    Please answer y or n.")


def choose(prompt: str, options: list[tuple[str, str]], default: int = 0) -> str:
    """Single-choice menu. `options` is a list of (key, description)."""
    print(f"  {prompt}")
    for i, (_, desc) in enumerate(options, 1):
        marker = "*" if i - 1 == default else " "
        print(f"    {marker} {i}) {desc}")
    while True:
        raw = _read(f"  choice [{default + 1}]: ").strip()
        if not raw:
            return options[default][0]
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][0]
        print(f"    Enter a number 1-{len(options)}.")


# --------------------------------------------------------------------------- #
# Setup steps.
# --------------------------------------------------------------------------- #

def check_prerequisites(need_reddit_browser: bool) -> None:
    heading("Prerequisites")
    ok = True

    major, minor = sys.version_info[:2]
    if (major, minor) >= (3, 10):
        print(f"  ✓ Python {major}.{minor} (need 3.10+)")
    else:
        ok = False
        print(f"  ✗ Python {major}.{minor} — this project needs 3.10+")

    if need_reddit_browser:
        browser = next(
            (b for b in ("chromium", "chromium-browser", "google-chrome",
                         "google-chrome-stable", "chrome")
             if shutil.which(b)),
            None,
        )
        if browser:
            print(f"  ✓ Browser for Selenium found: {browser}")
        else:
            ok = False
            print("  ✗ No Chromium/Chrome found — Reddit control drives a real")
            print("    browser via Selenium and needs one installed:")
            print("        sudo apt-get install -y chromium-browser")
            print("    (any Blink-based browser Selenium can drive works)")

    if not ok:
        print()
        if not yes_no("Some prerequisites are missing. Continue anyway?", False):
            raise Abort()


def pick_mode() -> str:
    heading("How will you drive the ad toggles?")
    note(
        """
Two ways to flip campaigns when you go live (you can use both):

  • Polling daemon — a systemd service that polls the Twitch API on a timer.
    Needs Twitch API credentials.
  • OBS integration — OBS's own start/stop events flip campaigns instantly,
    with NO Twitch API credentials at all. Reads your title from Twitch's
    public endpoint.
        """
    )
    return choose(
        "Pick your driver:",
        [
            ("daemon", "Polling daemon (run.py / systemd)"),
            ("obs", "OBS integration (event-driven, no Twitch creds)"),
            ("both", "Both — OBS for instant toggles, daemon as a backstop"),
        ],
        default=0,
    )


def pick_networks() -> list[str]:
    heading("Which ad network(s) do you want to control?")
    note(
        """
Pick any combination. You only configure credentials for networks you use.

  • Reddit Ads    — no toggle API, so a headless Chromium logs in once and the
                    session is reused. Most setup; good for subreddit targeting.
  • TrafficStars  — plain REST, one API key.
  • ExoClick      — plain REST, one API token.
  • PropellerAds  — plain REST, one static API token (simplest of all).
  • Meta          — Facebook/Instagram via the Marketing API, one system-user
                    token that never expires.
        """
    )
    while True:
        chosen = [n.key for n in NETWORKS if yes_no(f"Use {n.label}?", n.key == "reddit")]
        if chosen:
            return chosen
        print("    Pick at least one network.")


def collect_twitch(mode: str, env: dict) -> None:
    heading("Twitch")
    note("Your channel login is the name in your URL: twitch.tv/<this>. "
         "It is not a secret.")
    env["TWITCH_CHANNEL_LOGIN"] = ask("Twitch channel login", allow_empty=False)

    # The polling daemon always needs API creds. OBS-only setups don't, unless
    # the user later switches the title source to helix.
    if mode in ("daemon", "both"):
        print()
        note("The polling daemon reads the live title from the official Twitch "
             "API, which needs a registered app:")
        link("Create an app (any name; OAuth redirect http://localhost is fine)",
             "https://dev.twitch.tv/console/apps/create")
        note("Then open the app and copy its Client ID, and 'New Secret' for "
             "the Client Secret.")
        env["TWITCH_CLIENT_ID"] = ask("TWITCH_CLIENT_ID", allow_empty=False)
        env["TWITCH_CLIENT_SECRET"] = ask("TWITCH_CLIENT_SECRET", secret=True,
                                          allow_empty=False)
    else:
        print()
        note("OBS reads the title from Twitch's PUBLIC endpoint — no app, no "
             "credentials. Skipping Twitch API keys.")
        if yes_no("Register a Twitch app anyway (for --title-source helix)?",
                  False):
            link("Create an app", "https://dev.twitch.tv/console/apps/create")
            env["#TWITCH_CLIENT_ID"] = ask("TWITCH_CLIENT_ID")
            env["#TWITCH_CLIENT_SECRET"] = ask("TWITCH_CLIENT_SECRET", secret=True)


def collect_reddit(env: dict, state: dict) -> None:
    heading("Reddit Ads")
    note(
        """
Reddit control works off a saved browser session (a "cookie jar") created once
with scripts/bootstrap_reddit_session.py. With the jar, the daemon runs
headlessly and needs NO username/password stored.

Heads up:
  • Use a dedicated, NON-2FA account for ads access — 2FA is not supported.
  • The bootstrap must run once on a machine with a desktop/display.
        """
    )
    default_jar = str(Path.home() / ".config" / "streaming-ad-control"
                      / "reddit-session.json")
    jar = ask("Where should the Reddit session be stored (REDDIT_COOKIE_JAR)?",
              default_jar, allow_empty=False)
    env["REDDIT_COOKIE_JAR"] = jar
    state["reddit_cookie_jar"] = jar

    print()
    note("Login credentials are only used by the one-time bootstrap (and only "
         "if the jar doesn't already hold a live session). Store them in the "
         "env file now, or type them at bootstrap time.")
    if yes_no("Store Reddit username/password in the env file?", False):
        link("The account you log into Reddit Ads with",
             "https://ads.reddit.com/")
        env["REDDIT_USERNAME"] = ask("REDDIT_USERNAME", allow_empty=False)
        env["REDDIT_PASSWORD"] = ask("REDDIT_PASSWORD", secret=True,
                                     allow_empty=False)
    else:
        env["#REDDIT_USERNAME"] = "your_reddit_user"
        env["#REDDIT_PASSWORD"] = "your_reddit_password"

    print()
    note(
        """
Finding a Reddit campaign ID: open the campaign in the ads dashboard. The URL
reads ads.reddit.com/.../dashboard/campaigns/<LONG_NUMBER> — that trailing
number is the campaign ID.
        """
    )
    link("Reddit Ads dashboard", "https://ads.reddit.com/")
    print()
    note("The ads account id is auto-discovered after login. Set it only to "
         "pin one account when your login has several.")
    acct = ask("REDDIT_ADS_ACCOUNT_ID (optional, ENTER to skip)")
    if acct:
        env["REDDIT_ADS_ACCOUNT_ID"] = acct


def collect_trafficstars(env: dict) -> None:
    heading("TrafficStars")
    note(
        """
TrafficStars is a plain REST API. Your account API key acts as the OAuth2
refresh token — the daemon renews short-lived bearers from it automatically.
The key itself does not expire unless you regenerate it.
        """
    )
    link("Generate / copy your API key on your profile page",
         "https://admin.trafficstars.com/profile/")
    link("Campaign IDs are the numbers in your campaign list",
         "https://admin.trafficstars.com/")
    env["TRAFFICSTARS_API_KEY"] = ask("TRAFFICSTARS_API_KEY", secret=True,
                                      allow_empty=False)


def collect_exoclick(env: dict) -> None:
    heading("ExoClick")
    note(
        """
Plain REST API. You create a permanent API token once; the daemon swaps it for
short-lived session tokens on its own.
        """
    )
    link("Log in → My Profile → API Tokens → create a token",
         "https://admin.exoclick.com/")
    link("Campaign IDs are the numbers in your campaign list",
         "https://admin.exoclick.com/")
    link("API reference (try calls in the browser)",
         "https://api.exoclick.com/v2/docs/")
    env["EXOCLICK_API_TOKEN"] = ask("EXOCLICK_API_TOKEN", secret=True,
                                    allow_empty=False)


def collect_propellerads(env: dict) -> None:
    heading("PropellerAds")
    note(
        """
The simplest network: one static API token sent on every request, no token
exchange. API access may be gated on account history (e.g. a minimum spend) —
if the Profile page shows no API token, ask PropellerAds support to enable it.
        """
    )
    link("Log in → Profile → API token (copy it)",
         "https://partners.propellerads.com/")
    link("Campaign IDs are the numbers in your campaign list",
         "https://partners.propellerads.com/")
    link("API reference", "https://ssp-api.propellerads.com/v5/docs/")
    env["PROPELLERADS_API_TOKEN"] = ask("PROPELLERADS_API_TOKEN", secret=True,
                                        allow_empty=False)


def collect_meta(env: dict) -> None:
    heading("Meta (Facebook / Instagram)")
    note(
        """
Uses the Marketing API with a SYSTEM USER token, which never expires. One-time
setup in Meta Business Suite:

  1. Create an app (type "Business") and add the Marketing API product.
  2. Business Settings → Users → System users → Add (role: Admin or Employee).
  3. On that system user: Assign assets → your ad account → "Manage campaigns".
  4. Generate new token → pick your app → tick ads_management → copy it.

Campaign IDs: Ads Manager → Campaigns tab → add the "Campaign ID" column
(Columns → Customize columns), or open a campaign and read it from the URL.

Health-related ads (e.g. FHI) can get extra review on Meta; pausing is always
allowed, but a resumed campaign may sit in review before it delivers.
        """
    )
    link("Create an app", "https://developers.facebook.com/apps/")
    link("System users", "https://business.facebook.com/settings/system-users")
    link("Ads Manager", "https://adsmanager.facebook.com/")
    env["META_ACCESS_TOKEN"] = ask("META_ACCESS_TOKEN", secret=True,
                                   allow_empty=False)


_CREDENTIAL_COLLECTORS = {
    "trafficstars": collect_trafficstars,
    "exoclick": collect_exoclick,
    "propellerads": collect_propellerads,
    "meta": collect_meta,
}


def _ids(prompt: str) -> list[str]:
    raw = ask(prompt)
    return [i.strip() for i in raw.split(",") if i.strip()]


def _ask_ids(network: Network, required: bool) -> list[str]:
    while True:
        suffix = "" if required else ", ENTER for none"
        ids = _ids(f"  {network.label} campaign IDs (comma-separated{suffix})")
        bad = [i for i in ids if network.numeric_ids and not i.isdigit()]
        if bad:
            print(f"    {network.label} IDs are numeric — find them in "
                  f"{network.id_hint}. Got: {bad}")
            continue
        if ids or not required:
            return ids
        print("    At least one ID is required.")


def collect_rules(networks: list[str], env: dict, state: dict) -> None:
    """Either a single keyword→campaign rule (env vars) or a rules.yaml."""
    heading("Rules: which title keyword gates which campaign(s)")
    note(
        """
Each rule is one stream topic: title keywords → the campaigns to run while
that topic is live. Matching is case-insensitive and on WHOLE WORDS, so
"Spark" matches "Apache Spark" and "#Spark" but not "sparkle". Multi-word
keywords ("Fight Health Insurance") match as a phrase.

Streaming more than one topic (e.g. FHI and Spark dev)? Use one rule per
topic — that needs a rules.yaml.
        """
    )
    multi = yes_no("Do you stream more than one topic (write a rules.yaml)?",
                   True)
    if multi:
        _collect_rules_yaml(networks, env, state)
        return

    keyword = ask("Trigger keyword (title must contain this to run ads)",
                  "Spark", allow_empty=False)
    env["TRIGGER_KEYWORD"] = keyword
    excludes = ask("Exclude keywords — never run ads if the title has any "
                   "(comma-separated, ENTER for none)")
    if excludes:
        env["TRIGGER_EXCLUDE_KEYWORDS"] = excludes
    for key in networks:
        network = NETWORKS_BY_KEY[key]
        ids = _ask_ids(network, required=True)
        # Single-rule env mode takes one ID per network.
        env[network.legacy_env] = ids[0]
        if len(ids) > 1:
            print(f"    (single-rule mode takes one ID; using {ids[0]}. "
                  "Use a rules.yaml for more.)")
        state.setdefault("verify_ids", {})[key] = ids[0]


def _quote_list(items: list[str], indent: str) -> list[str]:
    return [f'{indent}- "{i}"' for i in items]


def _collect_rules_yaml(networks: list[str], env: dict, state: dict) -> None:
    default_path = str(Path.home() / ".config" / "streaming-ad-control"
                       / "rules.yaml")
    path = ask("Where should rules.yaml live (RULES_FILE)?", default_path,
               allow_empty=False)
    rules: list[dict] = []
    print()
    note("Add one rule per topic. Leave the rule name blank to finish.")
    while True:
        print()
        name = ask(f"Rule #{len(rules) + 1} name, e.g. 'FHI' or 'Spark dev' "
                   "(blank to finish)")
        if not name:
            if rules:
                break
            print("    Add at least one rule.")
            continue
        keywords = [k.strip() for k in
                    ask("  Keywords (comma-separated)", allow_empty=False).split(",")
                    if k.strip()]
        excludes = [k.strip() for k in
                    ask("  Exclude keywords (comma-separated, ENTER for none)").split(",")
                    if k.strip()]
        ids: dict[str, list[str]] = {}
        while not ids:
            for key in networks:
                found = _ask_ids(NETWORKS_BY_KEY[key], required=False)
                if found:
                    ids[key] = found
            if not ids:
                print("    A rule needs at least one campaign on some network.")
        rules.append({"name": name, "keywords": keywords,
                      "excludes": excludes, "ids": ids})

    if len(rules) > 1:
        print()
        note("Should a title that mentions two topics (\"Fixing FHI's Spark "
             "job\") run only the ads of... neither? Making topics exclusive "
             "adds each rule's keywords to the other rules' exclude list.")
        if yes_no("Make topics mutually exclusive?", False):
            for r in rules:
                for other in rules:
                    if other is not r:
                        r["excludes"] += [k for k in other["keywords"]
                                          if k not in r["excludes"]]

    blocks = []
    for r in rules:
        block = [f'  - name: "{r["name"]}"', "    keywords:"]
        block += _quote_list(r["keywords"], "      ")
        if r["excludes"]:
            block.append("    exclude_keywords:")
            block += _quote_list(r["excludes"], "      ")
        for key, found in r["ids"].items():
            network = NETWORKS_BY_KEY[key]
            block.append(f"    {network.yaml_keys[0]}:   # {network.label}")
            block += _quote_list(found, "      ")
            state.setdefault("verify_ids", {}).setdefault(key, found[0])
        blocks.append("\n".join(block))

    state["rules_file_path"] = path
    state["rules_file_content"] = (
        "# Generated by scripts/setup.py\n"
        "# Keywords match case-insensitively on whole words. A campaign listed\n"
        "# under several rules runs while any of them matches.\n"
        "rules:\n" + "\n\n".join(blocks) + "\n"
    )
    env["RULES_FILE"] = path


def collect_announcements(env: dict) -> None:
    heading("Go-live announcements (optional)")
    note(
        """
Independent of the ad toggles: post your stream link to X, Bluesky, and/or
Mastodon when you go live, and thread the YouTube simulcast link underneath
once it appears. Configure any combination, or none.
        """
    )
    if not yes_no("Set up any go-live announcements now?", False):
        return

    # --- X (Twitter) ---
    if yes_no("Announce on X (Twitter)?", False):
        print()
        note(
            """
IMPORTANT: set the app to Read AND Write BEFORE generating tokens — tokens
minted under read-only keep that scope and posting fails with a 403.
            """
        )
        link("Create a project + app",
             "https://developer.x.com/en/portal/dashboard")
        note("In the app: User authentication settings → App permissions → "
             "Read and write. Then Keys and tokens → copy API Key/Secret and "
             "generate an Access Token/Secret for the posting account.")
        env["TWITTER_API_KEY"] = ask("TWITTER_API_KEY", secret=True,
                                     allow_empty=False)
        env["TWITTER_API_SECRET"] = ask("TWITTER_API_SECRET", secret=True,
                                        allow_empty=False)
        env["TWITTER_ACCESS_TOKEN"] = ask("TWITTER_ACCESS_TOKEN", secret=True,
                                          allow_empty=False)
        env["TWITTER_ACCESS_TOKEN_SECRET"] = ask(
            "TWITTER_ACCESS_TOKEN_SECRET", secret=True, allow_empty=False)

    # --- Bluesky ---
    if yes_no("Announce on Bluesky?", False):
        print()
        note("Use an APP PASSWORD, never your account password.")
        link("Settings → Privacy and security → App passwords → Add",
             "https://bsky.app/settings/app-passwords")
        env["BLUESKY_HANDLE"] = ask("BLUESKY_HANDLE (e.g. you.bsky.social)",
                                    allow_empty=False)
        env["BLUESKY_APP_PASSWORD"] = ask(
            "BLUESKY_APP_PASSWORD (xxxx-xxxx-xxxx-xxxx)", secret=True,
            allow_empty=False)
        pds = ask("BLUESKY_PDS_URL (only if self-hosting a PDS, ENTER to skip)")
        if pds:
            env["BLUESKY_PDS_URL"] = pds

    # --- Mastodon ---
    if yes_no("Announce on Mastodon?", False):
        print()
        note("On YOUR instance: Preferences → Development → New application. "
             "The write:statuses scope is all it needs — uncheck the rest, "
             "then copy 'Your access token'.")
        note("(On mastodon.social that page is "
             "https://mastodon.social/settings/applications)")
        env["MASTODON_ACCESS_TOKEN"] = ask("MASTODON_ACCESS_TOKEN", secret=True,
                                           allow_empty=False)
        inst = ask("MASTODON_INSTANCE_URL (ENTER for default https://tech.lgbt)")
        if inst:
            env["MASTODON_INSTANCE_URL"] = inst
        vis = ask("MASTODON_VISIBILITY public/unlisted/private/direct "
                  "(ENTER for public)")
        if vis:
            env["MASTODON_VISIBILITY"] = vis

    # --- YouTube follow-up (no API key) ---
    if yes_no("Thread a YouTube simulcast link when it appears?", False):
        print()
        note("No API key or Google project needed — just the channel whose "
             "/live page should be watched.")
        handle = ask("YOUTUBE_CHANNEL_HANDLE (e.g. @yourhandle)")
        if handle:
            env["YOUTUBE_CHANNEL_HANDLE"] = handle
        else:
            cid = ask("YOUTUBE_CHANNEL_ID (UC..., if you have no handle)")
            if cid:
                env["YOUTUBE_CHANNEL_ID"] = cid

    # --- State file (duplicate-post guarantee across restarts) ---
    if any(k in env for k in (
        "TWITTER_API_KEY", "BLUESKY_HANDLE", "MASTODON_ACCESS_TOKEN",
    )):
        print()
        note("Strongly recommended: a state file keeps a restart mid-stream "
             "from re-announcing the stream you're already on.")
        default_state = str(Path.home() / ".config" / "streaming-ad-control"
                            / "announce-state.json")
        if yes_no("Set ANNOUNCE_STATE_FILE?", True):
            env["ANNOUNCE_STATE_FILE"] = ask("ANNOUNCE_STATE_FILE",
                                             default_state, allow_empty=False)


# --------------------------------------------------------------------------- #
# Rendering and writing.
# --------------------------------------------------------------------------- #

# Human-readable section order for the generated env file.
_SECTIONS = [
    ("Twitch", ["TWITCH_CHANNEL_LOGIN", "TWITCH_CLIENT_ID",
                "TWITCH_CLIENT_SECRET", "#TWITCH_CLIENT_ID",
                "#TWITCH_CLIENT_SECRET", "POLL_INTERVAL"]),
    ("Reddit Ads", ["REDDIT_COOKIE_JAR", "REDDIT_USERNAME", "REDDIT_PASSWORD",
                    "#REDDIT_USERNAME", "#REDDIT_PASSWORD",
                    "REDDIT_ADS_ACCOUNT_ID"]),
    ("TrafficStars", ["TRAFFICSTARS_API_KEY"]),
    ("ExoClick", ["EXOCLICK_API_TOKEN"]),
    ("PropellerAds", ["PROPELLERADS_API_TOKEN"]),
    ("Meta (Facebook / Instagram)", ["META_ACCESS_TOKEN"]),
    ("Rules", ["RULES_FILE", "TRIGGER_KEYWORD", "TRIGGER_EXCLUDE_KEYWORDS",
               *(n.legacy_env for n in NETWORKS)]),
    ("Announcements — X (Twitter)", [
        "TWITTER_API_KEY", "TWITTER_API_SECRET", "TWITTER_ACCESS_TOKEN",
        "TWITTER_ACCESS_TOKEN_SECRET"]),
    ("Announcements — Bluesky", ["BLUESKY_HANDLE", "BLUESKY_APP_PASSWORD",
                                 "BLUESKY_PDS_URL"]),
    ("Announcements — Mastodon", ["MASTODON_ACCESS_TOKEN",
                                  "MASTODON_INSTANCE_URL",
                                  "MASTODON_VISIBILITY"]),
    ("Announcements — YouTube follow-up", ["YOUTUBE_CHANNEL_HANDLE",
                                           "YOUTUBE_CHANNEL_ID"]),
    ("Announcements — misc", ["ANNOUNCE_STATE_FILE"]),
]


def render_env(env: dict) -> str:
    lines = [
        "# streaming-ad-control environment file",
        f"# Generated by scripts/setup.py on {datetime.now():%Y-%m-%d %H:%M}",
        "# Lines beginning with # are comments. Quotes around values are",
        "# stripped. Keep this file private — it holds secrets.",
    ]
    placed: set[str] = set()
    for title, keys in _SECTIONS:
        present = [k for k in keys if k in env]
        if not present:
            continue
        lines.append("")
        lines.append(f"# --- {title} " + "-" * max(0, 60 - len(title)))
        for k in present:
            placed.add(k)
            if k.startswith("#"):
                # A commented-out placeholder: "#NAME" -> "# NAME=value".
                lines.append(f"# {k[1:]}={env[k]}")
            else:
                lines.append(f"{k}={env[k]}")
    # Anything not covered by a known section (future-proofing).
    extra = [k for k in env if k not in placed]
    if extra:
        lines.append("")
        lines.append("# --- Other " + "-" * 56)
        for k in extra:
            lines.append(f"{k}={env[k]}")
    return "\n".join(lines) + "\n"


def write_file(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_suffix(path.suffix + f".bak-{datetime.now():%Y%m%d%H%M%S}")
        shutil.copy2(path, backup)
        print(f"  (existing file backed up to {backup})")
    path.write_text(content)
    os.chmod(path, mode)
    print(f"  ✓ wrote {path}  (mode {mode:o})")


# --------------------------------------------------------------------------- #
# Optional follow-on actions (install / bootstrap / verify).
# --------------------------------------------------------------------------- #

def _run(cmd: list[str], env_overlay: dict | None = None) -> int:
    print(f"\n  $ {' '.join(cmd)}")
    run_env = os.environ.copy()
    if env_overlay:
        run_env.update(env_overlay)
    try:
        return subprocess.call(cmd, cwd=str(REPO_ROOT), env=run_env)
    except FileNotFoundError as exc:
        print(f"    could not run: {exc}")
        return 1


def _env_overlay_from(env: dict) -> dict:
    """Real (uncommented) key/values, usable as a subprocess environment."""
    return {k: v for k, v in env.items() if not k.startswith("#")}


def offer_followups(env: dict, state: dict, networks: list[str]) -> None:
    heading("Next: install, bootstrap, verify")
    overlay = _env_overlay_from(env)

    if yes_no("Install Python dependencies now (pip install -e .)?", False):
        _run([sys.executable, "-m", "pip", "install", "-e", "."])

    if "reddit" in networks:
        print()
        note("The Reddit bootstrap opens a real browser window to log in once "
             "and save the session. Run it on a machine with a display.")
        if yes_no("Run the Reddit session bootstrap now?", False):
            _run([sys.executable, "scripts/bootstrap_reddit_session.py"],
                 overlay)

    # One round-trip per network, on the first campaign configured for it.
    # Each leaves the campaign PAUSED.
    for key, cid in state.get("verify_ids", {}).items():
        label = NETWORKS_BY_KEY[key].label
        if not yes_no(f"Verify {label} toggle on campaign {cid} "
                      "(enable→pause)?", False):
            continue
        if key == "reddit":
            _run([sys.executable, "scripts/test_enable_disable.py", cid],
                 overlay)
        else:
            _run([sys.executable, "scripts/test_enable_disable_network.py",
                  key, cid], overlay)

    announces = any(k in env for k in (
        "TWITTER_API_KEY", "BLUESKY_HANDLE", "MASTODON_ACCESS_TOKEN"))
    if announces:
        if yes_no("Dry-run the announcement (renders text, posts nothing)?",
                  True):
            _run([sys.executable, "scripts/test_announce.py", "--dry-run"],
                 overlay)


def print_next_steps(mode: str, env: dict, env_path: Path, state: dict,
                     need_reddit: bool) -> None:
    heading("You're set up")
    print(f"  Env file: {env_path}")
    if "rules_file_path" in state:
        print(f"  Rules:    {state['rules_file_path']}")
    print()
    note("To load the env into your shell for a manual run:")
    print(f"      set -a; source {env_path}; set +a")
    print()

    if need_reddit and "REDDIT_COOKIE_JAR" in env:
        note("Reddit runs off a saved session. If you haven't bootstrapped "
             "yet, do it once on a desktop:")
        print(f"      set -a; source {env_path}; set +a")
        print("      python scripts/bootstrap_reddit_session.py")
        print()

    if mode in ("daemon", "both"):
        note("Start the polling daemon manually:")
        print("      python run.py")
        print()
        note("Or install it as a systemd service (see README → 'Enable the "
             "service'): copy contrib/streaming-ad-monitor.service, point its "
             "EnvironmentFile at your env file, then:")
        print("      sudo systemctl enable --now streaming-ad-monitor")
        print()

    if mode in ("obs", "both"):
        note("For OBS: copy your env file where OBS can read it, then in OBS "
             "load contrib/obs_ad_control.py (Tools → Scripts → +) and set its "
             "'Env file' property. Full walkthrough in README → 'OBS "
             "integration'.")
        print()

    note("Re-reading the README is worth it — it covers token rotation, the "
         "no-duplicate-posts guarantee, and failure modes.")


# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--env-file", default=None,
        help="Where to write the env file (default: ask).")
    args = parser.parse_args()

    print(__doc__.split("\n\n")[0])
    print()
    note("This wizard writes files but calls no network service on its own. "
         "Press Ctrl-C any time to abort — nothing is saved until you confirm.")

    env: dict = {}
    state: dict = {}

    try:
        mode = pick_mode()
        networks = pick_networks()
        need_reddit = "reddit" in networks

        check_prerequisites(need_reddit_browser=need_reddit)

        collect_twitch(mode, env)
        if need_reddit:
            collect_reddit(env, state)
        for key in networks:
            if key in _CREDENTIAL_COLLECTORS:
                _CREDENTIAL_COLLECTORS[key](env)
        collect_rules(networks, env, state)
        collect_announcements(env)

        # --- Review & write ---
        heading("Review")
        content = render_env(env)
        print(content)
        if "rules_file_content" in state:
            print(f"  rules.yaml → {state['rules_file_path']}:")
            rule()
            print(state["rules_file_content"])

        if not yes_no("Write these files now?", True):
            print("\n  Nothing written. Re-run when ready.")
            return 1

        default_path = args.env_file or str(
            Path.home() / ".config" / "streaming-ad-control" / "env")
        env_path = Path(ask("Env file path", default_path, allow_empty=False)
                        ).expanduser()
        write_file(env_path, content, 0o600)
        if "rules_file_content" in state:
            write_file(Path(state["rules_file_path"]).expanduser(),
                       state["rules_file_content"], 0o644)

        offer_followups(env, state, networks)
        print_next_steps(mode, env, env_path, state, need_reddit)
        return 0

    except Abort:
        print("\n\n  Aborted. Nothing was saved.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
