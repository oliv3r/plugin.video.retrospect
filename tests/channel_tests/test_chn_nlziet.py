# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import re
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
from tests.channel_tests.nlziet_mocks import MOCK_APPCONFIG_RESPONSE, MOCK_EPG_LIVE_RESPONSE


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
        self._orig_blocked_reason = chn_nlziet.Channel.blocked_reason
        self._orig_is_update_required = chn_nlziet.Channel.is_update_required

    def tearDown(self):
        import chn_nlziet
        chn_nlziet.Channel.service_interval = self._orig_service_interval
        chn_nlziet.Channel.is_blocked = self._orig_is_blocked
        chn_nlziet.Channel.blocked_reason = self._orig_blocked_reason
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

    def test_settings_actions_use_defined_labels(self):
        from resources.lib.helpers.languagehelper import LanguageHelper

        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))
        settings_path = os.path.join(
            repo_root, "channels", "channel.nlziet", "nlziet", "chn_nlziet.json"
        )

        with open(settings_path, encoding="utf-8") as fh:
            settings = json.load(fh)["settings"]

        settings_by_id = {setting["id"]: setting["value"] for setting in settings}

        def get_label_id(setting_id):
            match = re.search(r'label="(\d+)"', settings_by_id[setting_id])
            self.assertIsNotNone(match)
            return int(match.group(1))

        self.assertEqual(get_label_id("nlziet_device_setup"), LanguageHelper.DeviceSetupTitle)
        self.assertEqual(get_label_id("nlziet_log_off"), LanguageHelper.LogOut)
        self.assertEqual(get_label_id("nlziet_switch_profile"), LanguageHelper.SwitchProfile)
        self.assertEqual(LanguageHelper.get_localized_string(LanguageHelper.LogOut), "Log out")
        self.assertEqual(
            LanguageHelper.get_localized_string(LanguageHelper.SwitchProfile), "Switch profile"
        )

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
                patch.object(self.channel._Channel__handler, "refresh_access_token"), \
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

    def test_on_service_refreshes_token(self):
        """on_service() calls refresh_access_token() to proactively renew before expiry."""
        raw = self._appconfig_raw()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"), \
                patch.object(self.channel._Channel__handler,
                             "refresh_access_token") as mock_refresh:
            self.channel.on_service()
        mock_refresh.assert_called_once()

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
        """__sync_appconfig() sets Channel.is_blocked and stores blocked_reason."""
        payload = {"isAppBlocked": True, "appBlockedReason": "Maintenance"}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel._Channel__sync_appconfig()
        import chn_nlziet
        self.assertTrue(chn_nlziet.Channel.is_blocked)
        self.assertEqual(chn_nlziet.Channel.blocked_reason, "Maintenance")

    def test_sync_appconfig_resets_blocked_reason_to_fallback_when_not_blocked(self):
        """__sync_appconfig() resets blocked_reason to the localized fallback when isAppBlocked is false."""
        import chn_nlziet
        from resources.lib.helpers.languagehelper import LanguageHelper
        chn_nlziet.Channel.blocked_reason = "stale"
        payload = {"isAppBlocked": False}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel._Channel__sync_appconfig()
        expected = LanguageHelper.get_localized_string(LanguageHelper.UnknownBlockReason)
        self.assertEqual(chn_nlziet.Channel.blocked_reason, expected)

    def test_sync_appconfig_blocked_reason_logged(self):
        """__sync_appconfig() logs appBlockedReason when isAppBlocked is true."""
        payload = {"isAppBlocked": True, "appBlockedReason": "Maintenance window"}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"), \
                patch("resources.lib.logger.Logger.warning") as mock_warn:
            self.channel._Channel__sync_appconfig()
        warned = any("Maintenance window" in str(c) for c in mock_warn.call_args_list)
        self.assertTrue(warned, f"Expected reason in warning, got: {mock_warn.call_args_list}")

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
        msg = mock_dialog.call_args[0][1]
        self.assertIn("blocked", msg.lower())

    def test_log_on_blocked_shows_reason(self):
        """log_on() includes appBlockedReason in the dialog when reason is set."""
        import chn_nlziet
        chn_nlziet.Channel.is_blocked = True
        chn_nlziet.Channel.blocked_reason = "Gepland onderhoud"
        with patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel.log_on()
        self.assertFalse(result)
        msg = mock_dialog.call_args[0][1]
        self.assertIn("Gepland onderhoud", msg)

    def test_log_on_blocked_no_reason_no_parentheses(self):
        """log_on() does not add empty parentheses when blocked_reason is empty."""
        import chn_nlziet
        chn_nlziet.Channel.is_blocked = True
        chn_nlziet.Channel.blocked_reason = ""
        with patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel.log_on()
        self.assertFalse(result)
        msg = mock_dialog.call_args[0][1]
        self.assertNotIn("()", msg)

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

    def test_log_on_credentials_fail_blocked_shows_combined_message(self):
        """Credential failure + is_blocked shows a combined login-failed + reason message."""
        import chn_nlziet
        chn_nlziet.Channel.is_blocked = True
        chn_nlziet.Channel.blocked_reason = "Onderhoud"
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
        msg = mock_dialog.call_args[0][1]
        self.assertIn("Onderhoud", msg)

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
        """Stored profile_id scopes the token without prompting."""
        with patch.object(self.channel, "_Channel__get_stored_profile_id", return_value="stored-id"), \
             patch.object(self.channel._Channel__handler, "set_profile_claim") as mock_set_profile, \
             patch.object(self.channel, "_Channel__set_stored_profile_id") as mock_store:
            self.channel._Channel__select_profile_if_needed()
        mock_set_profile.assert_called_once_with("stored-id")
        mock_store.assert_not_called()

    def test_select_profile_if_needed_auto_selects_single(self):
        """Single available profile is auto-selected without a dialog."""
        profile = {"id": "p1", "displayName": "Oliver"}
        with patch.object(self.channel, "_Channel__get_stored_profile_id", return_value=""), \
             patch.object(self.channel, "_Channel__list_profiles", return_value=[profile]), \
             patch.object(self.channel._Channel__handler, "set_profile_claim",
                          return_value=True) as mock_set_profile, \
             patch.object(self.channel, "_Channel__set_stored_profile_id") as mock_store, \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_selection_dialog") as mock_dlg:
            self.channel._Channel__select_profile_if_needed()
        mock_set_profile.assert_called_once_with("p1")
        mock_store.assert_called_once_with("p1")
        mock_dlg.assert_not_called()

    def test_select_profile_if_needed_prompts_for_multiple(self):
        """Multiple profiles trigger a selection dialog; selected profile is set."""
        profiles = [{"id": "p1", "displayName": "Oliver"}, {"id": "p2", "displayName": "Kids"}]
        with patch.object(self.channel, "_Channel__get_stored_profile_id", return_value=""), \
             patch.object(self.channel, "_Channel__list_profiles", return_value=profiles), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_selection_dialog",
                   return_value=1) as mock_dlg, \
             patch.object(self.channel._Channel__handler, "set_profile_claim",
                          return_value=True) as mock_set_profile, \
             patch.object(self.channel, "_Channel__set_stored_profile_id") as mock_store:
            self.channel._Channel__select_profile_if_needed()
        mock_dlg.assert_called_once()
        mock_set_profile.assert_called_once_with("p2")
        mock_store.assert_called_once_with("p2")

    def test_select_profile_if_needed_no_profiles_skips_silently(self):
        """Empty profile list is handled without crash or profile selection call."""
        with patch.object(self.channel, "_Channel__get_stored_profile_id", return_value=""), \
             patch.object(self.channel, "_Channel__list_profiles", return_value=[]), \
             patch.object(self.channel._Channel__handler, "set_profile_claim") as mock_set_profile:
            self.channel._Channel__select_profile_if_needed()
        mock_set_profile.assert_not_called()

    def test_select_profile_if_needed_cancelled(self):
        """Cancelling the selection dialog does not call set_profile_claim."""
        profiles = [{"id": "p1", "displayName": "A"}, {"id": "p2", "displayName": "B"}]
        with patch.object(self.channel, "_Channel__get_stored_profile_id", return_value=""), \
             patch.object(self.channel, "_Channel__list_profiles", return_value=profiles), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_selection_dialog",
                   return_value=-1), \
             patch.object(self.channel._Channel__handler, "set_profile_claim") as mock_set_profile:
            self.channel._Channel__select_profile_if_needed()
        mock_set_profile.assert_not_called()

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

    # -- appconfig: mocked equivalent of TestNlzietAppconfigLive -----------

    def test_sync_appconfig_stores_expected_keys(self):
        """__sync_appconfig() parses and stores a response containing the expected keys."""
        import chn_nlziet

        stored = {}
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=json.dumps(MOCK_APPCONFIG_RESPONSE)), \
             patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
             patch("resources.lib.addonsettings.AddonSettings.set_setting",
                   side_effect=lambda k, v, **kw: stored.update({k: v})):
            self.channel._Channel__sync_appconfig()

        self.assertIn(chn_nlziet.Channel.APPCONFIG_CACHE_KEY, stored)
        data = json.loads(stored[chn_nlziet.Channel.APPCONFIG_CACHE_KEY])
        self.assertIn("heartbeatInterval", data)

    # -- process_folder_list: mocked equivalent of TestNlzietChannelLive ---

    def test_process_folder_list_returns_live_channel_items(self):
        """process_folder_list(None) returns MediaItems for each channel in the API response."""
        with patch.object(self.channel, "log_on", return_value=True), \
             patch("resources.lib.urihandler.UriHandler.open",
                   return_value=json.dumps(MOCK_EPG_LIVE_RESPONSE)), \
             patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
             patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            items = self.channel.process_folder_list(None)

        self.assertIsNotNone(items)
        self.assertGreater(len(items), 0)
        urls = [i.url for i in items]
        expected_ids = [e["channel"]["content"]["id"] for e in MOCK_EPG_LIVE_RESPONSE["data"]]
        for channel_id in expected_ids:
            self.assertTrue(any(f"channel={channel_id}" in u for u in urls),
                            f"No item URL contains channel={channel_id}")

    # -- Channel metadata (live streaming) ---------------------------------

    def test_mainlist_uri_is_live_endpoint(self):
        import chn_nlziet
        self.assertIn(chn_nlziet.Channel.API_V9_EPG_LIVE, self.channel.mainListUri)

    # -- create_live_channel_item ------------------------------------------

    def _live_result_set(self, channel_id="test-ch-1", title="Test Channel",
                         logo_url="https://example.com/test-ch-1.png",
                         asset_id="abc", program_title="Current Show",
                         missing_feature=None):
        data = {
            "channel": {
                "content": {
                    "id": channel_id,
                    "title": title,
                    "logo": {"normalUrl": logo_url}
                }
            },
            "programLocations": [
                {"content": {"assetId": asset_id, "title": program_title}}
            ]
        }
        if missing_feature is not None:
            data["channel"]["missingSubscriptionFeature"] = missing_feature
        return data

    def test_create_live_channel_item_full(self):
        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        self.assertTrue(item.isLive)
        self.assertTrue(item.isDrmProtected)
        self.assertIn("channel=test-ch-1", item.url)
        self.assertEqual(item.thumb, "https://example.com/test-ch-1.png")
        self.assertEqual(item.description, "Current Show")
        self.assertEqual(item.metaData.get("asset_id"), "abc")

    def test_create_live_channel_item_no_channel(self):
        """Missing channel dict returns None."""
        self.assertIsNone(self.channel.create_live_channel_item({}))

    def test_create_live_channel_item_no_id(self):
        """Channel without id returns None."""
        result_set = {"channel": {"content": {"title": "No ID"}}}
        self.assertIsNone(self.channel.create_live_channel_item(result_set))

    def test_create_live_channel_item_paid(self):
        """Channel with missingSubscriptionFeature is marked paid."""
        result_set = self._live_result_set(missing_feature="PremiumFeature")
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        self.assertTrue(item.isPaid)

    # -- update_live_item --------------------------------------------------

    def test_update_live_item_success(self):
        """update_live_item() with a valid handshake response marks item complete."""
        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)

        handshake_response = json.dumps({
            "manifestUrl": "https://example.com/stream.mpd",
            "drm": {
                "licenseUrl": "https://license.example.com/",
                "headers": {"Authorization": "Bearer tok"}
            }
        })
        with patch("resources.lib.urihandler.UriHandler.open", return_value=handshake_response), \
             patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
             patch("resources.lib.addonsettings.AddonSettings.set_setting"), \
             patch("resources.lib.streams.mpd.Mpd.get_license_key", return_value="key"), \
             patch("resources.lib.streams.mpd.Mpd.set_input_stream_addon_input"):
            updated = self.channel.update_live_item(item)
        self.assertTrue(updated.complete)

    def test_update_live_item_no_channel_id_returns_item(self):
        """update_live_item() with a URL that has no channel= returns without crash."""
        from resources.lib.mediaitem import MediaItem
        item = MediaItem("Test", "https://example.com/no-channel-param")
        result = self.channel.update_live_item(item)
        self.assertIsNotNone(result)
        self.assertFalse(result.complete)

    def test_update_live_item_extra_query_params_do_not_corrupt_channel_id(self):
        """update_live_item() extracts channel ID correctly even with extra query parameters."""
        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        expected_channel_id = result_set["channel"]["content"]["id"]
        item.url += "&extra=param"

        handshake_response = json.dumps({
            "manifestUrl": "https://example.com/stream.mpd",
            "drm": {"licenseUrl": "https://lic.example.com/", "headers": {}}
        })
        captured_url = []

        def capture_open(url, **kwargs):
            captured_url.append(url)
            return handshake_response

        with patch("resources.lib.urihandler.UriHandler.open", side_effect=capture_open), \
             patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
             patch("resources.lib.streams.mpd.Mpd.get_license_key", return_value="key"), \
             patch("resources.lib.streams.mpd.Mpd.set_input_stream_addon_input"):
            updated = self.channel.update_live_item(item)

        self.assertTrue(updated.complete)
        self.assertTrue(
            any(f"channel={expected_channel_id}" in u and "extra=param" not in u
                for u in captured_url),
            f"Expected clean channel ID in handshake URL, got: {captured_url}")

    def test_update_live_item_invalid_json_returns_item(self):
        """update_live_item() with a non-JSON handshake response returns without crash."""
        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)

        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value="<html>Service Unavailable</html>"):
            result = self.channel.update_live_item(item)

        self.assertIsNotNone(result)
        self.assertFalse(result.complete)

    def test_update_live_item_has_no_start_offset(self):
        """update_live_item() does not add startOffsetInSeconds to the handshake URL."""
        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        handshake_response = json.dumps({
            "manifestUrl": "https://example.com/stream.mpd",
            "drm": {"licenseUrl": "https://lic.example.com/", "headers": {}}
        })
        captured_url = []

        def capture_open(url, **kwargs):
            captured_url.append(url)
            return handshake_response

        with patch("resources.lib.urihandler.UriHandler.open", side_effect=capture_open), \
             patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
             patch("resources.lib.streams.mpd.Mpd.get_license_key", return_value="key"), \
             patch("resources.lib.streams.mpd.Mpd.set_input_stream_addon_input"):
            updated = self.channel.update_live_item(item)

        self.assertTrue(updated.complete)
        self.assertFalse(any("startOffsetInSeconds" in u for u in captured_url))

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
        if data.get("isAppBlocked"):
            reason = data.get("appBlockedReason") or "no reason provided"
            self.fail(f"NLZIET app is blocked — {reason}")


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

    def test_process_folder_list_returns_live_channels_after_login(self):
        """Live: process_folder_list(None) returns a non-empty list after login."""
        items = self.channel.process_folder_list(None)
        self.assertIsNotNone(items)
        self.assertGreater(len(items), 0)
