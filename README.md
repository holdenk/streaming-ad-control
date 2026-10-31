# streaming-ad-control

Monitors a Twitch stream, toggles ad campaigns based on the stream title, and
announces the stream on X, Bluesky, and Mastodon when it goes live.

Five ad networks are supported; each rule in `rules.yaml` can target any
combination of them:

- **Reddit Ads** — no official API access, so a headless Chromium handles
  login and cookie management, then a `requests.Session` (pre-loaded with the
  dashboard's `token_v2` bearer extracted via Selenium) PATCHes
  `ads-api.reddit.com` directly.
- **TrafficStars** — plain REST. The account API key (generate on
  [admin.trafficstars.com/profile](https://admin.trafficstars.com/profile/))
  acts as an OAuth2 refresh token against `POST /v1/auth/token`; campaigns
  are toggled with `PUT /v2/campaigns/run` / `PUT /v2/campaigns/pause`.
  No browser needed. ([API docs](https://docs.trafficstars.com/))
- **ExoClick** — plain REST. A permanent API token (My Profile → API Tokens on
  [admin.exoclick.com](https://admin.exoclick.com/)) is swapped for a session
  bearer via `POST /v2/login`; campaigns toggle with `PUT /v2/campaigns/play`
  / `PUT /v2/campaigns/pause`. ([API docs](https://api.exoclick.com/v2/docs/))
- **PropellerAds** — plain REST, the simplest of all: one static API token
  (Profile page) as a bearer header, `PUT /v5/adv/campaigns/play` /
  `PUT /v5/adv/campaigns/stop`. API access may need some account history;
  ask support if the Profile page shows no token.
  ([API docs](https://ssp-api.propellerads.com/v5/docs/))
- **Meta (Facebook / Instagram)** — the Marketing API. One non-expiring
  system-user token with `ads_management`; a toggle is
  `POST graph.facebook.com/v25.0/<campaign_id>` with `status=ACTIVE|PAUSED`
  (`META_API_VERSION` overrides the version). See
  [Meta setup](#meta-facebook--instagram-setup).

Credentials are only required for networks your rules actually use — a
setup without Reddit never launches Chromium and doesn't need Reddit
credentials.

Go-live announcements are a separate, optional feature on the same poll loop:
set credentials for X, Bluesky, and/or Mastodon and the monitor posts the
Twitch link when the stream starts, then threads the YouTube link underneath
once the simulcast shows up. Leave those credentials unset and nothing
changes.

Two ways to drive it, usable together or on their own:

- **Polling daemon** (`run.py`) — a systemd service that polls the Twitch API
  on a timer. Needs Twitch API credentials. Setup below.
- **[OBS integration](#obs-integration-event-driven-no-polling)** — OBS's own
  start/stop events flip the campaigns the instant you go live, with **no
  Twitch API credentials at all**. Jump there if you don't want a daemon.

## Architecture (Reddit)

Two things conspire to make this awkward:

1. **Reddit's ads-api won't accept requests that don't *look* like real
   Chrome.** Plain `curl`/`requests` from scratch fails with 401 even when
   every visible header matches.
2. **Reddit's modern login is a Lit web component with shadow-DOM inputs**,
   which means a CLI HTTP client can't fill in the form.

So we use Selenium for what only a real browser can do (login form, cookie
persistence, CAPTCHA-clear), then extract the cookies + bearer (`token_v2` is
HttpOnly so JS can't see it but `driver.get_cookies()` can) and run the
actual PATCH from a `requests.Session`. If the API call fails, we fall back
to clicking the pause/resume button in the live dashboard via Selenium.

The bearer expires ~24h after issuance. On 401 we force-navigate to
ads.reddit.com to let the dashboard JS mint a fresh `token_v2`, re-extract,
and retry once.

## Architecture (announcements)

The two links don't become available at the same time. Twitch reports the
stream (and its title) on the poll that flips the channel online; the YouTube
simulcast only becomes addressable once the broadcast has been ingesting for a
while — often a minute or two, sometimes longer. Waiting for both would delay
the announcement past the point where it's useful, so:

1. The stream goes live → post the Twitch link immediately.
2. Keep checking `youtube.com/@handle/live` on its own timer
   (`YOUTUBE_LOOKUP_INTERVAL`, default 60s) until the broadcast appears or
   `YOUTUBE_LOOKUP_TIMEOUT` (default 30 min) runs out.
3. When it appears, post the YouTube link as a **reply** to the announcement
   on each platform, so it threads instead of landing as a context-free
   orphan post.

Set `ANNOUNCE_WAIT_FOR_YOUTUBE_SEC` if you'd rather hold the announcement for
a bit and get one post carrying both links.

Finding the YouTube link needs no API key: `@handle/live` already resolves to
whatever the channel is currently broadcasting. That page is a JavaScript
shell (its `<link rel="canonical">` is literally the string `"undefined"`), so
the client reads the embedded `ytInitialData` instead — the primary video's id
plus its live view counter. A *scheduled* broadcast is deliberately ignored;
its link would send viewers to a countdown. If YouTube ever reshapes that
page, the failure mode is a missing follow-up post, never a wrong link.

Two guarantees, since the failure modes here are public:

- **Never post twice for the same stream.** State is keyed by the Twitch
  stream id, and a stream that briefly reads as offline (the Twitch API does
  drop the odd poll) does not reset it. Across a *restart* the guarantee
  holds only if `ANNOUNCE_STATE_FILE` is set — that state is in memory
  otherwise, and `Restart=on-failure` will re-announce the live stream.
- **Never break ad control.** Every post and lookup is best-effort; a social
  API outage is logged and retried on a budget, and the campaign toggles
  carry on regardless.

## Quick start: the setup wizard

Rather than editing the env file by hand, run the interactive wizard. It walks
the whole setup in order — driver (daemon vs OBS), ad networks, keyword rules,
and optional announcements — prints exactly where to click for each token, and
writes a ready-to-use env file (and a `rules.yaml` if you need multiple rules).
It's static: it asks questions and writes files, talking to no network service
of its own. At the end it offers to install deps, bootstrap the Reddit session,
and verify the toggles.

```sh
python scripts/setup.py
```

The manual steps below are what the wizard automates; read them to understand
what it writes, or follow them directly if you'd rather not run the wizard.

## Install

Selenium needs a Chromium and a matching driver. Selenium Manager auto-fetches
chromedriver, but you still need Chromium itself on the host:

```sh
sudo apt-get install -y chromium-browser
# (or chromium / google-chrome — any Blink-based browser selenium can drive)
```dotenv

```sh
# 1. Create unprivileged service user
sudo useradd --system --no-create-home --shell /usr/sbin/nologin streaming-ad-monitor

# 2. Deploy via poetry
sudo cp -r . /opt/streaming-ad-monitor
sudo python3 -m venv /opt/streaming-ad-monitor/venv
sudo /opt/streaming-ad-monitor/venv/bin/pip install poetry
sudo /opt/streaming-ad-monitor/venv/bin/poetry --directory /opt/streaming-ad-monitor install --only main

# 3. Drop in your rules and secrets
sudo mkdir /etc/streaming-ad-monitor
sudo cp rules.example.yaml /etc/streaming-ad-monitor/rules.yaml  # then edit
sudo tee /etc/streaming-ad-monitor/env <<'EOF'
# Your channel name — always needed. Not a secret.
TWITCH_CHANNEL_LOGIN=...
# Twitch API credentials (https://dev.twitch.tv/console). Required by the
# polling daemon below. NOT needed for the OBS integration, which reads the
# title from Twitch's public endpoint — see "OBS integration".
TWITCH_CLIENT_ID=...
TWITCH_CLIENT_SECRET=...
# Where to persist the Reddit session between runs. With this set (see
# "One-time bootstrap"), no Reddit username/password is needed.
REDDIT_COOKIE_JAR=/var/lib/streaming-ad-monitor/reddit-session.json
# Only required when a rule targets Reddit and there's NO cookie jar above:
# REDDIT_USERNAME=...
# REDDIT_PASSWORD=...
# Optional: ads account id (ads.reddit.com/account/<id>/dashboard).
# Auto-discovered after login when unset.
# REDDIT_ADS_ACCOUNT_ID=...
# Required when any rule targets TrafficStars campaigns:
TRAFFICSTARS_API_KEY=...
# Required when any rule targets that network:
# EXOCLICK_API_TOKEN=...
# PROPELLERADS_API_TOKEN=...
# META_ACCESS_TOKEN=...
# Optional: announce the stream on X (see "Announcing the stream" below)
TWITTER_API_KEY=...
TWITTER_API_SECRET=...
TWITTER_ACCESS_TOKEN=...
TWITTER_ACCESS_TOKEN_SECRET=...
# Optional: announce the stream on Bluesky
BLUESKY_HANDLE=you.bsky.social
BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
# Optional: announce the stream on Mastodon (instance defaults to tech.lgbt)
MASTODON_ACCESS_TOKEN=...
# Optional: follow up with the YouTube link once the simulcast is up
YOUTUBE_CHANNEL_HANDLE=@yourhandle
# Required for the no-duplicate-posts guarantee: without it, a restart
# mid-stream re-announces the stream you are already on
ANNOUNCE_STATE_FILE=/var/lib/streaming-ad-monitor/announce-state.json
EOF
sudo chmod 640 /etc/streaming-ad-monitor/env
sudo chown root:streaming-ad-monitor /etc/streaming-ad-monitor/env
sudo install -d -o streaming-ad-monitor -m 700 /var/lib/streaming-ad-monitor
```

## One-time bootstrap

Do this once interactively from a desktop session that owns an X display, so
a real browser can establish the session:

```sh
cd /opt/streaming-ad-monitor
set -a; source /etc/streaming-ad-monitor/env; set +a
./venv/bin/python scripts/bootstrap_reddit_session.py
```

First it checks `$REDDIT_COOKIE_JAR`: if it already restores a live session,
it prints "already bootstrapped" and exits — safe to re-run anytime.

Otherwise a Chromium window opens and the script logs in automatically with
your configured credentials. It warms up via the reddit.com homepage, waits
for it to settle, then opens the login form and fills in your username and
password — cold-hitting `/login` directly trips Reddit's "blocked by network
security" page, and the homepage warm-up avoids it. If a transient block
still occurs it backs off and retries the automated flow (no manual login).
The one thing that needs you is a CAPTCHA: solve it in the window and press
ENTER. On success it writes the session (cookies + localStorage) to
`$REDDIT_COOKIE_JAR` and logs the discovered ads account id. From then on,
the daemon restores the session headlessly with no human in the loop.

If the automated login is blocked on every attempt, the egress IP is likely
flagged (VPN/datacenter reputation) — run the bootstrap from a residential
connection.

If the file path is owned by a user the daemon can't read, `chmod 644` it or
move it. The contents are sensitive (full Reddit session) — keep that in mind
when picking a path.

## Finding your Reddit campaign IDs

Navigate to a campaign in the ads dashboard. The URL will show
`.../dashboard/campaigns/2470329120103230906` — the trailing number is the
campaign ID. Put it in `rules.yaml` under `campaign_ids` (or
`REDDIT_CAMPAIGN_ID` for single-rule mode).

The bare `ads.reddit.com` host redirects to the account-scoped dashboard
(`ads.reddit.com/account/<id>/dashboard`) once you're logged in — the client
reads that `<id>` from the redirect automatically, so you don't normally need
to set `REDDIT_ADS_ACCOUNT_ID`. Set it only to pin a specific account when
your login has more than one.

**TrafficStars:** the numeric campaign ID is shown in the campaign list at
admin.trafficstars.com. Put it in `rules.yaml` under
`trafficstars_campaign_ids` (or `TRAFFICSTARS_CAMPAIGN_ID` for single-rule
mode).

**ExoClick / PropellerAds:** the numeric ID in each network's campaign list →
`exoclick_campaign_ids` / `propellerads_campaign_ids` (single-rule:
`EXOCLICK_CAMPAIGN_ID` / `PROPELLERADS_CAMPAIGN_ID`), with
`EXOCLICK_API_TOKEN` / `PROPELLERADS_API_TOKEN` in the env file.

**Meta:** Ads Manager → Campaigns → add the *Campaign ID* column →
`meta_campaign_ids` (single-rule: `META_CAMPAIGN_ID`).

### Meta (Facebook / Instagram) setup

1. Create an app of type *Business* at
   [developers.facebook.com/apps](https://developers.facebook.com/apps/) and
   add the Marketing API product.
2. [Business Settings → System users](https://business.facebook.com/settings/system-users)
   → add one, then *Assign assets* → your ad account → *Manage campaigns*.
3. *Generate new token* → pick the app → tick `ads_management` → set it as
   `META_ACCESS_TOKEN`. System-user tokens don't expire.

Pausing a campaign also pauses its ad sets and ads. Health-related ads can
get extra review on Meta (and on Reddit), so a resumed campaign may sit in
review before it delivers — test the round trip once before relying on it.

## Rules: one per stream topic

Streaming more than one topic — say Fight Health Insurance and Spark dev —
means one rule per topic:

```yaml
rules:
  - name: "FHI"
    keywords: ["FHI", "Fight Health Insurance"]
    exclude_keywords: ["Spark"]        # optional veto
    campaign_ids: ["<reddit fhi>"]
    meta_campaign_ids: ["<meta fhi>"]
  - name: "Spark dev"
    keywords: ["Spark", "PySpark"]
    exclude_keywords: ["FHI"]
    campaign_ids: ["<reddit spark>"]
    propellerads_campaign_ids: ["<propeller spark>"]
```

- **Whole-word matching.** Keywords match case-insensitively on whole words:
  "Spark" matches "Apache Spark", "#Spark", and "Spark's" but not "sparkle"
  or "SparkSQL". A multi-word keyword matches as a phrase. Add the variants you
  use (`PySpark`, `SparkSQL`) as keywords, or set `match: substring` on a rule
  for the old any-occurrence behaviour (`TRIGGER_MATCH=substring` in
  single-rule mode).
- **Exclude keywords** veto a rule: a title containing any of them never runs
  that rule's campaigns (`TRIGGER_EXCLUDE_KEYWORDS` in single-rule mode).
- **Shared campaigns.** A campaign listed under several rules runs while *any*
  of them matches, in both the polling daemon and the OBS gate — the rules
  never fight over it.
- **Failure isolation.** Each campaign toggle is independent: one network
  erroring is logged and retried on the next poll, and never stops the other
  campaigns from being paused or enabled.

The setup wizard (`python scripts/setup.py`) builds this file for you and can
make topics mutually exclusive automatically.

## Announcing the stream

Optional, and independent of the ad rules. Configure any combination of the
three platforms, or none.

### X (Twitter)

Posting uses the v2 API with OAuth 1.0a, so there are four static credentials
and no refresh dance.

1. Create a project + app at
   [developer.x.com](https://developer.x.com/en/portal/dashboard).
2. In the app's **User authentication settings**, set App permissions to
   **Read and write**. Do this *first* — tokens minted under read-only
   permission keep that scope, and posting fails with a 403 until you
   regenerate them.
3. From **Keys and tokens**, copy the API Key and Secret, then generate an
   Access Token and Secret **for the account that should appear as the
   author**, into:

```dotenv
TWITTER_API_KEY=...
TWITTER_API_SECRET=...
TWITTER_ACCESS_TOKEN=...
TWITTER_ACCESS_TOKEN_SECRET=...
```

Check the Developer Console for the write limits and pricing that apply to
your app — they have changed more than once. Two posts per stream is a small
ask of any of them. Separately, X rejects a post whose text duplicates a
recent one with a 403, which is worth knowing if you test with the same title
repeatedly.

### Bluesky

1. Settings → Privacy and security → **App passwords** → add one. Use the app
   password, never your account password.
2. Set your full handle (the one in your profile URL):

```dotenv
BLUESKY_HANDLE=you.bsky.social
BLUESKY_APP_PASSWORD=xxxx-xxxx-xxxx-xxxx
```

Self-hosting a PDS? Point `BLUESKY_PDS_URL` at it (default
`https://bsky.social`). It must be `https://` — it carries your app password —
except for `localhost`, where there is no network to sniff. Requests that
carry credentials don't follow redirects either: a same-host `https`→`http`
307 replays the request body, and on a login that body *is* the password.
Point the URL straight at the API host.

Links are posted with rich-text facets so they're clickable; Bluesky does not
auto-detect URLs in API posts.

### Mastodon

The simplest of the three: one access token, and it doesn't expire.

1. On your instance, Preferences → Development → **New application**. The
   `write:statuses` scope is all this needs — uncheck the rest.
2. Copy **Your access token** from the application's page:

```dotenv
MASTODON_ACCESS_TOKEN=...
```

The instance defaults to `https://tech.lgbt`; set `MASTODON_INSTANCE_URL` for
any other one. It must be `https://` (it carries your access token), except
for `localhost`. `MASTODON_VISIBILITY` takes `public` (default), `unlisted`,
`private`, or `direct`.

The post length limit is read from the instance on the first post rather than
assumed — the stock limit is 500, but forks routinely raise it (tech.lgbt runs
glitch-soc at 1024), so either constant would be wrong somewhere. Set
`MASTODON_MAX_CHARS` to pin it and skip the lookup.

Posts carry an idempotency key identifying the broadcast and which post it
is (announcement or YouTube follow-up), so a retry after a request that timed
out *after* the post landed is collapsed by the server rather than
double-posting — while two streams that happen to share a title stay
distinct, which keeps the second one's follow-up from threading onto the
first one's announcement.

### YouTube link

No API key, no Google Cloud project — set the channel whose `/live` page
should be watched:

```dotenv
YOUTUBE_CHANNEL_HANDLE=@yourhandle
```

`YOUTUBE_CHANNEL_ID=UC…` works too, and `YOUTUBE_LIVE_URL` takes an explicit
URL if your channel uses an older `/c/name` form. Leave all three unset and
announcements simply carry the Twitch link.

### Message format

Defaults:

```text
🔴 Live now: {title}

https://twitch.tv/<channel>
```

then, as a reply once the simulcast is up:

```text
Also streaming on YouTube: https://www.youtube.com/watch?v=…
```

Override with `ANNOUNCE_TEMPLATE` and `ANNOUNCE_YOUTUBE_TEMPLATE`.
Placeholders: `{title}`, `{channel}`, `{twitch_url}`, `{youtube_url}`, and
`{links}` (every link known at post time, one per line). An env file can't
hold a real newline, so write `\n` for a line break:

```dotenv
ANNOUNCE_TEMPLATE="🔴 Live now: {title}\n\n{links}"
```

Quote it. The verify step below sources this file with `set -a; source …`,
and unquoted, the shell stops the assignment at the first space and tries to
run `Live` as a command. The quotes also keep the `\n` literal for the daemon
to expand.

Other knobs: `ANNOUNCE_KEYWORDS` (comma-separated — only announce matching
titles; default announces every stream), `ANNOUNCE_WAIT_FOR_YOUTUBE_SEC`
(hold the announcement so one post can carry both links),
`ANNOUNCE_TITLE_MAX_CHARS`, and `ANNOUNCE_ENABLED=false` to switch the whole
thing off without removing credentials. `run.py`'s docstring lists them all.

## Verify

After bootstrap, verify the toggles work end-to-end before enabling the
daemon. The Reddit checks reuse the bootstrapped session, so only
`REDDIT_COOKIE_JAR` needs to be set — no username/password required (the ads
account id is loaded from the jar or discovered on the fly):

```sh
# Reddit: round-trip enable→disable, asserts configured_status changes
./venv/bin/python scripts/test_enable_disable.py <campaign_id>

# Reddit: or the longer disable→enable→disable flow
./venv/bin/python scripts/smoke_test_reddit.py <campaign_id> --log-level DEBUG

# TrafficStars: round-trip run→pause via the REST API
./venv/bin/python scripts/test_enable_disable_trafficstars.py <campaign_id>

# Any REST network (trafficstars, exoclick, propellerads, meta)
./venv/bin/python scripts/test_enable_disable_network.py meta <campaign_id>
```

All leave the campaign PAUSED on success.

If you configured announcements, check those too — finding out that an access
token is read-only is much nicer here than at the top of a stream:

```sh
# Render the announcement and probe the YouTube page. Posts nothing.
./venv/bin/python scripts/test_announce.py --dry-run

# Actually post to every configured platform (then delete the test posts).
./venv/bin/python scripts/test_announce.py --title "test post, ignore"
```dotenv

## Enable the service

```sh
sudo cp contrib/streaming-ad-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now streaming-ad-monitor
```

## OBS integration (event-driven, no polling)

The daemon above polls Twitch on a timer. If you'd rather flip the campaign the
instant you go live (and pause it the instant you stop), OBS can drive it
directly. Two pieces:

- **`contrib/obs_ad_control.py`** — an OBS script (load via **Tools → Scripts →
  +**). It hooks OBS's streaming start/stop events and shells out to the gate
  below. It never does network or Selenium work itself — that would run on OBS's
  UI thread and freeze the window — it only launches a detached subprocess in
  your venv and returns.
- **`scripts/obs_title_gate.py`** — the CLI the OBS script calls. On `started`
  it resolves the current title and enables the campaigns whose keywords match;
  on `stopped` it pauses every campaign. It's default-deny: anything it can't
  confirm as a live, on-topic stream stays paused, so ad spend never runs on an
  off-topic or mistitled stream. With `--watch` it stays running as a guard (see
  below).

### No Twitch credentials required

OBS knows *when* you go live but not your Twitch title, so the gate reads the
title from Twitch. By default it uses Twitch's **public** endpoint — the same
one the Twitch website uses — which needs **no developer app and no
credentials**, only your channel name (`TWITCH_CHANNEL_LOGIN`, which isn't a
secret). So the whole integration works with zero Twitch setup.

Two other title sources exist if you want them:

- `--title-source helix` reads from the official Helix API instead. This one
  *does* need credentials — register an app at <https://dev.twitch.tv/console>
  and set `TWITCH_CLIENT_ID` + `TWITCH_CLIENT_SECRET`.
- `--title "..."` (or the OBS "Manual title" source) skips Twitch entirely — no
  network, no credentials, but you supply the title yourself.

Either way, Twitch's API lags OBS by a few seconds when you go live, so the gate
polls until the channel reports live (up to `--twitch-timeout`, default 45s)
before reading the title.

### Watch mode (the crash-safe guard)

A normal **Stop Streaming** fires the `stopped` hook and pauses everything
instantly. But if OBS is force-killed it can't fire that hook — so with
**watch** on (the default), going live launches a detached **guard** instead of
a one-shot: it enables the matching campaign, then keeps polling Twitch and
**pauses the campaign the moment the stream actually ends**, then exits. Because
it polls independently, it tears the campaign down even if OBS crashed. Only one
guard runs at a time (a stop/restart supersedes the old one), and the `stopped`
hook also stops it for an instant clean teardown. The guard needs no always-on
daemon — it lives only for the duration of one stream.

### Wiring it up

1. Bootstrap the Reddit session and confirm the toggles work (the sections
   above).
2. Copy the sample env file and fill it in:

   ```sh
   cp contrib/obs-ad-control.env.example ~/.config/obs-ad-control.env
   chmod 600 ~/.config/obs-ad-control.env
   ```

   With `REDDIT_COOKIE_JAR` set, the gate runs headlessly off the saved session
   — no Reddit username/password needed.
3. In OBS, load `contrib/obs_ad_control.py` and set its properties:
   - **Python executable** — the interpreter in your venv (e.g.
     `~/.venvs/py313/bin/python`).
   - **streaming-ad-control directory** — your checkout.
   - **Env file** — the file from step 2.
   - **Title source** — leave on *Twitch — automatic, no credentials*.
   - Leave **Watch until the stream ends** on for crash-safe teardown.
4. Go live. Watch the OBS **Script Log** and the configured gate log file.

You can exercise the gate without OBS:

```sh
# went live — public (no-creds) auto title, watch until the stream ends
python scripts/obs_title_gate.py started --watch --env-file ~/.config/obs-ad-control.env

# went live — one-shot, public (no-creds) auto title
python scripts/obs_title_gate.py started --env-file ~/.config/obs-ad-control.env

# went live with an explicit title (no Twitch, no creds)
python scripts/obs_title_gate.py started --title "Apache Spark deep dive"

# stopped — stop the guard and pause everything (never needs Twitch)
python scripts/obs_title_gate.py stopped --env-file ~/.config/obs-ad-control.env
```

The polling daemon (`run.py`) still works as an alternative or an extra
backstop: it and the gate compute the *same* desired state from the *same*
title, so running both is safe — they don't fight.

## Long-running notes

- **`token_v2` rotation (~24h):** handled automatically. On 401, the client
  re-navigates to ads.reddit.com, lets the dashboard JS mint a fresh bearer,
  and retries. No action needed.
- **`reddit_session` expiry (~6 months):** when this happens, restored
  cookies stop working and the daemon falls back to form login, which can
  re-trip CAPTCHA on a headless service. Set a calendar reminder to re-run
  `bootstrap_reddit_session.py` every ~5 months.
- **2FA is not supported.** Use a non-2FA account dedicated to ads access.
- **PATCH body shape** is set to `{"data":{"configured_status":"PAUSED"}}` /
  `{"data":{"configured_status":"ACTIVE"}}` — verified against ads-api. If
  Reddit ever changes the schema, override via `REDDIT_PATCH_BODY_PAUSE` /
  `REDDIT_PATCH_BODY_RESUME` env vars without redeploying.
- **Resource cost.** Headless Chromium is ~200 MB resident. Fine for this
  workload (one PATCH per state edge, on the order of a few per day);
  excessive if you ever scale to dozens of accounts.
- **TrafficStars token rotation (~10h):** handled automatically. The bearer
  is renewed from the API key shortly before its `expires_in` deadline, and
  a mid-flight 401 triggers one re-auth + retry. The API key itself does not
  expire unless you regenerate it on the profile page.
- **Bluesky session rotation (~2h):** handled automatically. The access token
  is refreshed with the refresh token, and a failed refresh falls back to a
  fresh login from the app password, so uptime is unbounded. X's OAuth 1.0a
  credentials and the Mastodon access token are static and never rotate.
- **Set `ANNOUNCE_STATE_FILE` if you announce.** Announcement progress is
  otherwise in-memory only, so a restart mid-stream (`Restart=on-failure`
  does happen) re-announces the stream you're already on. The systemd unit's
  `StateDirectory=` already owns a writable path for it.
- **YouTube page shape.** The link lookup parses a public HTML page, not an
  API. If YouTube reshapes it, announcements keep working and simply stop
  carrying the YouTube link — check the logs for "not broadcasting" on a
  stream you know is simulcast. `scripts/test_announce.py --dry-run` probes
  the lookup on its own.
- **What a social outage costs you.** Nothing structural: each platform
  posts independently, so one being down doesn't stop the others; posts are
  retried on a bounded budget (5 announcement attempts, 3 follow-up attempts)
  and then abandoned for that broadcast; and ad control is unaffected either
  way. A platform that missed the announcement is skipped for the YouTube
  follow-up too, rather than being sent a context-free orphan post.
