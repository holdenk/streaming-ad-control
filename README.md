# streaming-ad-control

Monitors a Twitch stream and toggles ad campaigns based on the stream title.
Two ad networks are supported, each rule in `rules.yaml` can target either or
both:

- **Reddit Ads** — no official API access, so a headless Chromium handles
  login and cookie management, then a `requests.Session` (pre-loaded with the
  dashboard's `token_v2` bearer extracted via Selenium) PATCHes
  `ads-api.reddit.com` directly.
- **TrafficStars** — plain REST. The account API key (generate on
  [admin.trafficstars.com/profile](https://admin.trafficstars.com/profile/))
  acts as an OAuth2 refresh token against `POST /v1/auth/token`; campaigns
  are toggled with `PUT /v2/campaigns/run` / `PUT /v2/campaigns/pause`.
  No browser needed. ([API docs](https://docs.trafficstars.com/))

Credentials are only required for networks your rules actually use — a
TrafficStars-only setup never launches Chromium and doesn't need Reddit
credentials.

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

## Install

Selenium needs a Chromium and a matching driver. Selenium Manager auto-fetches
chromedriver, but you still need Chromium itself on the host:

```sh
sudo apt-get install -y chromium-browser
# (or chromium / google-chrome — any Blink-based browser selenium can drive)
```

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
TWITCH_CLIENT_ID=...
TWITCH_CLIENT_SECRET=...
TWITCH_CHANNEL_LOGIN=...
# Required when any rule targets Reddit campaigns:
REDDIT_USERNAME=...
REDDIT_PASSWORD=...
# Where to persist the Reddit session between runs
REDDIT_COOKIE_JAR=/var/lib/streaming-ad-monitor/reddit-session.json
# Optional: ads account id (ads.reddit.com/account/<id>/dashboard).
# Auto-discovered after login when unset.
# REDDIT_ADS_ACCOUNT_ID=...
# Required when any rule targets TrafficStars campaigns:
TRAFFICSTARS_API_KEY=...
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
```

All leave the campaign PAUSED on success.

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
