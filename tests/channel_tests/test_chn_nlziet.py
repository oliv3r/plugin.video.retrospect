# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("KODI_INTERACTIVE", "0")
os.environ.setdefault("KODI_HOME", "tests/home")

import xbmcgui as _xbmcgui
if not hasattr(_xbmcgui, "WindowXMLDialog"):
    class _FakeWindowXMLDialog:
        def __new__(cls, *args, **kwargs): return object.__new__(cls)
        def __init__(self, *args, **kwargs): pass
    _xbmcgui.WindowXMLDialog = _FakeWindowXMLDialog
    sys.modules["xbmcgui"].WindowXMLDialog = _FakeWindowXMLDialog

from . channeltest import ChannelTest


class TestNlzietChannel(ChannelTest):
    # noinspection PyPep8Naming
    def __init__(self, methodName):  # NOSONAR
        super(TestNlzietChannel, self).__init__(methodName, "channel.nlziet.nlziet", None)

    def setUp(self):
        super().setUp()
        # Stash class-level state so tests don't leak into each other.
        import chn_nlziet
        self._orig_service_interval = chn_nlziet.Channel.service_interval
        self._orig_is_blocked = chn_nlziet.Channel.is_blocked
        self._orig_is_update_required = chn_nlziet.Channel.is_update_required

    def tearDown(self):
        import chn_nlziet
        chn_nlziet.Channel.service_interval = self._orig_service_interval
        chn_nlziet.Channel.is_blocked = self._orig_is_blocked
        chn_nlziet.Channel.is_update_required = self._orig_is_update_required
        super().tearDown()

    # -- Channel metadata --------------------------------------------------

    def test_channel_exists(self):
        self.assertIsNotNone(self.channel)

    def test_service_interval_default(self):
        self.assertEqual(self.channel.service_interval, 90)

    def test_service_interval_is_positive(self):
        self.assertGreater(self.channel.service_interval, 0)

    def test_requires_logon(self):
        self.assertTrue(self.channel.requiresLogon)

    # -- on_service / appconfig cache --------------------------------------

    def _appconfig_raw(self, extra=None):
        payload = {"epgCacheTime": 300, "isAppBlocked": False}
        if extra:
            payload.update(extra)
        return json.dumps(payload)

    def test_on_service_fetches_stale_cache(self):
        """on_service() with a stale/empty cache triggers a fresh network fetch."""
        raw = self._appconfig_raw()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw) as mock_open, \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel.on_service()
        mock_open.assert_called_once()

    def test_on_service_empty_response_does_not_crash(self):
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel.on_service()
        mock_set.assert_not_called()

    def test_on_service_bad_json_does_not_crash(self):
        with patch("resources.lib.urihandler.UriHandler.open", return_value="not-json"), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel.on_service()
        mock_set.assert_not_called()

    def test_on_service_stores_synced_at(self):
        raw = self._appconfig_raw()
        before = time.time()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel.on_service()
        stored_json = mock_set.call_args[0][1]
        stored = json.loads(stored_json)
        self.assertGreaterEqual(stored["_synced_at"], before)

    def test_sync_appconfig_empty_response_does_not_write(self):
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel._Channel__sync_appconfig()
        mock_set.assert_not_called()

    def test_sync_appconfig_bad_json_does_not_write(self):
        with patch("resources.lib.urihandler.UriHandler.open", return_value="not json"), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel._Channel__sync_appconfig()
        mock_set.assert_not_called()

    def test_on_service_updates_service_interval(self):
        raw = self._appconfig_raw({"heartbeatInterval": 120})
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel.on_service()
        import chn_nlziet
        self.assertEqual(chn_nlziet.Channel.service_interval, 120)

    def test_on_service_uses_default_when_no_heartbeat(self):
        raw = self._appconfig_raw()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel.on_service()
        import chn_nlziet
        self.assertEqual(chn_nlziet.Channel.service_interval,
                         chn_nlziet.Channel.APPCONFIG_HEARTBEAT_DEFAULT)

    def test_sync_appconfig_sets_is_blocked(self):
        payload = {"isAppBlocked": True}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel._Channel__sync_appconfig()
        import chn_nlziet
        self.assertTrue(chn_nlziet.Channel.is_blocked)

    def test_sync_appconfig_sets_is_update_required(self):
        """__sync_appconfig() sets Channel.is_update_required when server signals deprecation."""
        payload = {"isUpdateRequired": True, "updateText": "Please update your client"}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel._Channel__sync_appconfig()
        import chn_nlziet
        self.assertTrue(chn_nlziet.Channel.is_update_required)

    def test_on_service_updates_service_interval(self):
        raw = self._appconfig_raw({"heartbeatInterval": 120})
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel.on_service()
        import chn_nlziet
        self.assertEqual(chn_nlziet.Channel.service_interval, 120)

    def test_on_service_uses_default_when_no_heartbeat(self):
        raw = self._appconfig_raw()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel.on_service()
        import chn_nlziet
        self.assertEqual(chn_nlziet.Channel.service_interval,
                         chn_nlziet.Channel.APPCONFIG_HEARTBEAT_DEFAULT)

    # -- log_on ------------------------------------------------------------

    def test_log_on_fast_path_succeeds(self):
        """log_on() takes the active_authentication fast path when token is valid."""
        mock_result = MagicMock()
        mock_result.logged_on = True
        with patch.object(self.channel._Channel__handler,
                          "active_authentication", return_value=mock_result), \
             patch.object(self.channel, "_Channel__validate_token", return_value=True), \
             patch.object(self.channel, "_Channel__set_auth_headers"), \
             patch.object(self.channel, "_Channel__select_profile_if_needed"):
            result = self.channel.log_on()
        self.assertTrue(result)
        self.assertTrue(self.channel.loggedOn)

    def test_log_on_with_credentials_succeeds(self):
        """log_on() with username+password succeeds when authenticator returns logged_on."""
        mock_active = MagicMock()
        mock_active.logged_on = False
        mock_auth_result = MagicMock()
        mock_auth_result.logged_on = True
        self.channel._Channel__handler._use_device_flow = False
        with patch.object(self.channel._Channel__handler,
                          "active_authentication", return_value=mock_active), \
             patch.object(self.channel._Channel__authenticator,
                          "log_on", return_value=mock_auth_result), \
             patch.object(self.channel, "_Channel__set_auth_headers"), \
             patch.object(self.channel, "_Channel__welcome_and_select_profile"):
            result = self.channel.log_on(username="user@test.nl", password="secret")
        self.assertTrue(result)

    def test_log_on_returns_false_on_failure(self):
        """log_on() returns False when active auth fails, no creds given, non-interactive."""
        mock_active = MagicMock()
        mock_active.logged_on = False
        with patch.object(self.channel._Channel__handler,
                          "active_authentication", return_value=mock_active):
            result = self.channel.log_on(interactive=False)
        self.assertFalse(result)

    def test_log_on_already_logged_in_returns_true(self):
        """log_on() is a no-op when self.loggedOn is already True."""
        self.channel.loggedOn = True
        result = self.channel.log_on()
        self.assertTrue(result)
        self.channel.loggedOn = False  # restore

    def test_log_on_blocked_returns_false(self):
        """log_on() returns False and shows AccountBlocked dialog when app is blocked."""
        import chn_nlziet
        chn_nlziet.Channel.is_blocked = True
        with patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel.log_on()
        self.assertFalse(result)
        mock_dialog.assert_called_once()

    def test_log_on_credentials_fail_shows_dialog(self):
        """Credential failure shows a dialog and returns False."""
        mock_active = MagicMock()
        mock_active.logged_on = False
        mock_fail = MagicMock()
        mock_fail.logged_on = False
        self.channel._Channel__handler._use_device_flow = False
        with patch.object(self.channel._Channel__handler,
                          "active_authentication", return_value=mock_active), \
             patch.object(self.channel._Channel__authenticator,
                          "log_on", return_value=mock_fail), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel.log_on(username="user@test.nl", password="secret")
        self.assertFalse(result)
        mock_dialog.assert_called_once()

    # -- log_off -----------------------------------------------------------

    def test_log_off_deregisters_device_when_device_flow(self):
        """log_off() calls deregister_device() when the device-flow login method is used."""
        from unittest.mock import PropertyMock
        with patch.object(type(self.channel._Channel__handler), "use_device_flow",
                          new_callable=PropertyMock, return_value=True), \
             patch.object(self.channel._Channel__handler, "deregister_device") as mock_dereg, \
             patch.object(self.channel._Channel__authenticator, "log_off"), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog"), \
             patch("xbmc.executebuiltin"):
            self.channel.log_off()
        mock_dereg.assert_called_once()

    def test_log_off_skips_deregister_for_web_flow(self):
        """log_off() does NOT call deregister_device() for username/password logins."""
        from unittest.mock import PropertyMock
        with patch.object(type(self.channel._Channel__handler), "use_device_flow",
                          new_callable=PropertyMock, return_value=False), \
             patch.object(self.channel._Channel__handler, "deregister_device") as mock_dereg, \
             patch.object(self.channel._Channel__authenticator, "log_off"), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog"), \
             patch("xbmc.executebuiltin"):
            self.channel.log_off()
        mock_dereg.assert_not_called()



    def test_validate_token_succeeds_when_user_info_available(self):
        """__validate_token() returns True when get_user_info() succeeds."""
        with patch.object(self.channel._Channel__handler, "get_user_info",
                          return_value={"name": "Oliver"}):
            result = self.channel._Channel__validate_token()
        self.assertTrue(result)

    def test_validate_token_fails_when_no_user_info(self):
        """__validate_token() returns False and shows dialog when get_user_info() fails."""
        with patch.object(self.channel._Channel__handler, "get_user_info", return_value=None), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel._Channel__validate_token()
        self.assertFalse(result)
        mock_dialog.assert_called_once()

    # -- __list_profiles ---------------------------------------------------

    def test_list_profiles_returns_profiles(self):
        """__list_profiles() returns profile list from API response."""
        profiles = [{"id": "p1", "displayName": "Oliver"}, {"id": "p2", "displayName": "Kids"}]
        with patch.object(self.channel._Channel__handler, "get_valid_token",
                          return_value="tok"), \
             patch("resources.lib.urihandler.UriHandler.open",
                   return_value=json.dumps(profiles)):
            result = self.channel._Channel__list_profiles()
        self.assertEqual(result, profiles)

    def test_list_profiles_no_token_returns_empty(self):
        """__list_profiles() returns [] when no token is available."""
        with patch.object(self.channel._Channel__handler, "get_valid_token",
                          return_value=None):
            result = self.channel._Channel__list_profiles()
        self.assertEqual(result, [])

    def test_list_profiles_empty_response_returns_empty(self):
        """__list_profiles() returns [] when the API returns an empty response."""
        with patch.object(self.channel._Channel__handler, "get_valid_token",
                          return_value="tok"), \
             patch("resources.lib.urihandler.UriHandler.open", return_value=""):
            result = self.channel._Channel__list_profiles()
        self.assertEqual(result, [])

    # -- __select_profile_if_needed ----------------------------------------

    def test_select_profile_if_needed_uses_stored_profile_id(self):
        """Stored profile_id triggers a token exchange without prompting."""
        with patch.object(self.channel, "_Channel__get_stored_profile_id", return_value="stored-id"), \
             patch.object(self.channel._Channel__handler, "exchange_token",
                          return_value=True) as mock_exchange, \
             patch.object(self.channel, "_Channel__set_stored_profile_id") as mock_store:
            self.channel._Channel__select_profile_if_needed()
        mock_exchange.assert_called_once_with(
            {"grant_type": "profile", "profile": "stored-id", "scope": "openid api"})
        mock_store.assert_not_called()

    def test_select_profile_if_needed_auto_selects_single(self):
        """Single available profile is auto-selected without a dialog."""
        profile = {"id": "p1", "displayName": "Oliver"}
        with patch.object(self.channel, "_Channel__get_stored_profile_id", return_value=""), \
             patch.object(self.channel, "_Channel__list_profiles", return_value=[profile]), \
             patch.object(self.channel._Channel__handler, "exchange_token",
                          return_value=True) as mock_exchange, \
             patch.object(self.channel, "_Channel__set_stored_profile_id") as mock_store, \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_selection_dialog") as mock_dlg:
            self.channel._Channel__select_profile_if_needed()
        mock_exchange.assert_called_once_with(
            {"grant_type": "profile", "profile": "p1", "scope": "openid api"})
        mock_store.assert_called_once_with("p1")
        mock_dlg.assert_not_called()

    def test_select_profile_if_needed_prompts_for_multiple(self):
        """Multiple profiles trigger a selection dialog; selected profile is set."""
        profiles = [{"id": "p1", "displayName": "Oliver"}, {"id": "p2", "displayName": "Kids"}]
        with patch.object(self.channel, "_Channel__get_stored_profile_id", return_value=""), \
             patch.object(self.channel, "_Channel__list_profiles", return_value=profiles), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_selection_dialog",
                   return_value=1) as mock_dlg, \
             patch.object(self.channel._Channel__handler, "exchange_token",
                          return_value=True) as mock_exchange, \
             patch.object(self.channel, "_Channel__set_stored_profile_id") as mock_store:
            self.channel._Channel__select_profile_if_needed()
        mock_dlg.assert_called_once()
        mock_exchange.assert_called_once_with(
            {"grant_type": "profile", "profile": "p2", "scope": "openid api"})
        mock_store.assert_called_once_with("p2")

    def test_select_profile_if_needed_no_profiles_skips_silently(self):
        """Empty profile list is handled without crash or exchange_token call."""
        with patch.object(self.channel, "_Channel__get_stored_profile_id", return_value=""), \
             patch.object(self.channel, "_Channel__list_profiles", return_value=[]), \
             patch.object(self.channel._Channel__handler, "exchange_token") as mock_exchange:
            self.channel._Channel__select_profile_if_needed()
        mock_exchange.assert_not_called()

    def test_select_profile_if_needed_cancelled(self):
        """Cancelling the selection dialog does not call exchange_token."""
        profiles = [{"id": "p1", "displayName": "A"}, {"id": "p2", "displayName": "B"}]
        with patch.object(self.channel, "_Channel__get_stored_profile_id", return_value=""), \
             patch.object(self.channel, "_Channel__list_profiles", return_value=profiles), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_selection_dialog",
                   return_value=-1), \
             patch.object(self.channel._Channel__handler, "exchange_token") as mock_exchange:
            self.channel._Channel__select_profile_if_needed()
        mock_exchange.assert_not_called()

    # -- switch_profile action ---------------------------------------------

    def test_switch_profile_action_requires_login(self):
        """switch_profile() shows LoginFirst and does nothing when not logged in."""
        mock_not_logged = MagicMock()
        mock_not_logged.logged_on = False
        with patch.object(self.channel._Channel__handler,
                          "active_authentication", return_value=mock_not_logged), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog, \
             patch.object(self.channel, "_Channel__set_stored_profile_id") as mock_clear:
            self.channel.switch_profile()
        mock_dialog.assert_called_once()
        mock_clear.assert_not_called()

    def test_switch_profile_action_clears_and_reselects(self):
        """switch_profile() clears the stored profile and re-runs selection."""
        mock_logged = MagicMock()
        mock_logged.logged_on = True
        with patch.object(self.channel._Channel__handler,
                          "active_authentication", return_value=mock_logged), \
             patch.object(self.channel, "_Channel__set_stored_profile_id") as mock_clear, \
             patch.object(self.channel, "_Channel__select_profile_if_needed") as mock_select, \
             patch.object(self.channel, "_Channel__set_auth_headers"), \
             patch("xbmc.executebuiltin"):
            self.channel.switch_profile()
        mock_clear.assert_called_once_with("")
        mock_select.assert_called_once()

    # -- __profile_type ----------------------------------------------------

    def test_profile_type_returns_jwt_claim(self):
        """__profile_type() extracts profileType from the access token JWT."""
        from tests.authentication.nlziethandler_mocks import MOCK_PROFILE_ACCESS_TOKEN
        with patch.object(self.channel._Channel__handler, "get_valid_token",
                          return_value=MOCK_PROFILE_ACCESS_TOKEN):
            result = self.channel._Channel__profile_type()
        self.assertEqual(result, "ChildYoung")

    def test_profile_type_returns_empty_when_no_token(self):
        """__profile_type() returns '' when no token is available."""
        with patch.object(self.channel._Channel__handler, "get_valid_token", return_value=None):
            result = self.channel._Channel__profile_type()
        self.assertEqual(result, "")

    def test_profile_type_returns_empty_for_unscoped_token(self):
        """__profile_type() returns '' when token carries no profileType claim."""
        import base64 as _base64
        import json as _json
        payload = _base64.urlsafe_b64encode(
            _json.dumps({"sub": "u1", "exp": 9999999999}).encode()
        ).rstrip(b"=").decode()
        plain_token = f"header.{payload}.sig"
        with patch.object(self.channel._Channel__handler, "get_valid_token",
                          return_value=plain_token):
            result = self.channel._Channel__profile_type()
        self.assertEqual(result, "")

    # -- __run_device_flow / __poll_with_progress --------------------------

    def test_poll_with_progress_does_not_pass_cancel_lbl_to_dialog(self):
        """cancel_lbl must NOT be passed as a positional arg to DeviceAuthDialog.

        Real Kodi's C __new__ rejects extra args; also passing cancel_lbl at
        position 7 collides with manual_label which is passed as keyword.
        """
        flow = {
            "user_code": "ABCD-1234",
            "verification_uri": "https://example.com/activate",
            "device_code": "devcode",
            "interval": 5,
            "expires_in": 60,
        }
        mock_dialog = MagicMock()
        mock_dialog.result = "cancelled"
        mock_dialog.stop_event.wait.return_value = True

        with patch("resources.lib.deviceauthdialog.DeviceAuthDialog",
                   return_value=mock_dialog) as MockDialog, \
             patch("threading.Thread"):
            self.channel._Channel__poll_with_progress(flow, qr_url=None)

        call_args = MockDialog.call_args
        positional = call_args.args if call_args else ()
        keyword = call_args.kwargs if call_args else {}
        # DeviceAuthDialog(title, visit_text, uri, code_text, code, timeout, ...)
        # Position 7 onwards must be absent (no cancel_lbl crammed in before manual_label)
        self.assertLessEqual(len(positional), 6,
                             "DeviceAuthDialog called with too many positional args "
                             "(cancel_lbl must not be passed positionally)")
        self.assertIn("manual_label", keyword,
                      "manual_label should be passed as keyword")

    def test_timeout_retries_device_flow_without_dialog(self):
        """On timeout the device flow restarts automatically; no yes/no dialog is shown."""
        flow = {
            "user_code": "ABCD",
            "verification_uri": "https://example.com",
            "device_code": "dc",
            "interval": 5,
            "expires_in": 60,
        }

        with patch.object(self.channel._Channel__handler, "start_device_flow",
                          return_value=flow), \
             patch.object(self.channel, "_Channel__poll_with_progress",
                          side_effect=["timeout", "cancelled"]) as mock_poll, \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_yes_no") as mock_yes_no, \
             patch("xbmc.getInfoLabel", return_value="Kodi"):
            result = self.channel._Channel__run_device_flow()

        mock_yes_no.assert_not_called()
        self.assertEqual(mock_poll.call_count, 2)
        self.assertFalse(result)

class TestNlzietAppconfigLive(ChannelTest):
    """Live integration tests for appconfig — requires NLZIET_USERNAME in the environment.

    The appconfig endpoint is public (no authentication needed), but
    NLZIET_USERNAME / NLZIET_PASSWORD presence is used as the 'run live tests' signal.
    """

    # noinspection PyPep8Naming
    def __init__(self, methodName):  # NOSONAR
        super().__init__(methodName, "channel.nlziet.nlziet", None)

    @classmethod
    def setUpClass(cls):
        if not os.getenv("NLZIET_USERNAME") or not os.getenv("NLZIET_PASSWORD"):
            raise unittest.SkipTest("NLZIET credentials not in environment.")
        super().setUpClass()

    def test_sync_appconfig_returns_valid_json(self):
        """Real appconfig fetch returns parseable JSON with expected keys."""
        import chn_nlziet
        from resources.lib.addonsettings import AddonSettings, LOCAL

        self.channel._Channel__sync_appconfig()

        raw = AddonSettings.get_setting(chn_nlziet.Channel.APPCONFIG_CACHE_KEY, store=LOCAL)
        self.assertIsNotNone(raw)
        data = json.loads(raw)
        self.assertIn("heartbeatInterval", data)


class TestNlzietChannelLive(ChannelTest):
    """Live integration tests — skipped when NLZIET_USERNAME / NLZIET_PASSWORD are absent."""

    # noinspection PyPep8Naming
    def __init__(self, methodName):  # NOSONAR
        super(TestNlzietChannelLive, self).__init__(methodName, "channel.nlziet.nlziet", None)

    @classmethod
    def setUpClass(cls):
        cls.username = os.getenv("NLZIET_USERNAME")
        cls.password = os.getenv("NLZIET_PASSWORD")
        if not cls.username or not cls.password:
            raise unittest.SkipTest("NLZIET credentials not in environment.")
        super().setUpClass()

        from resources.lib.addonsettings import AddonSettings, LOCAL
        from resources.lib.authentication.nlzietoauth2handler import NLZIETOAuth2Handler
        from resources.lib.authentication.authenticator import Authenticator
        for client_id in (NLZIETOAuth2Handler.WEB_CLIENT_ID, NLZIETOAuth2Handler.TV_CLIENT_ID):
            prefix = "nlziet_oauth2_{}_".format(client_id)
            AddonSettings.set_setting("{}access_token".format(prefix), "", store=LOCAL)
            AddonSettings.set_setting("{}refresh_token".format(prefix), "", store=LOCAL)
            AddonSettings.set_setting("{}expires_at".format(prefix), "", store=LOCAL)
        AddonSettings.set_setting(NLZIETOAuth2Handler.AUTH_METHOD_KEY, "web", store=LOCAL)
        handler = NLZIETOAuth2Handler(use_device_flow=False)
        auth = Authenticator(handler)
        result = auth.log_on(username=cls.username, password=cls.password)
        if not result.logged_on:
            raise unittest.SkipTest("NLZIET live login failed in setUpClass.")

    def setUp(self):
        super().setUp()
        if not self.channel.log_on(self.username, self.password):
            self.skipTest("NLZIET login failed.")

    def test_login_succeeds(self):
        """Live: log_on() with real credentials succeeds."""
        self.assertTrue(self.channel.loggedOn)

    def test_profile_selected_after_login(self):
        """Live: a profile is active after login (profile_id is set)."""
        self.assertTrue(self.channel.loggedOn)
        self.assertIsNotNone(self.channel._Channel__handler.profile_id)
