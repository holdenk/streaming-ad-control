"""Tests for stream_ad_monitor.reddit_ad_client.

The selenium WebDriver is mocked; tests assert the script payload sent to
``execute_async_script`` and the UI-click fallback path.
"""

import json
from unittest.mock import MagicMock

import pytest

from stream_ad_monitor.reddit_ad_client import (
    RedditAdClient,
    RedditBlockedError,
)


_CAMPAIGN_ID = "2470329120103230906"


def _fake_driver(
    *,
    fetch_status: int = 200,
    fetch_body: str = '{"ok": true}',
    current_url: str = "https://ads.reddit.com/account/acct123/dashboard",
):
    """Build a mock WebDriver whose execute_async_script returns a fetch result."""
    driver = MagicMock()
    driver.current_url = current_url
    driver.title = "reddit"
    driver.page_source = "<html></html>"
    driver.execute_async_script.return_value = {
        "status": fetch_status,
        "body": fetch_body,
    }
    driver.execute_script.return_value = {}
    driver.get_cookies.return_value = []
    driver.get.return_value = None
    return driver


def _make_client(driver=None, **kwargs):
    return RedditAdClient(
        username=kwargs.pop("username", "u"),
        password=kwargs.pop("password", "p"),
        driver=driver if driver is not None else _fake_driver(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_init_rejects_missing_credentials():
    with pytest.raises(ValueError, match="username and password"):
        RedditAdClient(username="", password="p")
    with pytest.raises(ValueError, match="username and password"):
        RedditAdClient(username="u", password="")


def test_init_allows_cookie_jar_without_credentials():
    """A cookie jar is enough to construct the client — creds become optional."""
    client = RedditAdClient(username="", password="", cookie_jar_path="/tmp/jar.json")
    assert client.cookie_jar_path == "/tmp/jar.json"


def test_authenticate_raises_when_restore_fails_and_no_credentials(tmp_path, monkeypatch):
    """Jar-only mode: a dead restore must fail loudly, not attempt a login."""
    driver = _fake_driver(current_url="https://www.reddit.com/login/")
    client = RedditAdClient(
        username="", password="", cookie_jar_path=str(tmp_path / "missing.json"),
        driver=driver,
    )
    login = MagicMock(name="login")
    monkeypatch.setattr(client, "_login_via_form", login)

    with pytest.raises(RuntimeError, match="bootstrap_reddit_session"):
        client.authenticate()
    login.assert_not_called()


# ---------------------------------------------------------------------------
# Authentication flows
# ---------------------------------------------------------------------------


def test_authenticate_is_idempotent_when_already_authenticated():
    driver = _fake_driver()
    client = _make_client(driver=driver)
    client._authenticated = True
    client.authenticate()
    driver.get.assert_not_called()  # no login flow re-runs


def test_authenticate_no_cookie_jar_falls_through_to_form_login(monkeypatch):
    """Without cookie_jar_path, restore is skipped and login is attempted."""
    driver = _fake_driver(current_url="https://ads.reddit.com/dashboard")
    client = _make_client(driver=driver)
    monkeypatch.setattr(client, "_login_via_form", MagicMock(name="login"))
    client.authenticate()
    client._login_via_form.assert_called_once()
    assert client._authenticated is True


def test_authenticate_restores_session_when_cookie_jar_loads_and_dashboard_accepts(
    tmp_path, monkeypatch
):
    jar = tmp_path / "jar.json"
    jar.write_text(json.dumps({"cookies": [{"name": "reddit_session", "value": "x"}], "local_storage": {}}))
    driver = _fake_driver(current_url="https://ads.reddit.com/account/acct123/dashboard")
    client = _make_client(driver=driver, cookie_jar_path=str(jar))
    monkeypatch.setattr(client, "_login_via_form", MagicMock(name="login"))

    client.authenticate()

    client._login_via_form.assert_not_called()
    assert client._authenticated is True


def test_authenticate_falls_back_to_form_when_restored_session_is_dead(
    tmp_path, monkeypatch
):
    jar = tmp_path / "jar.json"
    jar.write_text(json.dumps({"cookies": [{"name": "reddit_session", "value": "x"}], "local_storage": {}}))
    # Restored cookies but the dashboard kicks us back to /login.
    driver = _fake_driver(current_url="https://www.reddit.com/login/")
    client = _make_client(driver=driver, cookie_jar_path=str(jar))
    monkeypatch.setattr(client, "_login_via_form", MagicMock(name="login"))
    monkeypatch.setattr(client, "_save_session", MagicMock(name="save"))

    client.authenticate()

    client._login_via_form.assert_called_once()
    client._save_session.assert_called_once()


def test_authenticate_skips_restore_when_jar_file_missing(tmp_path, monkeypatch):
    nonexistent = tmp_path / "missing.json"
    driver = _fake_driver()
    client = _make_client(driver=driver, cookie_jar_path=str(nonexistent))
    monkeypatch.setattr(client, "_login_via_form", MagicMock(name="login"))
    client.authenticate()
    client._login_via_form.assert_called_once()


# ---------------------------------------------------------------------------
# Campaign control – requests-based PATCH path
# ---------------------------------------------------------------------------


def _stub_http_session(client, status=200, body='{"ok":true}'):
    """Pre-install a mock requests.Session on the client so _build_http_session
    isn't called and the test asserts on the mock's .patch() invocation."""
    mock_response = MagicMock(name="response")
    mock_response.status_code = status
    mock_response.text = body
    session = MagicMock(name="http_session")
    session.patch.return_value = mock_response
    client._http_session = session
    return session


def test_disable_campaign_patches_with_pause_event_type():
    client = _make_client()
    client._authenticated = True
    session = _stub_http_session(client, body='{"id":1}')

    client.disable_campaign(_CAMPAIGN_ID)

    args, kwargs = session.patch.call_args
    url = args[0]
    assert url == f"https://ads-api.reddit.com/api/v3/campaigns/{_CAMPAIGN_ID}"
    assert b"PAUSED" in kwargs["data"]
    assert kwargs["headers"]["X-Event-Type"] == "manual_pause"
    assert kwargs["headers"]["Content-Type"] == "application/json"


def test_enable_campaign_patches_with_activate_event_type():
    client = _make_client()
    client._authenticated = True
    session = _stub_http_session(client)

    client.enable_campaign(_CAMPAIGN_ID)

    _, kwargs = session.patch.call_args
    assert b"ACTIVE" in kwargs["data"]
    assert kwargs["headers"]["X-Event-Type"] == "manual_activate"


def test_authenticate_called_lazily_on_first_state_change(monkeypatch):
    driver = _fake_driver()
    client = _make_client(driver=driver)
    monkeypatch.setattr(client, "_login_via_form", MagicMock(name="login"))
    monkeypatch.setattr(client, "_save_session", MagicMock(name="save"))
    monkeypatch.setattr(
        client, "_build_http_session", MagicMock(return_value=_stub_http_session(client))
    )

    assert client._authenticated is False
    client.disable_campaign(_CAMPAIGN_ID)
    assert client._authenticated is True
    client._login_via_form.assert_called_once()


def test_custom_patch_body_overrides_default():
    client = _make_client(
        patch_body_pause='{"effective_status":"MANUALLY_PAUSED"}',
        patch_body_resume='{"effective_status":"ACTIVE"}',
    )
    client._authenticated = True
    session = _stub_http_session(client)

    client.disable_campaign(_CAMPAIGN_ID)

    _, kwargs = session.patch.call_args
    assert kwargs["data"] == b'{"effective_status":"MANUALLY_PAUSED"}'


def test_401_triggers_bearer_refresh_then_retry(monkeypatch):
    """A 401 from ads-api should rebuild the http session and retry once."""
    client = _make_client()
    client._authenticated = True

    first = MagicMock(status_code=401, text='{"error":"expired"}')
    second = MagicMock(status_code=200, text='{"ok":true}')
    session = MagicMock(name="http_session")
    session.patch.side_effect = [first, second]
    client._http_session = session

    refresh = MagicMock(name="refresh")
    monkeypatch.setattr(client, "_refresh_http_session", refresh)

    client.disable_campaign(_CAMPAIGN_ID)

    refresh.assert_called_once()
    assert session.patch.call_count == 2


def test_refresh_http_session_navigates_before_rebuild(monkeypatch):
    """Token refresh must force-navigate to ads.reddit.com before re-extracting
    cookies; otherwise ``driver.get_cookies()`` returns the same stale token
    we just got 401'd with."""
    driver = _fake_driver(current_url="https://ads.reddit.com/dashboard")
    client = _make_client(driver=driver)

    # Track call order: navigate must happen before _build_http_session reads
    # cookies. We capture the call sequence and assert the ordering.
    calls: list[str] = []
    driver.get.side_effect = lambda url: calls.append(f"get({url})")
    monkeypatch.setattr(
        client,
        "_build_http_session",
        MagicMock(side_effect=lambda: calls.append("build") or MagicMock()),
    )
    monkeypatch.setattr("stream_ad_monitor.reddit_ad_client.time.sleep", lambda _: None)

    client._refresh_http_session()

    # Navigate happens before rebuild — re-extracting from the browser is
    # pointless if the page hasn't been reloaded to mint a fresh token.
    assert calls == ["get(https://ads.reddit.com/)", "build"]


# ---------------------------------------------------------------------------
# Campaign control – UI-click fallback
# ---------------------------------------------------------------------------


def test_non_2xx_falls_back_to_ui_click(monkeypatch):
    client = _make_client()
    client._authenticated = True
    _stub_http_session(client, status=400, body="bad body shape")
    click = MagicMock(name="click")
    monkeypatch.setattr(client, "_click_toggle", click)

    result = client.disable_campaign(_CAMPAIGN_ID)

    click.assert_called_once_with(_CAMPAIGN_ID, event_type="manual_pause")
    assert result == {"status": 400, "fallback": "ui_click"}


def test_5xx_falls_back_to_ui_click(monkeypatch):
    client = _make_client()
    client._authenticated = True
    _stub_http_session(client, status=500, body="server error")
    monkeypatch.setattr(client, "_click_toggle", MagicMock(name="click"))

    client.enable_campaign(_CAMPAIGN_ID)

    client._click_toggle.assert_called_once_with(
        _CAMPAIGN_ID, event_type="manual_activate"
    )


def test_network_exception_falls_back_to_ui_click(monkeypatch):
    """When requests.Session.patch raises (network error), fall back to UI click."""
    import requests as _requests

    client = _make_client()
    client._authenticated = True
    session = MagicMock(name="http_session")
    session.patch.side_effect = _requests.RequestException("connection refused")
    client._http_session = session
    monkeypatch.setattr(client, "_click_toggle", MagicMock(name="click"))

    client.disable_campaign(_CAMPAIGN_ID)

    client._click_toggle.assert_called_once()


# ---------------------------------------------------------------------------
# Driver lifecycle
# ---------------------------------------------------------------------------


def test_close_quits_driver_and_resets_auth():
    driver = _fake_driver()
    client = _make_client(driver=driver)
    client._authenticated = True
    client.close()
    driver.quit.assert_called_once()
    assert client._authenticated is False
    assert client._driver is None


def test_close_is_idempotent():
    driver = _fake_driver()
    client = _make_client(driver=driver)
    client.close()
    client.close()  # must not raise


# ---------------------------------------------------------------------------
# Session persistence
# ---------------------------------------------------------------------------


def test_save_session_writes_cookies_and_local_storage(tmp_path):
    jar = tmp_path / "jar.json"
    driver = _fake_driver()
    driver.get_cookies.return_value = [{"name": "reddit_session", "value": "abc"}]
    driver.execute_script.return_value = {"theme": "dark"}
    client = _make_client(driver=driver, cookie_jar_path=str(jar), ads_account_id="acct123")
    client._save_session()

    written = json.loads(jar.read_text())
    assert written["cookies"][0]["name"] == "reddit_session"
    assert written["local_storage"] == {"theme": "dark"}
    assert written["ads_account_id"] == "acct123"


def test_restore_session_loads_account_id_from_jar(tmp_path):
    """A jar with a saved account id scopes navigation without a redirect."""
    jar = tmp_path / "jar.json"
    jar.write_text(json.dumps({
        "cookies": [{"name": "reddit_session", "value": "x"}],
        "local_storage": {},
        "ads_account_id": "acct_from_jar",
    }))
    driver = _fake_driver(current_url="https://ads.reddit.com/account/acct_from_jar/dashboard")
    client = _make_client(driver=driver, cookie_jar_path=str(jar))

    assert client._restore_session() is True
    assert client.ads_account_id == "acct_from_jar"
    # Navigated to the account-scoped dashboard, not the bare host.
    assert any(
        "account/acct_from_jar" in c.args[0] for c in driver.get.call_args_list
    )


def test_settle_ads_dashboard_returns_on_account_url(monkeypatch):
    driver = _fake_driver(current_url="https://ads.reddit.com/account/acct123/dashboard")
    client = _make_client(driver=driver)
    slept = []
    monkeypatch.setattr(
        "stream_ad_monitor.reddit_ad_client.time.sleep", lambda s: slept.append(s)
    )
    client._settle_ads_dashboard()
    assert slept == []  # decisive URL already present → no polling


def test_save_session_no_op_without_jar_path():
    driver = _fake_driver()
    client = _make_client(driver=driver, cookie_jar_path="")
    client._save_session()  # must not raise, must not call get_cookies
    driver.get_cookies.assert_not_called()


# ---------------------------------------------------------------------------
# Account-scoped dashboard URLs
# ---------------------------------------------------------------------------


def test_ads_home_is_account_scoped_when_account_id_set():
    client = _make_client(ads_account_id="acct123")
    assert client._ads_home_url == "https://ads.reddit.com/account/acct123/dashboard"


def test_ads_home_falls_back_to_bare_host_without_account_id():
    client = _make_client()
    assert client._ads_home_url == "https://ads.reddit.com/"


def test_campaign_deep_link_is_account_scoped():
    client = _make_client(ads_account_id="acct123")
    assert client._campaign_url("777") == (
        "https://ads.reddit.com/account/acct123/dashboard/campaigns/777"
    )


def test_refresh_http_session_navigates_to_account_dashboard(monkeypatch):
    driver = _fake_driver(current_url="https://ads.reddit.com/account/acct123/dashboard")
    client = _make_client(driver=driver, ads_account_id="acct123")
    monkeypatch.setattr(client, "_build_http_session", MagicMock(return_value=MagicMock()))
    monkeypatch.setattr("stream_ad_monitor.reddit_ad_client.time.sleep", lambda _: None)

    client._refresh_http_session()

    driver.get.assert_called_once_with("https://ads.reddit.com/account/acct123/dashboard")


# ---------------------------------------------------------------------------
# Login flow: homepage warm-up + network-security block detection
# ---------------------------------------------------------------------------


def _no_sleep(monkeypatch):
    monkeypatch.setattr("stream_ad_monitor.reddit_ad_client.time.sleep", lambda _: None)


def test_login_warms_up_via_homepage_before_login_page(monkeypatch):
    """Cold-hitting /login trips Reddit's block page; the homepage must load first."""
    _no_sleep(monkeypatch)
    driver = _fake_driver(current_url="https://ads.reddit.com/account/acct123/dashboard")
    driver.get_cookie.return_value = {"name": "reddit_session", "value": "x"}
    client = _make_client(driver=driver, ads_account_id="acct123")

    username_field, password_field = MagicMock(), MagicMock()

    def find(selector):
        if "username" in selector:
            return username_field
        if "password" in selector:
            return password_field
        return None  # no login control found → direct /login navigation

    monkeypatch.setattr(client, "_find_in_shadow_dom", find)

    client._login_via_form()

    gets = [c.args[0] for c in driver.get.call_args_list]
    assert gets == [
        "https://www.reddit.com/",
        "https://www.reddit.com/login/",
        "https://ads.reddit.com/",  # bare host; redirect + discovery happen here
    ]
    username_field.send_keys.assert_called_once_with("u")


def test_login_dwells_on_homepage_before_navigating(monkeypatch):
    """A settle delay must happen after the homepage load and before /login."""
    calls = []
    monkeypatch.setattr(
        "stream_ad_monitor.reddit_ad_client.time.sleep",
        lambda s: calls.append(("sleep", s)),
    )
    driver = _fake_driver(current_url="https://ads.reddit.com/account/acct123/dashboard")
    driver.get_cookie.return_value = {"name": "reddit_session", "value": "x"}
    driver.get.side_effect = lambda url: calls.append(("get", url))
    client = _make_client(driver=driver)
    monkeypatch.setattr(client, "_find_in_shadow_dom", lambda sel: MagicMock())
    monkeypatch.setattr(client, "_wait_for_shadow_element", lambda sel, t: MagicMock())

    client._login_via_form()

    # First homepage load, then a settle sleep, before anything else navigates.
    assert calls[0] == ("get", "https://www.reddit.com/")
    assert calls[1][0] == "sleep" and calls[1][1] > 0


def test_login_clicks_homepage_login_control_when_present(monkeypatch):
    _no_sleep(monkeypatch)
    driver = _fake_driver(current_url="https://ads.reddit.com/account/acct123/dashboard")
    driver.get_cookie.return_value = {"name": "reddit_session", "value": "x"}
    client = _make_client(driver=driver, ads_account_id="acct123")

    login_control = MagicMock(name="login_control")
    username_field, password_field = MagicMock(), MagicMock()

    def find(selector):
        if "login" in selector:
            return login_control
        if "username" in selector:
            return username_field
        if "password" in selector:
            return password_field
        return None

    monkeypatch.setattr(client, "_find_in_shadow_dom", find)

    client._login_via_form()

    login_control.click.assert_called_once()
    gets = [c.args[0] for c in driver.get.call_args_list]
    assert "https://www.reddit.com/login/" not in gets  # clicked, not cold-hit


def test_login_raises_blocked_error_on_network_security_page(monkeypatch):
    _no_sleep(monkeypatch)
    driver = _fake_driver()
    driver.page_source = "<html>You've been blocked by network security.</html>"
    client = _make_client(driver=driver)

    with pytest.raises(RedditBlockedError, match="network security"):
        client._login_via_form()


# ---------------------------------------------------------------------------
# Account-id auto-discovery
# ---------------------------------------------------------------------------


def test_account_id_discovered_from_dashboard_url():
    driver = _fake_driver(
        current_url="https://ads.reddit.com/account/gun0aswfhklt/dashboard"
    )
    client = _make_client(driver=driver)  # no id configured
    assert client._is_logged_in() is True
    assert client.ads_account_id == "gun0aswfhklt"


def test_configured_account_id_is_not_overwritten_by_discovery():
    driver = _fake_driver(
        current_url="https://ads.reddit.com/account/other_acct/dashboard"
    )
    client = _make_client(driver=driver, ads_account_id="pinned_acct")
    client._is_logged_in()
    assert client.ads_account_id == "pinned_acct"


def test_no_account_id_captured_from_business_redirect():
    driver = _fake_driver(current_url="https://www.business.reddit.com/")
    client = _make_client(driver=driver)
    assert client._is_logged_in() is False
    assert client.ads_account_id == ""


def test_restore_session_discovers_account_id(tmp_path, monkeypatch):
    jar = tmp_path / "jar.json"
    jar.write_text(json.dumps({"cookies": [{"name": "reddit_session", "value": "x"}], "local_storage": {}}))
    driver = _fake_driver(current_url="https://ads.reddit.com/account/acct999/dashboard")
    client = _make_client(driver=driver, cookie_jar_path=str(jar))
    monkeypatch.setattr(client, "_login_via_form", MagicMock(name="login"))

    client.authenticate()

    client._login_via_form.assert_not_called()
    assert client.ads_account_id == "acct999"
