"""Reddit Ads client driven by a real Chromium via selenium.

Why selenium and not requests/curl: ads-api.reddit.com rejects requests that
don't carry the full browser auth signal (TLS fingerprint, in-memory bearer
attestation, etc.). A real browser presents all of those automatically.

Two-tier toggle strategy:
  1. **Primary (JS fetch).** Drive the browser to ads.reddit.com so we're in
     the dashboard's JS context, then `fetch('/api/v3/campaigns/{id}', ...)`
     from inside the page. The dashboard's auth flows attach to that fetch
     automatically — no header replication needed.
  2. **Fallback (UI click).** If the JS fetch returns non-2xx (e.g. body
     shape changed), navigate to the campaign and click the pause/resume
     button.

Session reuse: cookies + localStorage are persisted to ``cookie_jar_path``
and restored on subsequent runs so we don't re-login every poll cycle.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, NoReturn, Optional

import requests
from selenium import webdriver
from selenium.common.exceptions import (
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

# Walks the document and every shadow root looking for the first element
# matching the CSS selector. Reddit's login form puts inputs inside Lit
# components' shadow DOMs, so plain document.querySelector misses them.
_SHADOW_PIERCING_QUERY = """
const selector = arguments[0];
const stack = [document];
while (stack.length) {
    const node = stack.pop();
    if (node.querySelector) {
        const hit = node.querySelector(selector);
        if (hit) return hit;
    }
    const descendants = node.querySelectorAll
        ? node.querySelectorAll('*')
        : [];
    for (const el of descendants) {
        if (el.shadowRoot) stack.push(el.shadowRoot);
    }
}
return null;
"""

logger = logging.getLogger(__name__)

_LOGIN_URL = "https://www.reddit.com/login/"
_ADS_HOME = "https://ads.reddit.com/"
_ADS_API_BASE = "https://ads-api.reddit.com/api/v3"
_CAMPAIGN_DEEP_LINK = "https://ads.reddit.com/dashboard/campaigns/{campaign_id}"
_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36"
)

_LOGIN_TIMEOUT_SEC = 30
_PAGE_TIMEOUT_SEC = 30
# How long to wait after navigating to ads.reddit.com for the dashboard's JS
# to bootstrap and refresh the token_v2 cookie. Empirical; keep it short
# because this fires on the (rare) 401-retry path.
_TOKEN_REFRESH_SETTLE_SEC = 3

_CAPTCHA_TITLE_MARKERS = ("prove your humanity", "verify you are human")


class RedditCaptchaRequired(RuntimeError):
    """Raised when Reddit's login flow is gated by a CAPTCHA that needs a human."""


class RedditAdClient:
    """Toggles a Reddit campaign's status by driving the ads dashboard.

    Auth: username/password against reddit.com's login form. Cookies +
    localStorage are persisted between runs via ``cookie_jar_path``.

    Toggle: JS ``fetch()`` from inside ads.reddit.com (matches the
    dashboard's own request shape), with a UI-click fallback if the fetch
    returns a non-2xx status.
    """

    def __init__(
        self,
        username: str,
        password: str,
        *,
        cookie_jar_path: str = "",
        patch_body_pause: str = '{"data":{"configured_status":"PAUSED"}}',
        patch_body_resume: str = '{"data":{"configured_status":"ACTIVE"}}',
        headless: bool = True,
        driver: Optional[WebDriver] = None,
    ) -> None:
        if not username or not password:
            raise ValueError("RedditAdClient requires both username and password.")
        self.username = username
        self.password = password
        self.cookie_jar_path = cookie_jar_path
        self.patch_body_pause = patch_body_pause
        self.patch_body_resume = patch_body_resume
        self.headless = headless
        self._driver: Optional[WebDriver] = driver  # injected for tests
        self._http_session: Optional[requests.Session] = None
        self._authenticated = False

    # ------------------------------------------------------------------
    # Driver lifecycle
    # ------------------------------------------------------------------

    def _make_driver(self) -> WebDriver:
        """Build a Chromium driver. Selenium Manager fetches the binary."""
        opts = Options()
        if self.headless:
            opts.add_argument("--headless=new")
        opts.add_argument("--no-sandbox")  # systemd service, no user namespace
        opts.add_argument("--disable-dev-shm-usage")
        opts.add_argument("--disable-gpu")
        opts.add_argument("--window-size=1280,900")
        # Anti-bot evasion: hide selenium-tells that trigger Reddit's CAPTCHA.
        opts.add_argument("--disable-blink-features=AutomationControlled")
        opts.add_experimental_option("excludeSwitches", ["enable-automation"])
        opts.add_experimental_option("useAutomationExtension", False)
        # Match the UA the working dashboard request used.
        opts.add_argument(f"--user-agent={_USER_AGENT}")
        driver = webdriver.Chrome(options=opts)
        driver.set_page_load_timeout(_PAGE_TIMEOUT_SEC)
        # Patch out the navigator.webdriver flag at the CDP level — survives
        # navigations, which the in-page JS hook from add_blink_feature does not.
        try:
            driver.execute_cdp_cmd(
                "Page.addScriptToEvaluateOnNewDocument",
                {
                    "source": "Object.defineProperty(navigator, 'webdriver', "
                    "{get: () => undefined});"
                },
            )
        except WebDriverException:
            logger.debug("CDP webdriver patch not supported; continuing.", exc_info=True)
        return driver

    @property
    def driver(self) -> WebDriver:
        if self._driver is None:
            self._driver = self._make_driver()
        return self._driver

    def close(self) -> None:
        """Tear down the browser and HTTP session. Idempotent."""
        if self._http_session is not None:
            try:
                self._http_session.close()
            except Exception:
                logger.debug("HTTP session close raised; ignoring.", exc_info=True)
            self._http_session = None
        if self._driver is not None:
            try:
                self._driver.quit()
            except WebDriverException:
                logger.debug("Driver quit raised; ignoring.", exc_info=True)
            self._driver = None
        self._authenticated = False

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Restore a saved session if possible, otherwise log in via the form."""
        if self._authenticated:
            return

        if self._restore_session():
            logger.info("Reddit session restored from %s.", self.cookie_jar_path)
            self._authenticated = True
            return

        self._login_via_form()
        self._save_session()
        self._authenticated = True

    def _restore_session(self) -> bool:
        """Load cookies + localStorage from disk; verify by hitting the dashboard."""
        if not self.cookie_jar_path:
            return False
        try:
            with open(self.cookie_jar_path, "r", encoding="utf-8") as fh:
                jar = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return False

        cookies = jar.get("cookies", [])
        local_storage = jar.get("local_storage", {})
        if not cookies:
            return False

        # Selenium requires you to be on a domain before adding its cookies.
        self.driver.get(_ADS_HOME)
        for cookie in cookies:
            try:
                self.driver.add_cookie(cookie)
            except WebDriverException:
                logger.debug("Skipping cookie %r (driver rejected).", cookie.get("name"))

        if local_storage:
            self.driver.execute_script(
                "for (const [k,v] of Object.entries(arguments[0])) "
                "{ window.localStorage.setItem(k, v); }",
                local_storage,
            )

        self.driver.get(_ADS_HOME)
        return self._is_logged_in()

    def _save_session(self) -> None:
        """Persist cookies + localStorage to disk (if cookie_jar_path is set)."""
        if not self.cookie_jar_path:
            return
        cookies = self.driver.get_cookies()
        local_storage = self.driver.execute_script(
            "const out = {};"
            "for (let i = 0; i < window.localStorage.length; i++) {"
            "  const k = window.localStorage.key(i);"
            "  out[k] = window.localStorage.getItem(k);"
            "} return out;"
        )
        try:
            with open(self.cookie_jar_path, "w", encoding="utf-8") as fh:
                json.dump({"cookies": cookies, "local_storage": local_storage}, fh)
        except OSError as exc:
            logger.warning("Could not persist cookie jar to %s: %s", self.cookie_jar_path, exc)

    def _is_logged_in(self) -> bool:
        """Heuristic: dashboard navigates back to login if we're not logged in."""
        try:
            current = self.driver.current_url
        except WebDriverException:
            return False
        return "ads.reddit.com" in current and "login" not in current.lower()

    def _login_via_form(self) -> None:
        logger.info("Reddit login: submitting credentials for user '%s'.", self.username)
        self.driver.get(_LOGIN_URL)
        if self._is_captcha_page():
            raise RedditCaptchaRequired(
                "Reddit served a CAPTCHA challenge before the login form."
            )

        # The form inputs live inside Lit web components' shadow DOMs;
        # selenium's CSS selectors don't pierce shadow boundaries, so we use
        # JS to walk every shadow root.
        username_field = self._wait_for_shadow_element(
            "input[name='username']", _LOGIN_TIMEOUT_SEC
        )
        if username_field is None:
            if self._is_captcha_page():
                raise RedditCaptchaRequired(
                    "Reddit served a CAPTCHA before the login form rendered."
                )
            self._raise_with_diagnostics(
                "could not find username field on login page",
                RuntimeError("username input not in DOM"),
            )

        password_field = self._find_in_shadow_dom("input[name='password']")
        if password_field is None:
            self._raise_with_diagnostics(
                "could not find password field on login page",
                RuntimeError("password input not in DOM"),
            )

        username_field.clear()
        username_field.send_keys(self.username)
        password_field.clear()
        password_field.send_keys(self.password)
        # The Lit form components don't have a plain submit button selenium
        # can find with CSS; pressing Enter on the password field triggers
        # the form's submit handler reliably.
        password_field.send_keys(Keys.RETURN)

        # Wait for redirect off the login page or for an error to surface.
        wait = WebDriverWait(self.driver, _LOGIN_TIMEOUT_SEC)
        try:
            wait.until(lambda d: "login" not in d.current_url.lower())
        except TimeoutException as exc:
            if self._is_captcha_page():
                raise RedditCaptchaRequired(
                    "Reddit served a CAPTCHA after the login form was submitted."
                ) from exc
            self._raise_with_diagnostics(
                "login submitted but didn't redirect off /login (bad creds, CAPTCHA, or 2FA)",
                exc,
            )

        self.driver.get(_ADS_HOME)
        if not self._is_logged_in():
            self._raise_with_diagnostics(
                "redirected off /login but ads dashboard isn't accessible",
                RuntimeError("dashboard inaccessible"),
            )

    def _is_captcha_page(self) -> bool:
        try:
            title = (self.driver.title or "").lower()
        except WebDriverException:
            return False
        return any(marker in title for marker in _CAPTCHA_TITLE_MARKERS)

    def _find_in_shadow_dom(self, css_selector: str):
        """Walk all shadow roots looking for the first match. None if absent."""
        return self.driver.execute_script(_SHADOW_PIERCING_QUERY, css_selector)

    def _wait_for_shadow_element(self, css_selector: str, timeout: float):
        """Poll _find_in_shadow_dom until it returns a non-null element or timeout."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            elem = self._find_in_shadow_dom(css_selector)
            if elem is not None:
                return elem
            time.sleep(0.5)
        return None

    def _raise_with_diagnostics(self, message: str, cause: BaseException) -> NoReturn:
        """Capture URL, title, and screenshot before re-raising with context."""
        url = title = "<unavailable>"
        try:
            url = self.driver.current_url
            title = self.driver.title
        except WebDriverException:
            pass
        shot_path = "/tmp/reddit_login_failure.png"
        try:
            self.driver.save_screenshot(shot_path)
            shot_msg = f"; screenshot saved to {shot_path}"
        except WebDriverException:
            shot_msg = ""
        raise RuntimeError(
            f"Reddit login failed: {message}. Page URL={url!r}, title={title!r}{shot_msg}."
        ) from cause

    # ------------------------------------------------------------------
    # Campaign control
    # ------------------------------------------------------------------

    def enable_campaign(self, campaign_id: str) -> dict:
        """Set the campaign status to ACTIVE."""
        logger.info("Enabling Reddit campaign %s.", campaign_id)
        return self._set_campaign_state(
            campaign_id, body=self.patch_body_resume, event_type="manual_activate"
        )

    def disable_campaign(self, campaign_id: str) -> dict:
        """Set the campaign status to PAUSED."""
        logger.info("Disabling Reddit campaign %s.", campaign_id)
        return self._set_campaign_state(
            campaign_id, body=self.patch_body_pause, event_type="manual_pause"
        )

    def _set_campaign_state(self, campaign_id: str, body: str, event_type: str) -> dict:
        self.authenticate()
        if self._http_session is None:
            self._http_session = self._build_http_session()

        result = self._patch_via_requests(campaign_id, body, event_type)

        # 401 means the bearer rotated. Reddit's token_v2 cookie refreshes on
        # any dashboard navigation; pull the new value and retry once.
        if result["status"] == 401:
            logger.info(
                "Campaign %s: 401 from ads-api; refreshing bearer from browser.",
                campaign_id,
            )
            self._refresh_http_session()
            result = self._patch_via_requests(campaign_id, body, event_type)

        if 200 <= result["status"] < 300:
            logger.debug("Campaign %s: PATCH ok (%d).", campaign_id, result["status"])
            return self._parse_body(result["body"])

        logger.warning(
            "Campaign %s: requests PATCH returned %d (%s); falling back to UI click.",
            campaign_id,
            result["status"],
            (result["body"] or "")[:200],
        )
        self._click_toggle(campaign_id, event_type=event_type)
        return {"status": result["status"], "fallback": "ui_click"}

    def _build_http_session(self) -> requests.Session:
        """Mint a requests.Session pre-loaded with cookies + bearer from the browser.

        Uses ``driver.get_cookies()`` which sees HttpOnly cookies (notably
        ``token_v2`` and ``reddit_session``) that JS in-page can't read.
        """
        # Make sure we've actually visited ads.reddit.com so the dashboard's
        # cookies are present in the jar.
        if "ads.reddit.com" not in (self.driver.current_url or ""):
            self.driver.get(_ADS_HOME)

        cookies = self.driver.get_cookies()
        token_v2 = next((c["value"] for c in cookies if c["name"] == "token_v2"), "")
        if not token_v2:
            raise RuntimeError(
                "token_v2 cookie missing after authentication. The browser "
                "session looks logged out."
            )

        session = requests.Session()
        for ck in cookies:
            session.cookies.set(
                ck["name"], ck["value"],
                domain=ck.get("domain"),
                path=ck.get("path", "/"),
            )
        session.headers.update({
            "Authorization": f"Bearer {token_v2}",
            "Origin": _ADS_HOME.rstrip("/"),
            "Referer": _ADS_HOME,
            "User-Agent": _USER_AGENT,
            "sec-ch-ua": '"Chromium";v="145", "Not:A-Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Linux"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
        })
        return session

    def _refresh_http_session(self) -> None:
        """Force a fresh token_v2 from the browser and rebuild the session.

        Reddit's dashboard JS auto-refreshes ``token_v2`` shortly after page
        load. Without a navigation, ``driver.get_cookies()`` returns the
        already-stale value we got 401'd with — refresh would be a no-op.
        Force-loading ``ads.reddit.com`` and waiting briefly for the JS to
        settle gets us a freshly minted bearer.
        """
        if self._http_session is not None:
            try:
                self._http_session.close()
            except Exception:
                pass
            self._http_session = None
        self.driver.get(_ADS_HOME)
        time.sleep(_TOKEN_REFRESH_SETTLE_SEC)
        self._http_session = self._build_http_session()

    def _patch_via_requests(self, campaign_id: str, body: str, event_type: str) -> dict:
        """PATCH the campaign via requests.Session. Returns {status, body}."""
        assert self._http_session is not None
        url = f"{_ADS_API_BASE}/campaigns/{campaign_id}"
        try:
            r = self._http_session.patch(
                url,
                data=body.encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "X-Event-Type": event_type,
                },
                timeout=_PAGE_TIMEOUT_SEC,
            )
        except requests.RequestException as exc:
            logger.warning("requests PATCH raised: %r", exc)
            return {"status": 0, "body": str(exc)}
        return {"status": r.status_code, "body": r.text}

    def _click_toggle(self, campaign_id: str, event_type: str) -> None:
        """UI fallback: navigate to the campaign and click pause/resume."""
        wanted_action = "resume" if event_type == "manual_activate" else "pause"
        self.driver.get(_CAMPAIGN_DEEP_LINK.format(campaign_id=campaign_id))
        wait = WebDriverWait(self.driver, _PAGE_TIMEOUT_SEC)

        # Try several plausible selectors. The dashboard's exact selectors
        # change; pick the first that matches a clickable element whose
        # accessible label or text mentions pause/resume.
        candidates = [
            (By.CSS_SELECTOR, f"button[aria-label*='{wanted_action}' i]"),
            (By.CSS_SELECTOR, f"button[data-testid*='{wanted_action}' i]"),
            (By.XPATH, f"//button[contains(translate(., 'PAUSERME', 'pauserme'), '{wanted_action}')]"),
        ]
        for by, sel in candidates:
            try:
                btn = wait.until(EC.element_to_be_clickable((by, sel)))
                btn.click()
                # Brief settle — the dashboard typically shows a confirmation toast.
                time.sleep(1.0)
                return
            except (TimeoutException, NoSuchElementException):
                continue
        raise RuntimeError(
            f"UI fallback: could not find a '{wanted_action}' button for "
            f"campaign {campaign_id}. The dashboard DOM may have changed."
        )

    @staticmethod
    def _parse_body(body: str) -> Any:
        try:
            return json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return {"raw": body}
