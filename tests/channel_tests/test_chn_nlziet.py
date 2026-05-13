# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import sys
import time
import unittest
from typing import Any, Dict, Optional
from unittest.mock import ANY, MagicMock, PropertyMock, patch


import xbmcgui as _xbmcgui
if not hasattr(_xbmcgui, "WindowXMLDialog"):
    class _FakeWindowXMLDialog:
        def __new__(cls: "type[_FakeWindowXMLDialog]", *args: Any, **kwargs: Any) -> object: return object.__new__(cls)  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None: pass
    _xbmcgui.WindowXMLDialog = _FakeWindowXMLDialog  # type: ignore[attr-defined]
    sys.modules["xbmcgui"].WindowXMLDialog = _FakeWindowXMLDialog  # type: ignore[attr-defined]

from resources.lib.authentication.nlziethandler import DEVICE_FLOW_USER_AGENT
from resources.lib.logger import Logger
from resources.lib.urihandler import UriHandler, UriStatus
from .channeltest import ChannelTest
from tests.channel_tests.nlziet_mocks import MOCK_APPCONFIG_RESPONSE, MOCK_EPG_LIVE_RESPONSE


class TestNlzietChannel(ChannelTest):
    def __init__(self, methodName: str) -> None:
        super(TestNlzietChannel, self).__init__(methodName, "channel.nlziet.nlziet", None)


    def setUp(self) -> None:
        super().setUp()
        # Stash class-level state so tests don't leak into each other.
        import chn_nlziet
        self._orig_service_interval = chn_nlziet.Channel.service_interval
        self._orig_is_blocked = chn_nlziet.Channel.is_blocked
        self._orig_blocked_reason = chn_nlziet.Channel.blocked_reason
        self._orig_is_update_required = chn_nlziet.Channel.is_update_required
        self._orig_update_reason = chn_nlziet.Channel.update_reason

        # Channel init may make real HTTP calls without credentials; clear stale status.
        UriHandler.instance().status = UriStatus(code=0, url=None, error=False, reason=None)


    def tearDown(self) -> None:
        import chn_nlziet
        chn_nlziet.Channel.service_interval = self._orig_service_interval
        chn_nlziet.Channel.is_blocked = self._orig_is_blocked
        chn_nlziet.Channel.blocked_reason = self._orig_blocked_reason
        chn_nlziet.Channel.is_update_required = self._orig_is_update_required
        chn_nlziet.Channel.update_reason = self._orig_update_reason
        super().tearDown()

    # -- Channel metadata --------------------------------------------------

    def test_channel_exists(self) -> None:
        self.assertIsNotNone(self.channel)


    def test_initial_folder_items_returns_live_tv_folder_when_not_logged_on(self) -> None:
        """SUCCESS → returns Live TV folder even when the user is not logged in."""

        with patch.object(type(self.channel), "loggedOn",
                          new_callable=PropertyMock, return_value=False):
            _, items = self.channel.get_initial_folder_items("")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].isLive)


    def test_initial_folder_items_returns_live_tv_folder_when_logged_on(self) -> None:
        """SUCCESS → returns one isLive FolderItem for Live TV."""

        with patch.object(type(self.channel), "loggedOn",
                          new_callable=PropertyMock, return_value=True):
            _, items = self.channel.get_initial_folder_items("")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].isLive)


    def test_service_interval_default(self) -> None:
        self.assertEqual(self.channel.service_interval, 90)


    def test_service_interval_is_positive(self) -> None:
        self.assertGreater(self.channel.service_interval, 0)

    # -- _nlziet_headers (app headers) ------------------------------------


    def test_nlziet_headers_contains_required_keys(self) -> None:
        """_nlziet_headers carries all required Nlziet-* app-identification keys."""

        for key in ("Nlziet-AppName", "Nlziet-AppVersion", "Nlziet-BrandName",
                    "Nlziet-ModelName", "Nlziet-PlatformVersion",
                    "Nlziet-DeviceCapabilities", "Accept"):
            with self.subTest(key=key):
                self.assertIn(key, self.channel._nlziet_headers)


    def test_nlziet_headers_app_name_reflects_device_flow(self) -> None:
        """AppName/Version in _nlziet_headers matches the handler's device_flow setting."""

        import chn_nlziet as m
        is_device = self.channel._authenticator.device_flow
        expected_name = m.NLZIET_APP_NAME if is_device else m.NLZIET_WEB_NAME
        expected_version = m.NLZIET_APP_VERSION if is_device else m.NLZIET_WEB_VERSION
        self.assertEqual(self.channel._nlziet_headers["Nlziet-AppName"], expected_name)
        self.assertEqual(self.channel._nlziet_headers["Nlziet-AppVersion"], expected_version)

    # -- service_update / appconfig cache ----------------------------------

    def _appconfig_raw(self, extra: Optional[Dict[str, Any]] = None) -> str:
        payload = {"epgCacheTime": 300, "isAppBlocked": False}
        if extra:
            payload.update(extra)
        return json.dumps(payload)


    def test_sync_appconfig_empty_response_does_not_write(self) -> None:
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel._sync_appconfig()
        mock_set.assert_not_called()


    def test_sync_appconfig_bad_json_does_not_write(self) -> None:
        with patch("resources.lib.urihandler.UriHandler.open", return_value="not json"), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel._sync_appconfig()
        mock_set.assert_not_called()


    def test_sync_appconfig_non_dict_json_does_not_write(self) -> None:
        """_sync_appconfig() silently ignores valid JSON that is not a dict (e.g. null, [])."""

        for non_dict in ("null", "[]", '"string"', "42"):
            with self.subTest(response=non_dict):
                with patch("resources.lib.urihandler.UriHandler.open", return_value=non_dict), \
                        patch("resources.lib.addonsettings.AddonSettings.get_setting",
                              return_value=""), \
                        patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
                    self.channel._sync_appconfig()
                mock_set.assert_not_called()


    def test_service_update_updates_service_interval(self) -> None:
        raw = self._appconfig_raw({"heartbeatInterval": 120})
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel.service_update()
        import chn_nlziet
        self.assertEqual(chn_nlziet.Channel.service_interval, 120)


    def test_service_update_uses_default_when_no_heartbeat(self) -> None:
        raw = self._appconfig_raw()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel.service_update()
        import chn_nlziet
        self.assertEqual(chn_nlziet.Channel.service_interval,
                         chn_nlziet.APPCONFIG_HEARTBEAT_DEFAULT)


    def test_sync_appconfig_sets_is_blocked(self) -> None:
        """_sync_appconfig() sets Channel.is_blocked when isAppBlocked is true."""

        payload = {"isAppBlocked": True, "appBlockedReason": "Maintenance"}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel._sync_appconfig()
        import chn_nlziet
        self.assertTrue(chn_nlziet.Channel.is_blocked)
        self.assertEqual(chn_nlziet.Channel.blocked_reason, "Maintenance")


    def test_sync_appconfig_resets_blocked_reason_to_empty_when_not_blocked(self) -> None:
        """_sync_appconfig() resets blocked_reason to empty string when isAppBlocked is false."""

        import chn_nlziet
        chn_nlziet.Channel.blocked_reason = "stale"
        payload = {"isAppBlocked": False}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel._sync_appconfig()
        self.assertEqual(chn_nlziet.Channel.blocked_reason, "")


    def test_sync_appconfig_blocked_reason_logged(self) -> None:
        """_sync_appconfig() logs appBlockedReason when isAppBlocked is true."""

        payload = {"isAppBlocked": True, "appBlockedReason": "Maintenance window"}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"), \
                patch("resources.lib.logger.Logger.warning") as mock_warn:
            self.channel._sync_appconfig()
        warned = any("Maintenance window" in str(c) for c in mock_warn.call_args_list)
        self.assertTrue(warned, f"Expected reason in warning, got: {mock_warn.call_args_list}")


    def test_sync_appconfig_blocked_reason_null_normalised_to_empty(self) -> None:
        """_sync_appconfig() normalises a null appBlockedReason to an empty string."""

        payload = {"isAppBlocked": True, "appBlockedReason": None}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel._sync_appconfig()
        import chn_nlziet
        self.assertEqual(chn_nlziet.Channel.blocked_reason, "")


    def test_sync_appconfig_update_reason_null_normalised_to_empty(self) -> None:
        """_sync_appconfig() normalises a null updateText to an empty string."""

        payload = {"isUpdateRequired": True, "updateText": None}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel._sync_appconfig()
        import chn_nlziet
        self.assertEqual(chn_nlziet.Channel.update_reason, "")


    def test_sync_appconfig_sets_is_update_required(self) -> None:
        """_sync_appconfig() sets Channel.is_update_required when server signals deprecation."""

        payload = {"isUpdateRequired": True, "updateText": "Please update your client"}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel._sync_appconfig()
        import chn_nlziet
        self.assertTrue(chn_nlziet.Channel.is_update_required)


class TestNlzietChannelUnit(ChannelTest):
    """Unit tests for the NLZIET channel — always run, no credentials required.

    All HTTP calls and settings I/O are patched.
    """


    def __init__(self, methodName: str) -> None:
        super().__init__(methodName, "channel.nlziet.nlziet", None)


    def setUp(self) -> None:
        super().setUp()
        import chn_nlziet
        self._orig_service_interval = chn_nlziet.Channel.service_interval
        self._orig_is_blocked = chn_nlziet.Channel.is_blocked
        self._orig_blocked_reason = chn_nlziet.Channel.blocked_reason
        self._orig_is_update_required = chn_nlziet.Channel.is_update_required
        self._orig_update_reason = chn_nlziet.Channel.update_reason
        UriHandler.instance().status = UriStatus(code=0, url=None, error=False, reason=None)


    def tearDown(self) -> None:
        import chn_nlziet
        chn_nlziet.Channel.service_interval = self._orig_service_interval
        chn_nlziet.Channel.is_blocked = self._orig_is_blocked
        chn_nlziet.Channel.blocked_reason = self._orig_blocked_reason
        chn_nlziet.Channel.is_update_required = self._orig_is_update_required
        chn_nlziet.Channel.update_reason = self._orig_update_reason
        chn_nlziet.Channel._item_detail_cache = {}
        super().tearDown()


    def test_sync_appconfig_stores_expected_keys(self) -> None:
        """_sync_appconfig() parses and stores a response containing the expected keys."""

        import chn_nlziet

        stored = {}
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=json.dumps(MOCK_APPCONFIG_RESPONSE)), \
             patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
             patch("resources.lib.addonsettings.AddonSettings.set_setting",
                   side_effect=lambda k, v, **kw: stored.update({k: v})):
            self.channel._sync_appconfig()

        self.assertIn(chn_nlziet.APPCONFIG_CACHE_KEY, stored)
        data = json.loads(stored[chn_nlziet.APPCONFIG_CACHE_KEY])
        self.assertIn("heartbeatInterval", data)

    # -- log_on ------------------------------------------------------------





    def test_log_on_blocked_returns_false(self) -> None:
        """log_on() returns False and shows AccountBlocked dialog when app is blocked."""

        import chn_nlziet
        chn_nlziet.Channel.is_blocked = True
        with patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel.log_on()
        self.assertFalse(result)
        mock_dialog.assert_called_once()
        msg = mock_dialog.call_args[0][1]
        self.assertIn("blocked", msg.lower())


    def test_log_on_blocked_shows_reason(self) -> None:
        """log_on() includes appBlockedReason in the dialog when reason is set."""

        import chn_nlziet
        chn_nlziet.Channel.is_blocked = True
        chn_nlziet.Channel.blocked_reason = "Gepland onderhoud"
        with patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel.log_on()
        self.assertFalse(result)
        msg = mock_dialog.call_args[0][1]
        self.assertIn("Gepland onderhoud", msg)


    def test_log_on_blocked_no_reason_no_parentheses(self) -> None:
        """log_on() does not add empty parentheses when blocked_reason is empty."""

        import chn_nlziet
        chn_nlziet.Channel.is_blocked = True
        chn_nlziet.Channel.blocked_reason = ""
        with patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel.log_on()
        self.assertFalse(result)
        msg = mock_dialog.call_args[0][1]
        self.assertNotIn("()", msg)


    def test_log_on_credentials_fail_blocked_shows_blocked_message(self) -> None:
        """is_blocked → AccountBlocked dialog shown, returns False without attempting credentials."""

        import chn_nlziet
        chn_nlziet.Channel.is_blocked = True
        chn_nlziet.Channel.blocked_reason = "Onderhoud"
        with patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel.log_on()
        self.assertFalse(result)
        msg = mock_dialog.call_args[0][1]
        self.assertIn("Onderhoud", msg)


    def test_log_on_delegates_to_authenticator(self) -> None:
        """log_on() calls authenticator.log_on() when not blocked."""

        from resources.lib.authentication.authenticationresult import AuthenticationResult
        mock_result = AuthenticationResult("user@test.nl")
        mock_result.logged_on = False
        with patch.object(self.channel._authenticator, "log_on",
                          return_value=mock_result) as mock_auth_log_on:
            self.channel.log_on()
        mock_auth_log_on.assert_called_once()


    def test_log_on_success_calls_activate_session(self) -> None:
        """SUCCESS → shows welcome dialog for a fresh login."""

        from resources.lib.authentication.authenticationresult import AuthenticationResult
        mock_result = AuthenticationResult("user@test.nl")
        mock_result.logged_on = True
        mock_result.existing_login = False
        greet_result = AuthenticationResult("user@test.nl")
        with patch.object(self.channel._authenticator, "log_on", return_value=mock_result), \
             patch.object(self.channel._authenticator, "active_authentication", return_value=greet_result), \
             patch.object(self.channel, "_select_profile", return_value=True), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            self.channel.log_on()
        mock_dialog.assert_called_once()


    def test_log_on_resume_skips_greeting(self) -> None:
        """SUCCESS → skips welcome dialog when resuming an existing session."""

        from resources.lib.authentication.authenticationresult import AuthenticationResult
        mock_result = AuthenticationResult("user@test.nl")
        mock_result.logged_on = True
        mock_result.existing_login = True
        with patch.object(self.channel._authenticator, "log_on", return_value=mock_result), \
             patch.object(self.channel, "_select_profile", return_value=True), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            self.channel.log_on()
        mock_dialog.assert_not_called()


    def test_log_on_failed_returns_false(self) -> None:
        """log_on() returns False when authenticator fails to authenticate."""

        from resources.lib.authentication.authenticationresult import AuthenticationResult
        mock_result = AuthenticationResult("")
        mock_result.logged_on = False
        mock_result.existing_login = False
        with patch.object(self.channel._authenticator, "log_on", return_value=mock_result):
            result = self.channel.log_on()
        self.assertFalse(result)


    # -- log_off -----------------------------------------------------------

    def test_log_off_deregisters_device_when_device_flow(self) -> None:
        """log_off() calls authenticator log_off and shows dialog."""

        with patch.object(self.channel._authenticator, "log_off") as mock_log_off, \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog"), \
             patch("xbmc.executebuiltin"):
            self.channel.log_off()
        mock_log_off.assert_called_once()


    def test_log_off_skips_deregister_for_web_flow(self) -> None:
        """log_off() calls the authenticator log_off."""

        with patch.object(self.channel._authenticator, "log_off") as mock_log_off, \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog"), \
             patch("xbmc.executebuiltin"):
            self.channel.log_off()
        mock_log_off.assert_called_once()


    def test_log_off_shows_dialog_and_navigates(self) -> None:
        """log_off() calls authenticator.log_off, shows a dialog, and triggers navigation."""

        with patch.object(self.channel._authenticator, "log_off") as mock_log_off, \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog, \
             patch("xbmc.executebuiltin") as mock_exec:
            self.channel.log_off()
        mock_log_off.assert_called_once_with("", force=True)
        mock_dialog.assert_called_once()
        self.assertGreaterEqual(mock_exec.call_count, 1)

    # -- __list_profiles ---------------------------------------------------

    def test_list_profiles_returns_profiles(self) -> None:
        """__list_profiles() returns profile list from API response."""

        profiles = [{"id": "p1", "displayName": "User1"}, {"id": "p2", "displayName": "Kids"}]
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=json.dumps(profiles)):
            result = self.channel._list_profiles()
        self.assertEqual(result, profiles)


    def test_list_profiles_forwards_merged_headers_to_uri_handler(self) -> None:
        """__list_profiles() merges auth headers and Nlziet-* headers into the request."""

        from resources.lib.authentication.authenticationresult import AuthenticationResult
        mock_auth = {"Authorization": "Bearer tok", "User-Agent": DEVICE_FLOW_USER_AGENT}
        profiles = [{"id": "p1", "displayName": "User1"}]
        with patch.object(type(self.channel._authenticator), "authentication_headers",
                          new_callable=PropertyMock, return_value=mock_auth), \
             patch("resources.lib.urihandler.UriHandler.open",
                   return_value=json.dumps(profiles)) as mock_open:
            self.channel._list_profiles()

        _, kwargs = mock_open.call_args
        actual = kwargs["additional_headers"]
        # Auth headers from authenticator must be present
        self.assertEqual(actual["Authorization"], "Bearer tok")
        # Nlziet-* app-identification headers must also be present
        self.assertIn("Nlziet-AppName", actual)


    def test_list_profiles_empty_response_returns_empty(self) -> None:
        """__list_profiles() returns [] when the API returns an empty response."""

        with patch("resources.lib.urihandler.UriHandler.open", return_value=""):
            result = self.channel._list_profiles()
        self.assertEqual(result, [])


    def test_list_profiles_http_error_returns_empty(self) -> None:
        """__list_profiles() returns [] when the API returns an HTTP error."""

        error_status = UriStatus(code=0, url=None, error=True, reason="fail")
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
             patch.object(UriHandler.instance(), "status", error_status, create=True):
            result = self.channel._list_profiles()
        self.assertEqual(result, [])

    # -- __select_profile_if_needed ----------------------------------------

    def test_select_profile_if_needed_uses_stored_profile_id(self) -> None:
        """Stored profile_id calls set_profile_claim when the current token lacks the profile claim."""

        with patch.object(self.channel, "_get_profile_id", return_value="stored-id"), \
             patch.object(self.channel._handler, "set_profile_claim") as mock_set_profile, \
             patch.object(self.channel, "_set_profile_id") as mock_store:
            self.channel._select_profile()
        mock_set_profile.assert_called_once_with("stored-id")
        mock_store.assert_not_called()


    def test_select_profile_skips_claim_if_token_already_has_profile(self) -> None:
        """_select_profile() skips set_profile_claim when the current token already carries the profile."""

        with patch.object(self.channel, "_get_profile_id", return_value="stored-id"), \
             patch.object(type(self.channel._handler), "token_profile_id",
                          new_callable=PropertyMock, return_value="stored-id"), \
             patch.object(self.channel._handler, "set_profile_claim") as mock_claim, \
             patch.object(self.channel, "_set_profile_id") as mock_store:
            result = self.channel._select_profile()
        self.assertTrue(result)
        mock_claim.assert_not_called()
        mock_store.assert_not_called()


    def test_select_profile_if_needed_auto_selects_single(self) -> None:
        """Single available profile is auto-selected without a dialog."""

        profile = {"id": "p1", "displayName": "User1"}
        with patch.object(self.channel, "_get_profile_id", return_value=""), \
             patch.object(self.channel, "_list_profiles", return_value=[profile]), \
             patch.object(self.channel._handler, "set_profile_claim",
                          return_value=True) as mock_set_profile, \
             patch.object(self.channel, "_set_profile_id") as mock_store, \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_selection_dialog") as mock_dlg:
            self.channel._select_profile()
        mock_set_profile.assert_called_once_with("p1")
        mock_store.assert_called_once_with("p1")
        mock_dlg.assert_not_called()


    def test_select_profile_if_needed_prompts_for_multiple(self) -> None:
        """Multiple profiles trigger a selection dialog; selected profile is set."""

        profiles = [{"id": "p1", "displayName": "User1"}, {"id": "p2", "displayName": "Kids"}]
        with patch.object(self.channel, "_get_profile_id", return_value=""), \
             patch.object(self.channel, "_list_profiles", return_value=profiles), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_selection_dialog",
                   return_value=1) as mock_dlg, \
             patch.object(self.channel._handler, "set_profile_claim",
                          return_value=True) as mock_set_profile, \
             patch.object(self.channel, "_set_profile_id") as mock_store:
            self.channel._select_profile()
        mock_dlg.assert_called_once()
        mock_set_profile.assert_called_once_with("p2")
        mock_store.assert_called_once_with("p2")


    def test_select_profile_if_needed_no_profiles_skips_silently(self) -> None:
        """Empty profile list is handled without crash or profile selection call."""

        with patch.object(self.channel, "_get_profile_id", return_value=""), \
             patch.object(self.channel, "_list_profiles", return_value=[]), \
             patch.object(self.channel._handler, "set_profile_claim") as mock_set_profile:
            self.channel._select_profile()
        mock_set_profile.assert_not_called()


    def test_select_profile_if_needed_cancelled(self) -> None:
        """Cancelling the selection dialog does not call set_profile_claim."""

        profiles = [{"id": "p1", "displayName": "A"}, {"id": "p2", "displayName": "B"}]
        with patch.object(self.channel, "_get_profile_id", return_value=""), \
             patch.object(self.channel, "_list_profiles", return_value=profiles), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_selection_dialog",
                   return_value=-1), \
             patch.object(self.channel._handler, "set_profile_claim") as mock_set_profile:
            self.channel._select_profile()
        mock_set_profile.assert_not_called()

    # -- switch_profile action ---------------------------------------------

    def test_switch_profile_action_requires_login(self) -> None:
        """switch_profile() shows LoginFirst and does nothing when not logged in."""

        with patch.object(type(self.channel), "loggedOn", new_callable=PropertyMock, return_value=False), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog, \
             patch.object(self.channel, "_clear_profile_id") as mock_clear:
            self.channel.switch_profile()
        mock_dialog.assert_called_once()
        mock_clear.assert_not_called()


    def test_switch_profile_action_clears_and_reselects(self) -> None:
        """switch_profile() clears the stored profile and re-runs selection."""

        with patch.object(type(self.channel), 'loggedOn', new_callable=PropertyMock, return_value=True), \
             patch.object(self.channel, "_clear_profile_id") as mock_clear, \
             patch.object(self.channel, "_select_profile") as mock_select, \
             patch("xbmc.executebuiltin"):
            self.channel.switch_profile()
        mock_clear.assert_called_once()
        mock_select.assert_called_once()


    def test_switch_profile_logs_off_on_failure(self) -> None:
        """switch_profile() calls log_off() when profile selection fails."""

        with patch.object(type(self.channel), 'loggedOn', new_callable=PropertyMock, return_value=True), \
             patch.object(self.channel, "_clear_profile_id"), \
             patch.object(self.channel, "_select_profile", return_value=False), \
             patch.object(self.channel, "log_off") as mock_log_off, \
             patch("xbmc.executebuiltin") as mock_refresh:
            self.channel.switch_profile()
        mock_log_off.assert_called_once()
        mock_refresh.assert_not_called()

    # -- __profile_type ----------------------------------------------------

    def test_profile_type_returns_jwt_claim(self) -> None:
        """__profile_type() returns profileType from the handler's token property."""

        with patch.object(type(self.channel._handler), "token_profile_type",
                          new_callable=PropertyMock, return_value="ChildYoung"):
            result = self.channel._profile_type()
        self.assertEqual(result, "ChildYoung")


    def test_profile_type_returns_empty_when_no_token(self) -> None:
        """__profile_type() returns '' when no token is available."""

        with patch.object(type(self.channel._handler), "token_profile_type",
                          new_callable=PropertyMock, return_value=""):
            result = self.channel._profile_type()
        self.assertEqual(result, "")


    def test_profile_type_returns_empty_for_unscoped_token(self) -> None:
        """__profile_type() returns '' when token carries no profileType claim."""

        with patch.object(type(self.channel._handler), "token_profile_type",
                          new_callable=PropertyMock, return_value=""):
            result = self.channel._profile_type()
        self.assertEqual(result, "")


    # -- process_folder_list: mocked equivalent of TestNlzietChannelLive ---

    def test_process_folder_list_returns_live_channel_items(self) -> None:
        """SUCCESS → navigating into the Live TV folder yields one item per EPG channel."""

        with patch.object(self.channel, "log_on", return_value=True), \
             patch.object(self.channel._handler, "get_authentication_token", return_value="tok"), \
             patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
             patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            top_level = self.channel.process_folder_list(None)

        self.assertIsNotNone(top_level)
        live_tv_item = next(i for i in top_level if i.isLive)

        with patch.object(self.channel, "log_on", return_value=True), \
             patch.object(self.channel._handler, "get_authentication_token", return_value="tok"), \
             patch("resources.lib.urihandler.UriHandler.open",
                   return_value=json.dumps(MOCK_EPG_LIVE_RESPONSE)), \
             patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
             patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            items = self.channel.process_folder_list(live_tv_item)

        self.assertIsNotNone(items)
        self.assertGreater(len(items), 0)
        urls = [i.url for i in items]
        expected_ids = [e["channel"]["content"]["id"] for e in MOCK_EPG_LIVE_RESPONSE["data"]]
        for channel_id in expected_ids:
            self.assertTrue(any(f"channel={channel_id}" in u for u in urls),
                            f"No item URL contains channel={channel_id}")

    # -- create_live_channel_item ------------------------------------------

    def _live_result_set(self, channel_id: str = "test-ch-1", title: str = "Test Channel",
                         logo_url: str = "https://example.com/test-ch-1.png",
                         asset_id: str = "abc", program_title: str = "Current Show",
                         missing_feature: Optional[str] = None,
                         landscape_url: str = "",
                         portrait_url: str = "",
                         content_item_id: str = "",
                         content_provider: str = "") -> Dict[str, Any]:
        program_content: Dict[str, Any] = {"assetId": asset_id, "title": program_title}
        if landscape_url or portrait_url:
            program_content["image"] = {
                "landscapeUrl": landscape_url or None,
                "portraitUrl": portrait_url or None,
            }
        if content_item_id:
            program_content["contentItemId"] = content_item_id
        data: Dict[str, Any] = {
            "channel": {
                "content": {
                    "id": channel_id,
                    "title": title,
                    "logo": {"normalUrl": logo_url}
                }
            },
            "programLocations": [{"content": program_content}]
        }
        if content_provider:
            data["channel"]["content"]["contentProvider"] = content_provider
        if missing_feature is not None:
            data["channel"]["missingSubscriptionFeature"] = missing_feature
        return data


    def test_create_live_channel_item_full(self) -> None:
        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        self.assertTrue(item.isLive)
        self.assertTrue(item.isDrmProtected)
        self.assertIn("channel=test-ch-1", item.url)
        self.assertEqual(item.icon, self.channel.icon)
        self.assertEqual(item.poster, "https://example.com/test-ch-1.png")
        self.assertEqual(item.name, "Test Channel")
        self.assertEqual(item.tv_show_title, "Current Show")
        self.assertIsNone(item.subtitle)
        self.assertEqual(item.description, "")
        self.assertEqual(item.metaData["asset_id"], "abc")


    def test_create_live_channel_item_stores_channel_id_in_metadata(self) -> None:
        """SUCCESS → channel_id stored in metaData."""

        result_set = self._live_result_set(channel_id="sbs6")
        item = self.channel.create_live_channel_item(result_set)
        self.assertEqual(item.metaData["channel_id"], "sbs6")


    def test_create_live_channel_item_stores_logo_url_in_metadata(self) -> None:
        """SUCCESS → logo HTTP URL stored in metaData."""

        result_set = self._live_result_set(logo_url="https://example.com/sbs6.png")
        item = self.channel.create_live_channel_item(result_set)
        self.assertEqual(item.metaData["logo_url"], "https://example.com/sbs6.png")


    def test_create_live_channel_item_no_logo_url_omits_metadata_key(self) -> None:
        """SUCCESS → missing logo URL leaves logo_url absent from metaData."""

        result_set = self._live_result_set(logo_url="")
        item = self.channel.create_live_channel_item(result_set)
        self.assertNotIn("logo_url", item.metaData)


    def test_create_live_channel_item_clearlogo_uses_flat_url(self) -> None:
        """SUCCESS → flatUrl present → clearlogo is set to flatUrl."""

        result_set = {
            "channel": {"content": {
                "id": "rtl4",
                "title": "RTL 4",
                "logo": {
                    "normalUrl": "https://example.com/rtl4-normal.png",
                    "flatUrl": "https://example.com/rtl4-flat.png",
                },
            }},
            "programLocations": [],
        }
        item = self.channel.create_live_channel_item(result_set)
        self.assertEqual(item.clearlogo, "https://example.com/rtl4-flat.png")


    def test_create_live_channel_item_clearlogo_falls_back_to_normal_url(self) -> None:
        """flatUrl absent, normalUrl present → clearlogo falls back to normalUrl."""

        result_set = {
            "channel": {"content": {
                "id": "rtl4",
                "title": "RTL 4",
                "logo": {"normalUrl": "https://example.com/rtl4-normal.png"},
            }},
            "programLocations": [],
        }
        item = self.channel.create_live_channel_item(result_set)
        self.assertEqual(item.clearlogo, "https://example.com/rtl4-normal.png")


    def test_create_live_channel_item_clearlogo_empty_when_no_logo(self) -> None:
        """logo key absent → clearlogo is empty."""

        result_set = {
            "channel": {"content": {"id": "rtl4", "title": "RTL 4"}},
            "programLocations": [],
        }
        item = self.channel.create_live_channel_item(result_set)
        self.assertFalse(item.clearlogo)


    def test_create_live_channel_item_stores_content_provider_in_metadata(self) -> None:
        """SUCCESS → contentProvider stored in metaData when present."""

        result_set = self._live_result_set(content_provider="RTL")
        item = self.channel.create_live_channel_item(result_set)
        self.assertEqual(item.metaData["content_provider"], "RTL")


    def test_create_live_channel_item_no_content_provider_omits_metadata_key(self) -> None:
        """SUCCESS → absent contentProvider leaves content_provider out of metaData."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertNotIn("content_provider", item.metaData)


    def test_create_live_channel_item_no_channel(self) -> None:
        """Missing channel dict returns None."""

        self.assertIsNone(self.channel.create_live_channel_item({}))


    def test_create_live_channel_item_no_id(self) -> None:
        """Channel without id returns None."""

        result_set = {"channel": {"content": {"title": "No ID"}}}
        self.assertIsNone(self.channel.create_live_channel_item(result_set))


    def test_create_live_channel_item_paid(self) -> None:
        """Channel with missingSubscriptionFeature is marked paid."""

        result_set = self._live_result_set(missing_feature="ExtraChannelPackage1")
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        self.assertTrue(item.isPaid)


    @staticmethod
    def _detail_response(description: str = "A great show", duration: int = 3600,
                         genres: Optional[list] = None, nicam_age: str = "12",
                         portrait_url: Optional[str] = "https://example.com/portrait.jpg",
                         landscape_url: str = "https://example.com/landscape-detail.jpg",
                         series_title: str = "The Series",
                         series_landscape_url: Optional[str] = None,
                         series_portrait_url: Optional[str] = None,
                         broadcaster_logo_url: Optional[str] = None) -> str:
        series: Dict[str, Any] = {"title": series_title}
        series_image: Dict[str, Any] = {}
        if series_landscape_url:
            series_image["landscapeUrl"] = series_landscape_url
        if series_portrait_url:
            series_image["portraitUrl"] = series_portrait_url
        if series_image:
            series["image"] = series_image
        content: Dict[str, Any] = {
            "description": description,
            "durationInSeconds": duration,
            "genres": genres if genres is not None else [{"name": "Drama"}],
            "nicam": {"age": nicam_age},
            "image": {"portraitUrl": portrait_url, "landscapeUrl": landscape_url},
            "series": series,
        }
        if broadcaster_logo_url:
            content["broadcasters"] = [{"name": "TestBroadcaster", "logoUrl": broadcaster_logo_url}]
        return json.dumps({"content": content})


    def test_create_live_channel_item_sets_fanart_from_landscape_url(self) -> None:
        """Landscape URL from the programme is set as fanart on the item."""

        result_set = self._live_result_set(
            landscape_url="https://example.com/landscape.jpg")
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        self.assertEqual(item.fanart, "https://example.com/landscape.jpg")


    def test_create_live_channel_item_landscape_sets_fanart_not_thumb(self) -> None:
        """SUCCESS → landscape sets fanart; thumb stays as logo; poster is not set."""

        result_set = self._live_result_set(
            logo_url="https://example.com/logo.png",
            landscape_url="https://example.com/landscape.jpg")
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        self.assertEqual(item.icon, self.channel.icon)
        self.assertEqual(item.thumb, "https://example.com/logo.png")
        self.assertEqual(item.fanart, "https://example.com/landscape.jpg")
        self.assertEqual(item.poster, "https://example.com/logo.png")


    def test_create_live_channel_item_programme_portrait_sets_poster(self) -> None:
        """SUCCESS → programme portrait from EPG is set as initial poster."""

        result_set = self._live_result_set(
            portrait_url="https://example.com/prog-portrait.jpg")
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        self.assertEqual(item.poster, "https://example.com/prog-portrait.jpg")


    def test_create_live_channel_item_no_content_item_id_skips_detail_call(self) -> None:
        """No contentItemId means no detail HTTP call is made."""

        result_set = self._live_result_set()
        with patch("resources.lib.urihandler.UriHandler.open") as mock_open:
            self.channel.create_live_channel_item(result_set)
        mock_open.assert_not_called()


    def test_create_live_channel_item_sets_description_duration_genre_mpaa(self) -> None:
        """SUCCESS → description, duration, genre, Mpaa and tv_show_title are all set."""

        result_set = self._live_result_set(content_item_id="item-001")
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=self._detail_response()):
            item = self.channel.create_live_channel_item(result_set)

        from resources.lib.mediaitem import MediaItem as MI
        self.assertEqual(item.description, "[B]The Series[/B]\n\nA great show")
        self.assertEqual(item.get_info_label(MI.LabelDuration), 3600)
        self.assertEqual(item.get_info_label("Genre"), "Drama")
        self.assertEqual(item.get_info_label("Mpaa"), "NICAM 12+")
        self.assertEqual(item.poster, "https://example.com/portrait.jpg")
        self.assertEqual(item.tv_show_title, "The Series")


    def test_create_live_channel_item_allages_does_not_set_mpaa(self) -> None:
        """NICAM age 'AllAges' must not produce an Mpaa label."""

        result_set = self._live_result_set(content_item_id="item-001")
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=self._detail_response(nicam_age="AllAges")):
            item = self.channel.create_live_channel_item(result_set)

        self.assertFalse(item.has_info_label("Mpaa"))


    def test_create_live_channel_item_detail_http_error_leaves_description_empty(self) -> None:
        """HTTP error from detail API leaves description unset."""

        def _error(url: str, **kwargs: Any) -> str:
            UriHandler.instance().status = UriStatus(
                code=503, url=url, error=True, reason="Service Unavailable")
            return ""

        result_set = self._live_result_set(content_item_id="item-001")
        with patch("resources.lib.urihandler.UriHandler.open", side_effect=_error):
            item = self.channel.create_live_channel_item(result_set)

        self.assertEqual(item.description, "")


    def test_create_live_channel_item_series_landscape_overrides_episode_landscape(self) -> None:
        """SUCCESS → series landscape replaces episode landscape as fanart."""

        result_set = self._live_result_set(
            landscape_url="https://example.com/episode-landscape.jpg",
            content_item_id="item-001")
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=self._detail_response(
                       series_landscape_url="https://example.com/series-landscape.jpg")):
            item = self.channel.create_live_channel_item(result_set)

        self.assertEqual(item.fanart, "https://example.com/series-landscape.jpg")


    def test_create_live_channel_item_no_series_landscape_keeps_episode_fanart(self) -> None:
        """SUCCESS → fanart stays as episode landscape when series has no image."""

        result_set = self._live_result_set(
            landscape_url="https://example.com/episode-landscape.jpg",
            content_item_id="item-001")
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=self._detail_response(series_landscape_url=None)):
            item = self.channel.create_live_channel_item(result_set)

        self.assertEqual(item.fanart, "https://example.com/episode-landscape.jpg")


    def test_create_live_channel_item_detail_bad_json_leaves_description_empty(self) -> None:
        """Non-JSON response from detail API leaves description unset."""

        result_set = self._live_result_set(content_item_id="item-001")
        with patch("resources.lib.urihandler.UriHandler.open", return_value="not json"):
            item = self.channel.create_live_channel_item(result_set)

        self.assertEqual(item.description, "")


    def test_create_live_channel_item_series_portrait_sets_poster(self) -> None:
        """SUCCESS → series portrait from detail API is set as poster."""

        result_set = self._live_result_set(content_item_id="item-001")
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=self._detail_response(
                       series_portrait_url="https://example.com/series-portrait.jpg")):
            item = self.channel.create_live_channel_item(result_set)

        self.assertEqual(item.poster, "https://example.com/series-portrait.jpg")


    def test_create_live_channel_item_series_portrait_overrides_programme_portrait(self) -> None:
        """SUCCESS → series portrait overrides programme portrait."""

        result_set = self._live_result_set(
            portrait_url="https://example.com/prog-portrait.jpg",
            content_item_id="item-001")
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=self._detail_response(
                       series_portrait_url="https://example.com/series-portrait.jpg")):
            item = self.channel.create_live_channel_item(result_set)

        self.assertEqual(item.poster, "https://example.com/series-portrait.jpg")


    def test_create_live_channel_item_content_landscape_used_when_no_series_landscape(
            self) -> None:
        """SUCCESS → content.image.landscapeUrl used as fanart when series has no landscape."""

        result_set = self._live_result_set(content_item_id="item-001")
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=self._detail_response(
                       landscape_url="https://example.com/content-landscape.jpg",
                       series_landscape_url=None)):
            item = self.channel.create_live_channel_item(result_set)

        self.assertEqual(item.fanart, "https://example.com/content-landscape.jpg")


    def test_create_live_channel_item_content_portrait_used_when_no_series_portrait(
            self) -> None:
        """SUCCESS → content.image.portraitUrl used as poster when series has no portrait."""

        result_set = self._live_result_set(content_item_id="item-001")
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=self._detail_response(
                       portrait_url="https://example.com/content-portrait.jpg",
                       series_portrait_url=None)):
            item = self.channel.create_live_channel_item(result_set)

        self.assertEqual(item.poster, "https://example.com/content-portrait.jpg")


    def test_create_live_channel_item_broadcaster_logo_used_as_fanart_fallback(self) -> None:
        """SUCCESS → broadcaster logo used as fanart when no landscape is available."""

        result_set = self._live_result_set(content_item_id="item-001")
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=self._detail_response(
                       landscape_url="",
                       series_landscape_url=None,
                       broadcaster_logo_url="https://example.com/broadcaster.png")):
            item = self.channel.create_live_channel_item(result_set)

        self.assertEqual(item.fanart, "https://example.com/broadcaster.png")


    def test_create_live_channel_item_broadcaster_logo_not_used_when_fanart_exists(
            self) -> None:
        """SUCCESS → existing fanart is not replaced by broadcaster logo."""

        result_set = self._live_result_set(
            landscape_url="https://example.com/prog-landscape.jpg",
            content_item_id="item-001")
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=self._detail_response(
                       landscape_url="",
                       series_landscape_url=None,
                       broadcaster_logo_url="https://example.com/broadcaster.png")):
            item = self.channel.create_live_channel_item(result_set)

        self.assertEqual(item.fanart, "https://example.com/prog-landscape.jpg")


    def test_create_live_channel_item_broadcaster_logo_used_as_poster_fallback(self) -> None:
        """SUCCESS → broadcaster logo used as poster when no portrait is available."""

        result_set = self._live_result_set(content_item_id="item-001")
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=self._detail_response(
                       portrait_url=None,
                       series_portrait_url=None,
                       broadcaster_logo_url="https://example.com/broadcaster.png")):
            item = self.channel.create_live_channel_item(result_set)

        self.assertEqual(item.poster, "https://example.com/broadcaster.png")


    def test_create_live_channel_item_item_detail_cache_avoids_second_http_call(self) -> None:
        """Second call with the same content_item_id hits the cache, not the network."""

        result_set = self._live_result_set(content_item_id="item-001")
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=self._detail_response()) as mock_open:
            item1 = self.channel.create_live_channel_item(result_set)
            item2 = self.channel.create_live_channel_item(result_set)

        self.assertEqual(mock_open.call_count, 1)
        self.assertEqual(item2.description, "[B]The Series[/B]\n\nA great show")

    # -- update_live_item --------------------------------------------------

    def test_update_live_item_success(self) -> None:
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


    def test_update_live_item_no_channel_id_returns_item(self) -> None:
        """update_live_item() with a URL that has no channel= returns without crash."""

        from resources.lib.mediaitem import MediaItem
        item = MediaItem("Test", "https://example.com/no-channel-param")
        result = self.channel.update_live_item(item)
        self.assertIsNotNone(result)
        self.assertFalse(result.complete)


    def test_update_live_item_extra_query_params_do_not_corrupt_channel_id(self) -> None:
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


        def capture_open(url: str, **kwargs: Any) -> str:
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


    def test_update_live_item_non_json_handshake_returns_incomplete(self) -> None:
        """update_live_item() with a non-JSON handshake response returns without crash."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)

        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value="<html>Service Unavailable</html>"):
            result = self.channel.update_live_item(item)

        self.assertIsNotNone(result)
        self.assertFalse(result.complete)


    def test_update_live_item_has_no_start_offset(self) -> None:
        """update_live_item() with padding disabled does not add startOffsetInSeconds."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        handshake_response = json.dumps({
            "manifestUrl": "https://example.com/stream.mpd",
            "drm": {"licenseUrl": "https://lic.example.com/", "headers": {}}
        })
        captured_url = []


        def capture_open(url: str, **kwargs: Any) -> str:
            captured_url.append(url)
            return handshake_response


        def no_padding(channel: Any, setting_id: str, value_for_none: Any = None, store: Any = None) -> str:
            return "false" if setting_id == "nlziet_restart_padding" else "0"

        with patch("resources.lib.urihandler.UriHandler.open", side_effect=capture_open), \
             patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
             patch("resources.lib.addonsettings.AddonSettings.get_channel_setting",
                   side_effect=no_padding), \
             patch("resources.lib.streams.mpd.Mpd.get_license_key", return_value="key"), \
             patch("resources.lib.streams.mpd.Mpd.set_input_stream_addon_input"):
            updated = self.channel.update_live_item(item)

        self.assertTrue(updated.complete)
        self.assertFalse(any("startOffsetInSeconds" in u for u in captured_url))

    # -- update_live_item: restart padding + live offset ---------------------

    def _channel_setting_side_effect(self, padding_value: str, slider_value: str = "0") -> Any:
        def _side_effect(channel: Any, setting_id: str, value_for_none: Any = None, store: Any = None) -> Optional[str]:
            if setting_id == "nlziet_restart_padding":
                return padding_value
            if setting_id == "nlziet_live_start_offset":
                return slider_value
            return None
        return _side_effect


    def _make_live_update_mocks(self, padding_value: str, slider_value: str = "0",
                                appconfig_padding: int = 0) -> Any:
        """Return (captured_url, context_managers) for update_live_item tests."""

        handshake_response = json.dumps({
            "manifestUrl": "https://example.com/stream.mpd",
            "drm": {"licenseUrl": "https://lic.example.com/", "headers": {}}
        })
        appconfig_json = json.dumps({"liveStreamRestartStartPadding": appconfig_padding})
        captured_url = []


        def capture_open(url: str, **kwargs: Any) -> str:
            captured_url.append(url)
            return handshake_response


        def get_setting_side_effect(setting_id: str, store: Any = None) -> str:
            if setting_id == "nlziet_appconfig":
                return appconfig_json
            return ""

        return captured_url, [
            patch("resources.lib.urihandler.UriHandler.open", side_effect=capture_open),
            patch("resources.lib.addonsettings.AddonSettings.get_setting",
                  side_effect=get_setting_side_effect),
            patch("resources.lib.addonsettings.AddonSettings.get_channel_setting",
                  side_effect=self._channel_setting_side_effect(padding_value, slider_value)),
            patch("resources.lib.streams.mpd.Mpd.get_license_key", return_value="key"),
            patch("resources.lib.streams.mpd.Mpd.set_input_stream_addon_input"),
        ]


    def test_update_live_item_restart_padding_appends_offset(self) -> None:
        """update_live_item() with padding on uses liveStreamRestartStartPadding as offset."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        captured_url, mocks = self._make_live_update_mocks("true", "0", appconfig_padding=120)
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
            updated = self.channel.update_live_item(item)
        self.assertTrue(updated.complete)
        self.assertTrue(any("startOffsetInSeconds=120" in u for u in captured_url))


    def test_update_live_item_positive_slider_adds_to_padding(self) -> None:
        """update_live_item() adds slider value on top of padding offset."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        captured_url, mocks = self._make_live_update_mocks("true", "30", appconfig_padding=120)
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
            updated = self.channel.update_live_item(item)
        self.assertTrue(updated.complete)
        self.assertTrue(any("startOffsetInSeconds=150" in u for u in captured_url))


    def test_update_live_item_negative_slider_reduces_offset(self) -> None:
        """update_live_item() subtracts negative slider from padding without going below 0."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        captured_url, mocks = self._make_live_update_mocks("true", "-60", appconfig_padding=120)
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
            updated = self.channel.update_live_item(item)
        self.assertTrue(updated.complete)
        self.assertTrue(any("startOffsetInSeconds=60" in u for u in captured_url))


    def test_update_live_item_total_offset_clamped_to_zero(self) -> None:
        """update_live_item() omits startOffsetInSeconds when combined total is negative."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        captured_url, mocks = self._make_live_update_mocks("true", "-120", appconfig_padding=30)
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
            updated = self.channel.update_live_item(item)
        self.assertTrue(updated.complete)
        self.assertFalse(any("startOffsetInSeconds" in u for u in captured_url))


    def test_update_live_item_padding_off_no_offset(self) -> None:
        """update_live_item() omits startOffsetInSeconds when padding toggle is off."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        captured_url, mocks = self._make_live_update_mocks("false", "0", appconfig_padding=180)
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
            updated = self.channel.update_live_item(item)
        self.assertTrue(updated.complete)
        self.assertFalse(any("startOffsetInSeconds" in u for u in captured_url))


    def test_update_live_item_offset_sets_manifest_config(self) -> None:
        """update_live_item() sets inputstream.adaptive.manifest_config when offset > 0."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        captured_url, mocks = self._make_live_update_mocks("true", "0", appconfig_padding=90)
        with mocks[0], mocks[1], mocks[2], mocks[3], mocks[4]:
            updated = self.channel.update_live_item(item)
        self.assertTrue(updated.complete)
        stream_props = dict(updated.streams[-1].Properties)
        self.assertIn("inputstream.adaptive.manifest_config", stream_props)
        config = json.loads(stream_props["inputstream.adaptive.manifest_config"])
        self.assertEqual(config["live_offset"], 90)


    # -- update_live_item: playerName per flow -----------------------------

    def _make_player_name_mocks(self) -> Any:
        handshake_response = json.dumps({
            "manifestUrl": "https://example.com/stream.mpd",
            "drm": {"licenseUrl": "https://lic.example.com/", "headers": {}}
        })
        captured_url = []

        def capture_open(url: str, **kwargs: Any) -> str:
            captured_url.append(url)
            return handshake_response

        return captured_url, [
            patch("resources.lib.urihandler.UriHandler.open", side_effect=capture_open),
            patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""),
            patch("resources.lib.streams.mpd.Mpd.get_license_key", return_value="key"),
            patch("resources.lib.streams.mpd.Mpd.set_input_stream_addon_input"),
        ]


    def test_update_live_item_web_flow_uses_web_player_name(self) -> None:
        """update_live_item() sends playerName=BitmovinWeb when _player_name is the web value."""

        import chn_nlziet

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        captured_url, mocks = self._make_player_name_mocks()
        self.channel._player_name = chn_nlziet.NLZIET_PLAYER_NAME_WEB

        with mocks[0], mocks[1], mocks[2], mocks[3]:
            self.channel.update_live_item(item)

        self.assertTrue(
            any(f"playerName={chn_nlziet.NLZIET_PLAYER_NAME_WEB}" in u for u in captured_url),
            f"Expected BitmovinWeb playerName in handshake URL, got: {captured_url}")


    def test_update_live_item_device_flow_uses_device_player_name(self) -> None:
        """update_live_item() sends playerName=NLZIETAndroidTVExoPlayer when _player_name is the device value."""

        import chn_nlziet

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        captured_url, mocks = self._make_player_name_mocks()
        self.channel._player_name = chn_nlziet.NLZIET_PLAYER_NAME_DEVICE

        with mocks[0], mocks[1], mocks[2], mocks[3]:
            self.channel.update_live_item(item)

        self.assertTrue(
            any(f"playerName={chn_nlziet.NLZIET_PLAYER_NAME_DEVICE}" in u for u in captured_url),
            f"Expected NLZIETAndroidTVExoPlayer playerName in handshake URL, got: {captured_url}")


    # -- __init__ device-flow branch -----------------------

    def test_init_device_flow_uses_device_app_name(self) -> None:
        """Channel.__init__ uses NLZIET_APP_NAME/VERSION when the device client is active."""

        from resources.lib.addonsettings import AddonSettings, LOCAL
        import chn_nlziet

        channel_guid = self.channel.guid
        orig = AddonSettings.get_channel_setting(channel_guid, "authentication_method", store=LOCAL)
        AddonSettings.set_channel_setting(channel_guid, "authentication_method", "device_auth", store=LOCAL)
        try:
            channel = self._switch_channel(None)
            self.assertEqual(channel._nlziet_headers["Nlziet-AppName"], chn_nlziet.NLZIET_APP_NAME)
            self.assertEqual(channel._nlziet_headers["Nlziet-AppVersion"], chn_nlziet.NLZIET_APP_VERSION)
            self.assertEqual(channel._player_name, chn_nlziet.NLZIET_PLAYER_NAME_DEVICE)
        finally:
            AddonSettings.set_channel_setting(channel_guid, "authentication_method", orig or "", store=LOCAL)
            self._switch_channel(None)

    # -- _get_live_restart_padding exception ---------------

    def test_get_live_restart_padding_bad_json_returns_zero(self) -> None:
        """_get_live_restart_padding returns 0 when the cached appconfig is corrupt JSON."""

        with patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value="[corrupt"):
            result = self.channel._get_live_restart_padding()
        self.assertEqual(result, 0)

    # -- _handle_stream_handshake branches ---------------------------------

    def test_update_live_item_http_error_returns_incomplete(self) -> None:
        """update_live_item() with an HTTP error returns an incomplete item."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        UriHandler.instance().status = UriStatus(code=503, url=None, error=True, reason="Service Unavailable")
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""):
            result = self.channel.update_live_item(item)
        self.assertFalse(result.complete)

    def test_update_live_item_errors_string_list_returns_incomplete(self) -> None:
        """String-list error in handshake marks item incomplete."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        response = json.dumps({"errors": ["Access denied — subscription required"]})
        with patch("resources.lib.urihandler.UriHandler.open", return_value=response), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""):
            result = self.channel.update_live_item(item)
        self.assertFalse(result.complete)

    def test_update_live_item_errors_empty_dict_values_returns_incomplete(self) -> None:
        """Dict errors whose values flatten to [] are handled gracefully."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        response = json.dumps({"errors": {"field": []}})
        with patch("resources.lib.urihandler.UriHandler.open", return_value=response), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""):
            result = self.channel.update_live_item(item)
        self.assertFalse(result.complete)

    def test_update_live_item_errors_typed_dict_returns_incomplete(self) -> None:
        """Typed error object in handshake is logged and item is incomplete."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        response = json.dumps({"errors": [{"type": "Unauthorized", "message": "Not allowed"}]})
        with patch("resources.lib.urihandler.UriHandler.open", return_value=response), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""):
            result = self.channel.update_live_item(item)
        self.assertFalse(result.complete)

    def test_update_live_item_errors_max_streams_shows_dialog(self) -> None:
        """MaximumStreamsReached error triggers the dialog."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        response = json.dumps({
            "errors": [{"type": "MaximumStreamsReached",
                        "data": {"maximumNumberOfStreams": 2},
                        "message": "Max streams reached"}]
        })
        with patch("resources.lib.urihandler.UriHandler.open", return_value=response), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel.update_live_item(item)
        self.assertFalse(result.complete)
        mock_dialog.assert_called_once()

    def test_update_live_item_errors_missing_subscription_shows_dialog(self) -> None:
        """MissingSubscriptionFeature error triggers the subscription dialog."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        response = json.dumps({
            "errors": [{"type": "MissingSubscriptionFeature", "message": "No subscription"}]
        })
        with patch("resources.lib.urihandler.UriHandler.open", return_value=response), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel.update_live_item(item)
        self.assertFalse(result.complete)
        mock_dialog.assert_called_once()


    def test_update_live_item_errors_unauthorized_logged_on_shows_dialog(self) -> None:
        """Unauthorized error while session is active shows the stream dialog without login prompt."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        response = json.dumps({
            "errors": [{"type": "Unauthorized", "message": "Not allowed"}]
        })
        with patch("resources.lib.urihandler.UriHandler.open", return_value=response), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch.object(type(self.channel), "loggedOn",
                             new_callable=PropertyMock, return_value=True), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel.update_live_item(item)
        self.assertFalse(result.complete)
        mock_dialog.assert_called_once()
        dialog_msg = mock_dialog.call_args[0][1]
        self.assertNotIn("\n", dialog_msg)


    def test_update_live_item_errors_unauthorized_logged_out_appends_login_prompt(self) -> None:
        """Unauthorized error with expired session appends the login reminder."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        response = json.dumps({
            "errors": [{"type": "Unauthorized", "message": "Not allowed"}]
        })
        with patch("resources.lib.urihandler.UriHandler.open", return_value=response), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch.object(type(self.channel), "loggedOn",
                             new_callable=PropertyMock, return_value=False), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel.update_live_item(item)
        self.assertFalse(result.complete)
        mock_dialog.assert_called_once()
        dialog_msg = mock_dialog.call_args[0][1]
        self.assertIn("\n", dialog_msg)


    def test_update_live_item_errors_channel_not_found_shows_dialog(self) -> None:
        """ChannelNotFound error triggers the channel-unavailable dialog."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        response = json.dumps({
            "errors": [{"type": "ChannelNotFound", "message": "Channel not found"}]
        })
        with patch("resources.lib.urihandler.UriHandler.open", return_value=response), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel.update_live_item(item)
        self.assertFalse(result.complete)
        mock_dialog.assert_called_once()


    def test_update_live_item_errors_invalid_asset_shows_dialog(self) -> None:
        """InvalidAsset error triggers the content-not-playable dialog."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        response = json.dumps({
            "errors": [{"type": "InvalidAsset", "message": "Asset invalid"}]
        })
        with patch("resources.lib.urihandler.UriHandler.open", return_value=response), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel.update_live_item(item)
        self.assertFalse(result.complete)
        mock_dialog.assert_called_once()


    def test_update_live_item_no_manifest_url_returns_incomplete(self) -> None:
        """Handshake response without manifestUrl returns an incomplete item."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        response = json.dumps({})
        with patch("resources.lib.urihandler.UriHandler.open", return_value=response), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""):
            result = self.channel.update_live_item(item)
        self.assertFalse(result.complete)

    def test_update_live_item_no_drm_completes_without_license(self) -> None:
        """Handshake with manifestUrl but no DRM completes the item without a license key (line 284)."""

        result_set = self._live_result_set()
        item = self.channel.create_live_channel_item(result_set)
        self.assertIsNotNone(item)
        response = json.dumps({"manifestUrl": "https://example.com/stream.mpd"})
        with patch("resources.lib.urihandler.UriHandler.open", return_value=response), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.streams.mpd.Mpd.set_input_stream_addon_input"):
            result = self.channel.update_live_item(item)
        self.assertTrue(result.complete)

    # -- _set_profile_id / _clear_profile_id ------

    def test_set_profile_id_stores_value(self) -> None:
        """_set_profile_id() persists the profile ID via AddonSettings."""

        with patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel._set_profile_id("test-profile-uuid")
        self.assertEqual(mock_set.call_count, 1)
        args, _ = mock_set.call_args
        self.assertEqual(args[0], "nlziet_profile_id")
        self.assertEqual(args[1], "test-profile-uuid")

    def test_clear_profile_id_clears_stored_value(self) -> None:
        """_clear_profile_id() writes an empty string to the profile ID setting."""

        with patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel._clear_profile_id()
        self.assertEqual(mock_set.call_count, 1)
        args, _ = mock_set.call_args
        self.assertEqual(args[0], "nlziet_profile_id")
        self.assertEqual(args[1], "")

    # -- _list_profiles exception --------------------------

    def test_list_profiles_parse_exception_returns_empty(self) -> None:
        """_list_profiles() returns [] when JSON parsing raises an exception."""

        with patch("resources.lib.urihandler.UriHandler.open", return_value="[1,2,3]"), \
                patch("chn_nlziet.JsonHelper", side_effect=Exception("unexpected parse error")):
            result = self.channel._list_profiles()
        self.assertEqual(result, [])

    # -- _select_profile failure paths -----------

    def test_select_profile_stored_claim_fails_clears_and_retries(self) -> None:
        """Stale stored profile clears itself and retries with fresh list."""

        profiles = [{"id": "new-id", "displayName": "New User"}]
        with patch.object(self.channel, "_get_profile_id", return_value="stale-id"), \
                patch.object(type(self.channel._handler), "token_profile_id",
                             new_callable=PropertyMock, return_value="other-id"), \
                patch.object(self.channel._handler, "set_profile_claim", side_effect=[False, True]), \
                patch.object(self.channel, "_list_profiles", return_value=profiles), \
                patch.object(self.channel, "_set_profile_id"), \
                patch.object(self.channel, "_clear_profile_id") as mock_clear:
            result = self.channel._select_profile()
        mock_clear.assert_called_once()
        self.assertTrue(result)

    def test_select_profile_single_auto_select_claim_fails_returns_false(self) -> None:
        """Auto-selecting the only profile returns False when set_profile_claim fails (line 490)."""

        profile = {"id": "p1", "displayName": "User1"}
        with patch.object(self.channel, "_get_profile_id", return_value=""), \
                patch.object(self.channel, "_list_profiles", return_value=[profile]), \
                patch.object(self.channel._handler, "set_profile_claim", return_value=False):
            result = self.channel._select_profile()
        self.assertFalse(result)

    def test_select_profile_multi_select_claim_fails_returns_false(self) -> None:
        """Multi-profile selection returns False when set_profile_claim fails (line 505)."""

        profiles = [{"id": "p1", "displayName": "A"}, {"id": "p2", "displayName": "B"}]
        with patch.object(self.channel, "_get_profile_id", return_value=""), \
                patch.object(self.channel, "_list_profiles", return_value=profiles), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_selection_dialog", return_value=0), \
                patch.object(self.channel._handler, "set_profile_claim", return_value=False):
            result = self.channel._select_profile()
        self.assertFalse(result)

    # -- log_on: select_profile failure → log_off ---------

    def test_log_on_select_profile_fails_logs_off_returns_false(self) -> None:
        """log_on() calls log_off and returns False when profile selection fails."""

        from resources.lib.authentication.authenticationresult import AuthenticationResult
        mock_result = AuthenticationResult("user@test.nl")
        mock_result.logged_on = True
        mock_result.existing_login = True
        with patch.object(self.channel._authenticator, "log_on", return_value=mock_result), \
                patch.object(self.channel, "_select_profile", return_value=False), \
                patch.object(self.channel, "log_off") as mock_log_off:
            result = self.channel.log_on()
        self.assertFalse(result)
        mock_log_off.assert_called_once()

    # -- _sync_appconfig error paths -------------

    def test_sync_appconfig_http_error_returns_early(self) -> None:
        """_sync_appconfig() returns early without writing when the HTTP request fails."""

        UriHandler.instance().status = UriStatus(code=403, url=None, error=True, reason="Forbidden")
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel._sync_appconfig()
        mock_set.assert_not_called()

    def test_sync_appconfig_bad_cached_value_handles_gracefully(self) -> None:
        """_sync_appconfig() treats corrupt cached JSON as an empty baseline without raising."""

        import chn_nlziet
        good_response = json.dumps({"heartbeatInterval": 120, "isAppBlocked": False})

        def get_setting_side_effect(setting_id: str, store: Any = None) -> str:
            if setting_id == chn_nlziet.APPCONFIG_CACHE_KEY:
                return "[corrupt-json{"
            return ""

        with patch("resources.lib.urihandler.UriHandler.open", return_value=good_response), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting",
                      side_effect=get_setting_side_effect), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel._sync_appconfig()
        self.assertEqual(chn_nlziet.Channel.service_interval, 120)


    # -- _add_metadata_item: genre label -----------------------------------

    @staticmethod
    def _make_item() -> "MediaItem":
        from resources.lib.mediaitem import MediaItem
        return MediaItem("Test Item", "https://example.com")


    def test_genre_single_name_is_set_as_label(self) -> None:
        """SUCCESS → single genre name is written as the Genre info-label."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["g-1"] = {"genres": [{"name": "Drama"}]}
        self.channel._add_metadata_item(item, "g-1")
        self.assertEqual(item.get_info_label("Genre"), "Drama")


    def test_genre_multiple_names_joined_with_comma(self) -> None:
        """SUCCESS → multiple genre names are joined with \", \"."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["g-2"] = {
            "genres": [{"name": "Drama"}, {"name": "Thriller"}]
        }
        self.channel._add_metadata_item(item, "g-2")
        self.assertEqual(item.get_info_label("Genre"), "Drama, Thriller")


    def test_genre_absent_key_does_not_set_label(self) -> None:
        """genres key absent from detail dict → Genre label is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["g-3"] = {}
        self.channel._add_metadata_item(item, "g-3")
        self.assertFalse(item.has_info_label("Genre"))


    def test_genre_empty_list_does_not_set_label(self) -> None:
        """genres is an empty list → Genre label is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["g-4"] = {"genres": []}
        self.channel._add_metadata_item(item, "g-4")
        self.assertFalse(item.has_info_label("Genre"))


    def test_genre_all_missing_name_key_does_not_set_label(self) -> None:
        """All genre dicts lack the 'name' key → Genre label is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["g-5"] = {
            "genres": [{"id": "1"}, {"id": "2"}]
        }
        self.channel._add_metadata_item(item, "g-5")
        self.assertFalse(item.has_info_label("Genre"))


    def test_genre_all_empty_string_names_does_not_set_label(self) -> None:
        """All genre names are empty strings → Genre label is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["g-6"] = {
            "genres": [{"name": ""}, {"name": ""}]
        }
        self.channel._add_metadata_item(item, "g-6")
        self.assertFalse(item.has_info_label("Genre"))


    def test_genre_all_none_names_does_not_set_label(self) -> None:
        """All genre names are None → Genre label is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["g-7"] = {"genres": [{"name": None}]}
        self.channel._add_metadata_item(item, "g-7")
        self.assertFalse(item.has_info_label("Genre"))


    def test_genre_filters_out_dicts_without_name_key(self) -> None:
        """Genre dicts missing 'name' key are skipped; named entries are joined."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["g-8"] = {
            "genres": [{"name": "Drama"}, {"id": "no-name"}]
        }
        self.channel._add_metadata_item(item, "g-8")
        self.assertEqual(item.get_info_label("Genre"), "Drama")


    def test_genre_filters_out_empty_string_name(self) -> None:
        """Genre dict with empty string name is skipped; named entries are joined."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["g-9"] = {
            "genres": [{"name": "Drama"}, {"name": ""}]
        }
        self.channel._add_metadata_item(item, "g-9")
        self.assertEqual(item.get_info_label("Genre"), "Drama")


    def test_genre_filters_out_none_name(self) -> None:
        """Genre dict with None name is skipped; named entries are joined."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["g-10"] = {
            "genres": [{"name": "Drama"}, {"name": None}]
        }
        self.channel._add_metadata_item(item, "g-10")
        self.assertEqual(item.get_info_label("Genre"), "Drama")


    # -- _add_metadata_item: nicam/Mpaa label ------------------------------

    def test_nicam_age_sets_mpaa_label(self) -> None:
        """SUCCESS → numeric NICAM age is formatted and set as the Mpaa label."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["n-1"] = {"nicam": {"age": "16"}}
        self.channel._add_metadata_item(item, "n-1")
        self.assertEqual(item.get_info_label("Mpaa"), "NICAM 16+")


    def test_nicam_allages_does_not_set_mpaa_label(self) -> None:
        """AllAges NICAM rating → Mpaa label is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["n-2"] = {"nicam": {"age": "AllAges"}}
        self.channel._add_metadata_item(item, "n-2")
        self.assertFalse(item.has_info_label("Mpaa"))


    def test_nicam_key_absent_does_not_set_mpaa_label(self) -> None:
        """nicam key absent from detail → Mpaa label is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["n-3"] = {}
        self.channel._add_metadata_item(item, "n-3")
        self.assertFalse(item.has_info_label("Mpaa"))


    def test_nicam_empty_dict_does_not_set_mpaa_label(self) -> None:
        """nicam present but empty → Mpaa label is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["n-4"] = {"nicam": {}}
        self.channel._add_metadata_item(item, "n-4")
        self.assertFalse(item.has_info_label("Mpaa"))


    def test_nicam_age_key_absent_does_not_set_mpaa_label(self) -> None:
        """nicam present but age key absent → Mpaa label is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["n-5"] = {"nicam": {"locale": "nl"}}
        self.channel._add_metadata_item(item, "n-5")
        self.assertFalse(item.has_info_label("Mpaa"))


    def test_nicam_age_empty_string_does_not_set_mpaa_label(self) -> None:
        """nicam age is empty string → Mpaa label is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["n-6"] = {"nicam": {"age": ""}}
        self.channel._add_metadata_item(item, "n-6")
        self.assertFalse(item.has_info_label("Mpaa"))


    def test_nicam_age_none_does_not_set_mpaa_label(self) -> None:
        """nicam age is None → Mpaa label is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["n-7"] = {"nicam": {"age": None}}
        self.channel._add_metadata_item(item, "n-7")
        self.assertFalse(item.has_info_label("Mpaa"))


    # -- _add_metadata_item: series_title ----------------------------------

    def test_series_title_is_set_on_item(self) -> None:
        """SUCCESS → series title is written to tv_show_title."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["s-1"] = {"series": {"title": "Breaking Bad"}}
        self.channel._add_metadata_item(item, "s-1")
        self.assertEqual(item.tv_show_title, "Breaking Bad")


    def test_series_key_absent_does_not_set_tv_show_title(self) -> None:
        """series key absent from detail → tv_show_title is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["s-2"] = {}
        self.channel._add_metadata_item(item, "s-2")
        self.assertFalse(item.tv_show_title)


    def test_series_empty_dict_does_not_set_tv_show_title(self) -> None:
        """series present but empty → tv_show_title is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["s-3"] = {"series": {}}
        self.channel._add_metadata_item(item, "s-3")
        self.assertFalse(item.tv_show_title)


    def test_series_title_key_absent_does_not_set_tv_show_title(self) -> None:
        """series present but title key absent → tv_show_title is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["s-4"] = {"series": {"id": "123"}}
        self.channel._add_metadata_item(item, "s-4")
        self.assertFalse(item.tv_show_title)


    def test_series_title_empty_string_does_not_set_tv_show_title(self) -> None:
        """series title is empty string → tv_show_title is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["s-5"] = {"series": {"title": ""}}
        self.channel._add_metadata_item(item, "s-5")
        self.assertFalse(item.tv_show_title)


    def test_series_title_none_does_not_set_tv_show_title(self) -> None:
        """series title is None → tv_show_title is not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["s-6"] = {"series": {"title": None}}
        self.channel._add_metadata_item(item, "s-6")
        self.assertFalse(item.tv_show_title)


    # -- _add_metadata_item: series_image ----------------------------------

    def test_series_image_landscape_sets_fanart(self) -> None:
        """SUCCESS → series landscapeUrl is set as fanart."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["si-1"] = {
            "series": {"image": {"landscapeUrl": "https://example.com/series-land.jpg"}}
        }
        self.channel._add_metadata_item(item, "si-1")
        self.assertEqual(item.fanart, "https://example.com/series-land.jpg")


    def test_series_image_portrait_sets_poster(self) -> None:
        """SUCCESS → series portraitUrl is set as poster."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["si-2"] = {
            "series": {"image": {"portraitUrl": "https://example.com/series-port.jpg"}}
        }
        self.channel._add_metadata_item(item, "si-2")
        self.assertEqual(item.poster, "https://example.com/series-port.jpg")


    def test_series_image_empty_dict_does_not_set_fanart_or_poster(self) -> None:
        """series image present but empty → fanart and poster not set from series."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["si-3"] = {"series": {"image": {}}}
        self.channel._add_metadata_item(item, "si-3")
        self.assertFalse(item.fanart)
        self.assertFalse(item.poster)


    def test_series_image_landscape_none_does_not_set_fanart(self) -> None:
        """series landscapeUrl is None → fanart not set from series."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["si-4"] = {
            "series": {"image": {"landscapeUrl": None}}
        }
        self.channel._add_metadata_item(item, "si-4")
        self.assertFalse(item.fanart)


    def test_series_image_portrait_none_does_not_set_poster(self) -> None:
        """series portraitUrl is None → poster not set from series."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["si-5"] = {
            "series": {"image": {"portraitUrl": None}}
        }
        self.channel._add_metadata_item(item, "si-5")
        self.assertFalse(item.poster)


    # -- _add_metadata_item: broadcasters ----------------------------------


    def test_broadcaster_logo_sets_fanart_and_poster_as_fallback(self) -> None:
        """SUCCESS → broadcasters[0].logoUrl sets fanart and poster when no other images."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["b-1"] = {
            "broadcasters": [{"logoUrl": "https://example.com/logo.png"}]
        }
        self.channel._add_metadata_item(item, "b-1")
        self.assertEqual(item.fanart, "https://example.com/logo.png")
        self.assertEqual(item.poster, "https://example.com/logo.png")


    def test_broadcaster_logo_not_used_when_series_landscape_present(self) -> None:
        """series landscapeUrl takes priority over broadcaster logo for fanart."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["b-2"] = {
            "broadcasters": [{"logoUrl": "https://example.com/logo.png"}],
            "series": {"image": {"landscapeUrl": "https://example.com/series-land.jpg"}},
        }
        self.channel._add_metadata_item(item, "b-2")
        self.assertEqual(item.fanart, "https://example.com/series-land.jpg")


    def test_broadcaster_logo_not_used_when_content_landscape_present(self) -> None:
        """detail image landscapeUrl takes priority over broadcaster logo for fanart."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["b-3"] = {
            "broadcasters": [{"logoUrl": "https://example.com/logo.png"}],
            "image": {"landscapeUrl": "https://example.com/content-land.jpg"},
        }
        self.channel._add_metadata_item(item, "b-3")
        self.assertEqual(item.fanart, "https://example.com/content-land.jpg")


    def test_broadcaster_key_absent_does_not_set_fanart(self) -> None:
        """broadcasters key absent → fanart not set from broadcaster."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["b-4"] = {}
        self.channel._add_metadata_item(item, "b-4")
        self.assertFalse(item.fanart)
        self.assertFalse(item.poster)


    def test_broadcaster_empty_list_does_not_set_fanart(self) -> None:
        """broadcasters is empty list → fanart not set from broadcaster."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["b-5"] = {"broadcasters": []}
        self.channel._add_metadata_item(item, "b-5")
        self.assertFalse(item.fanart)
        self.assertFalse(item.poster)


    def test_broadcaster_logo_url_key_absent_does_not_set_fanart(self) -> None:
        """broadcasters[0] has no logoUrl key → fanart not set from broadcaster."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["b-6"] = {"broadcasters": [{"name": "RTL"}]}
        self.channel._add_metadata_item(item, "b-6")
        self.assertFalse(item.fanart)
        self.assertFalse(item.poster)


    def test_broadcaster_logo_url_none_does_not_set_fanart(self) -> None:
        """broadcasters[0].logoUrl is None → fanart not set from broadcaster."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["b-7"] = {
            "broadcasters": [{"logoUrl": None}]
        }
        self.channel._add_metadata_item(item, "b-7")
        self.assertFalse(item.fanart)
        self.assertFalse(item.poster)


    def test_broadcaster_logo_url_empty_string_does_not_set_fanart(self) -> None:
        """broadcasters[0].logoUrl is empty string → fanart not set from broadcaster."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["b-8"] = {
            "broadcasters": [{"logoUrl": ""}]
        }
        self.channel._add_metadata_item(item, "b-8")
        self.assertFalse(item.fanart)
        self.assertFalse(item.poster)


    # -- _add_metadata_item: detail image ----------------------------------


    def test_detail_image_landscape_sets_fanart(self) -> None:
        """SUCCESS → detail image landscapeUrl is set as fanart when no series images."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["di-1"] = {
            "image": {"landscapeUrl": "https://example.com/content-land.jpg"}
        }
        self.channel._add_metadata_item(item, "di-1")
        self.assertEqual(item.fanart, "https://example.com/content-land.jpg")


    def test_detail_image_portrait_sets_poster(self) -> None:
        """SUCCESS → detail image portraitUrl is set as poster when no series images."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["di-2"] = {
            "image": {"portraitUrl": "https://example.com/content-port.jpg"}
        }
        self.channel._add_metadata_item(item, "di-2")
        self.assertEqual(item.poster, "https://example.com/content-port.jpg")


    def test_detail_image_key_absent_does_not_set_fanart_or_poster(self) -> None:
        """image key absent → fanart and poster not set from detail image."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["di-3"] = {}
        self.channel._add_metadata_item(item, "di-3")
        self.assertFalse(item.fanart)
        self.assertFalse(item.poster)


    def test_detail_image_empty_dict_does_not_set_fanart_or_poster(self) -> None:
        """image key present but empty dict → fanart and poster not set from detail image."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["di-4"] = {"image": {}}
        self.channel._add_metadata_item(item, "di-4")
        self.assertFalse(item.fanart)
        self.assertFalse(item.poster)


    def test_detail_image_landscape_none_does_not_set_fanart(self) -> None:
        """detail image landscapeUrl is None → fanart not set from detail image."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["di-5"] = {
            "image": {"landscapeUrl": None}
        }
        self.channel._add_metadata_item(item, "di-5")
        self.assertFalse(item.fanart)


    def test_detail_image_portrait_none_does_not_set_poster(self) -> None:
        """detail image portraitUrl is None → poster not set from detail image."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["di-6"] = {
            "image": {"portraitUrl": None}
        }
        self.channel._add_metadata_item(item, "di-6")
        self.assertFalse(item.poster)


    def test_detail_image_series_landscape_takes_priority_over_content(self) -> None:
        """series landscapeUrl takes priority over detail image landscapeUrl for fanart."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["di-7"] = {
            "image": {"landscapeUrl": "https://example.com/content-land.jpg"},
            "series": {"image": {"landscapeUrl": "https://example.com/series-land.jpg"}},
        }
        self.channel._add_metadata_item(item, "di-7")
        self.assertEqual(item.fanart, "https://example.com/series-land.jpg")


    # -- _add_metadata_item: description -----------------------------------


    def test_description_key_absent_does_not_set_description(self) -> None:
        """description key absent → item.description not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["desc-1"] = {}
        self.channel._add_metadata_item(item, "desc-1")
        self.assertFalse(item.description)


    def test_description_empty_string_does_not_set_description(self) -> None:
        """description is empty string → item.description not set."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["desc-2"] = {"description": ""}
        self.channel._add_metadata_item(item, "desc-2")
        self.assertFalse(item.description)


    def test_description_no_titles_set_as_plain_description(self) -> None:
        """description present, no series/episode title → item.description = plain text."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["desc-3"] = {
            "description": "Some episode description."
        }
        self.channel._add_metadata_item(item, "desc-3")
        self.assertEqual(item.description, "Some episode description.")


    def test_description_with_series_title_only(self) -> None:
        """description + series title, no episode title → bold series header prepended."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["desc-4"] = {
            "series": {"title": "My Show"},
            "description": "About this episode.",
        }
        self.channel._add_metadata_item(item, "desc-4")
        self.assertEqual(item.description, "[B]My Show[/B]\n\nAbout this episode.")


    def test_description_with_episode_title_only(self) -> None:
        """description + episode title, no series title → italic episode header prepended."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["desc-5"] = {
            "title": "Episode One",
            "description": "About this episode.",
        }
        self.channel._add_metadata_item(item, "desc-5")
        self.assertEqual(item.description, "[I]Episode One[/I]\n\nAbout this episode.")


    def test_description_with_series_and_episode_title(self) -> None:
        """description + both titles → bold series + italic episode header prepended."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["desc-6"] = {
            "series": {"title": "My Show"},
            "title": "Episode One",
            "description": "About this episode.",
        }
        self.channel._add_metadata_item(item, "desc-6")
        self.assertEqual(
            item.description,
            "[B]My Show[/B]\n[I]Episode One[/I]\n\nAbout this episode."
        )


    def test_description_episode_title_none_treated_as_absent(self) -> None:
        """episode title is None → treated as absent, no italic header."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["desc-7"] = {
            "title": None,
            "description": "About this episode.",
        }
        self.channel._add_metadata_item(item, "desc-7")
        self.assertEqual(item.description, "About this episode.")


    def test_description_series_title_none_treated_as_absent(self) -> None:
        """series title is None → treated as absent, no bold header."""

        import chn_nlziet
        item = self._make_item()
        chn_nlziet.Channel._item_detail_cache["desc-8"] = {
            "series": {"title": None},
            "description": "About this episode.",
        }
        self.channel._add_metadata_item(item, "desc-8")
        self.assertEqual(item.description, "About this episode.")


class TestNlzietAppconfigLive(ChannelTest):
    """
    Live integration tests for appconfig — requires NLZIET_USERNAME in the environment.

    Calls the real endpoint without authentication (the endpoint is public).
    Guards on NLZIET_USERNAME presence as the 'run live tests' signal so
    these only execute in environments that have network access and credentials
    configured.
    """


    def __init__(self, methodName: str) -> None:
        super().__init__(methodName, "channel.nlziet.nlziet", None)


    @classmethod
    def setUpClass(cls) -> None:
        if (not os.getenv("NLZIET_USERNAME") or
                not os.getenv("NLZIET_PASSWORD")):
            raise unittest.SkipTest("NLZIET credentials not in environment.")
        super().setUpClass()


    def setUp(self) -> None:
        super().setUp()
        import chn_nlziet
        self._orig_service_interval = chn_nlziet.Channel.service_interval
        self._orig_is_blocked = chn_nlziet.Channel.is_blocked
        self._orig_blocked_reason = chn_nlziet.Channel.blocked_reason
        self._orig_is_update_required = chn_nlziet.Channel.is_update_required
        self._orig_update_reason = chn_nlziet.Channel.update_reason
        # Channel init makes real HTTP calls without credentials; clear stale status.
        UriHandler.instance().status = UriStatus(code=0, url=None, error=False, reason=None)


    def tearDown(self) -> None:
        import chn_nlziet
        chn_nlziet.Channel.service_interval = self._orig_service_interval
        chn_nlziet.Channel.is_blocked = self._orig_is_blocked
        chn_nlziet.Channel.blocked_reason = self._orig_blocked_reason
        chn_nlziet.Channel.is_update_required = self._orig_is_update_required
        chn_nlziet.Channel.update_reason = self._orig_update_reason
        super().tearDown()


    def test_appconfig_unauthenticated(self) -> None:
        """Real appconfig fetch (no auth) returns parseable JSON with expected keys.

        Acts as a CI canary: fails hard if isAppBlocked, emits DeprecationWarning
        if isUpdateRequired so the pipeline surfaces it without breaking the build.
        """

        import chn_nlziet
        from resources.lib.addonsettings import AddonSettings, LOCAL
        import warnings

        self.channel._sync_appconfig()

        raw = AddonSettings.get_setting(chn_nlziet.APPCONFIG_CACHE_KEY, store=LOCAL)
        self.assertIsNotNone(raw, "appconfig was not cached — fetch may have failed")
        data = json.loads(raw)
        self.assertIn("heartbeatInterval", data)

        if data.get("isAppBlocked"):
            reason = data.get("appBlockedReason") or "no reason provided"
            self.fail(f"NLZIET app is blocked — {reason}")

        if data.get("isUpdateRequired"):
            Logger.warning(
                "*** NLZIET API DEPRECATION: isUpdateRequired=True — "
                "the API client needs updating! updateText: %s",
                data.get("updateText", ""))
            warnings.warn(
                "NLZIET API deprecation: isUpdateRequired=True. "
                "Update the API client.",
                DeprecationWarning, stacklevel=2)


    def test_sync_appconfig_stores_expected_keys(self) -> None:
        """_sync_appconfig() parses and stores a response containing the expected keys."""

        import chn_nlziet

        stored = {}
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=json.dumps(MOCK_APPCONFIG_RESPONSE)), \
             patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
             patch("resources.lib.addonsettings.AddonSettings.set_setting",
                   side_effect=lambda k, v, **kw: stored.update({k: v})):
            self.channel._sync_appconfig()

        self.assertIn(chn_nlziet.APPCONFIG_CACHE_KEY, stored)
        data = json.loads(stored[chn_nlziet.APPCONFIG_CACHE_KEY])
        self.assertIn("heartbeatInterval", data)


# Mocked counterpart — always runs
# ============================================================================


class TestNlzietAppconfigMocked(TestNlzietAppconfigLive):
    """Mocked counterpart of TestNlzietAppconfigLive — always runs.

    Inherits all live tests and executes them against a patched UriHandler so
    no network is required.  Also adds behavioral tests for response shapes
    the live API cannot produce (blocked, update required, etc.).
    """

    _mock_response: str = ""


    @classmethod
    def setUpClass(cls) -> None:
        """Bypass credential guard — mock tests always run."""

        from tests.channel_tests.nlziet_mocks import MOCK_APPCONFIG_RESPONSE as _MOCK
        ChannelTest.setUpClass()
        cls._mock_response = json.dumps(_MOCK)


    def setUp(self) -> None:
        super().setUp()
        self._patcher = patch(
            "resources.lib.urihandler.UriHandler.open",
            return_value=self._mock_response)
        self._patcher.start()


    def tearDown(self) -> None:
        self._patcher.stop()
        super().tearDown()


    def _sync_with(self, overrides: dict) -> None:
        """Call _sync_appconfig with a response built from the base mock + overrides."""

        from tests.channel_tests.nlziet_mocks import MOCK_APPCONFIG_RESPONSE as _MOCK
        payload = json.dumps(dict(_MOCK, **overrides))
        with patch("resources.lib.urihandler.UriHandler.open", return_value=payload):
            self.channel._sync_appconfig()


    def test_appconfig_blocked_sets_channel_state(self) -> None:
        """Channel.is_blocked is True when isAppBlocked is set in the response."""

        import chn_nlziet
        self._sync_with({"isAppBlocked": True, "appBlockedReason": "Scheduled maintenance"})
        self.assertTrue(chn_nlziet.Channel.is_blocked)


    def test_appconfig_update_required_sets_channel_state(self) -> None:
        """Channel.is_update_required is True when isUpdateRequired is set."""

        import chn_nlziet
        self._sync_with({"isUpdateRequired": True, "updateText": "Please update the add-on"})
        self.assertTrue(chn_nlziet.Channel.is_update_required)


    def test_appconfig_custom_heartbeat_applied(self) -> None:
        """Non-default heartbeatInterval is reflected in Channel.service_interval."""

        import chn_nlziet
        self._sync_with({"heartbeatInterval": 300})
        self.assertEqual(chn_nlziet.Channel.service_interval, 300)


    def test_appconfig_network_failure_is_graceful(self) -> None:
        """Empty network response leaves channel state unchanged and does not raise."""

        import chn_nlziet
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""):
            self.channel._sync_appconfig()
        self.assertFalse(chn_nlziet.Channel.is_blocked)
        self.assertFalse(chn_nlziet.Channel.is_update_required)


class TestNlzietLoggedOnProperty(ChannelTest):
    """Tests for the Channel.loggedOn property."""

    def __init__(self, methodName: str) -> None:
        super().__init__(methodName, "channel.nlziet.nlziet", None)


    def setUp(self) -> None:
        super().setUp()


    def test_logged_on_false_when_no_token(self) -> None:
        """loggedOn is False when get_authentication_token() returns None."""

        with patch.object(self.channel._authenticator, "get_authentication_token", return_value=None):
            self.assertFalse(self.channel.loggedOn)


    def test_logged_on_true_when_token_present(self) -> None:
        """loggedOn is True when get_authentication_token() returns a token."""

        with patch.object(self.channel._authenticator, "get_authentication_token", return_value="tok"):
            self.assertTrue(self.channel.loggedOn)


    def test_setter_is_noop(self) -> None:
        """loggedOn setter is a no-op; get_authentication_token() drives the value."""

        with patch.object(self.channel._authenticator, "get_authentication_token", return_value=None):
            self.channel.loggedOn = True  # no-op
            self.assertFalse(self.channel.loggedOn)


class TestNlzietChannelLive(ChannelTest):
    """Live integration tests — skipped when NLZIET_USERNAME / NLZIET_PASSWORD are absent."""

    username: Optional[str] = None
    password: Optional[str] = None


    def __init__(self, methodName: str) -> None:
        super(TestNlzietChannelLive, self).__init__(methodName, "channel.nlziet.nlziet", None)


    @classmethod
    def setUpClass(cls) -> None:
        cls.username = os.getenv("NLZIET_USERNAME")
        cls.password = os.getenv("NLZIET_PASSWORD")
        if (not cls.username or
                not cls.password):
            raise unittest.SkipTest("NLZIET credentials not in environment.")
        super().setUpClass()

        from resources.lib.addonsettings import AddonSettings, LOCAL
        from resources.lib.authentication.nlziethandler import (
            NLZIETHandler, WEB_CLIENT_ID, DEVICE_CLIENT_ID, AUTH_CLIENT_ID_KEY)
        from resources.lib.authentication.authenticator import Authenticator
        for client_id in (WEB_CLIENT_ID, DEVICE_CLIENT_ID):
            prefix = "nlziet_oauth2_{}_".format(client_id)
            AddonSettings.set_setting("{}access_token".format(prefix), "", store=LOCAL)
            AddonSettings.set_setting("{}refresh_token".format(prefix), "", store=LOCAL)
            AddonSettings.set_setting("{}expires_at".format(prefix), "", store=LOCAL)
        AddonSettings.set_setting(AUTH_CLIENT_ID_KEY, WEB_CLIENT_ID, store=LOCAL)
        handler = NLZIETHandler()
        auth = Authenticator(handler)
        result = auth.log_on(username=cls.username, password=cls.password)
        if not result.logged_on:
            raise unittest.SkipTest("NLZIET live login failed in setUpClass.")


    def setUp(self) -> None:
        from resources.lib.addonsettings import AddonSettings, LOCAL
        from resources.lib.authentication.nlziethandler import WEB_CLIENT_ID, AUTH_CLIENT_ID_KEY

        # Restore the client ID *before* creating the channel so NLZIETHandler.__init__
        # picks up the web client used in setUpClass.  A previous test failure may have
        # cleared AUTH_CLIENT_ID_KEY via _credential_log_off (nlziethandler.py:902), which
        # would cause NLZIETHandler to fall back to DEVICE_CLIENT_ID.
        AddonSettings.set_setting(AUTH_CLIENT_ID_KEY, WEB_CLIENT_ID, store=LOCAL)

        super().setUp()

        # Store the real username so the channel's Authenticator finds a matching session
        # and can resume it without falling through to _get_password() / Vault.
        AddonSettings.set_channel_setting(
            self.channel.guid, "nlziet_username", self.username, store=LOCAL)

        with unittest.mock.patch("xbmcgui.Dialog.select", return_value=0):
            if not self.channel.log_on():
                self.skipTest("NLZIET login failed.")


    def test_login_succeeds(self) -> None:
        """Live: log_on() with real credentials succeeds."""

        self.assertTrue(self.channel.loggedOn)


    def test_profile_selected_after_login(self) -> None:
        """Live: a profile is active after login (profile_id is stored)."""

        self.assertTrue(self.channel.loggedOn)
        self.assertIsNotNone(self.channel._get_profile_id())


    def test_validate_token_returns_valid_after_login(self) -> None:
        """Live: verify_token() returns 'valid' when a real token is present."""

        self.assertEqual(self.channel._handler.verify_token(), "valid")


    def test_log_on_already_logged_on_returns_true(self) -> None:
        """Live: a second log_on() call hits the fast path and returns True immediately."""

        self.assertTrue(self.channel.log_on())


    def test_process_folder_list_returns_live_channels(self) -> None:
        """Live: process_folder_list returns at least one live channel item from the real EPG API."""

        items = self.channel.process_folder_list(None)
        self.assertGreater(len(items), 0)


    def test_update_live_item_returns_stream_url(self) -> None:
        """Live: update_live_item completes a live channel item with a valid stream URL."""

        main_items = self.channel.process_folder_list(None)
        self.assertGreater(len(main_items), 0, "No items in main channel list")
        live_channels = self.channel.process_folder_list(main_items[0])
        self.assertGreater(len(live_channels), 0, "No live channels returned to test stream update on")
        with patch("resources.lib.streams.mpd.Mpd.set_input_stream_addon_input"):
            updated = self.channel.update_live_item(live_channels[0])
        self.assertTrue(updated.complete)


    def test_update_live_item_invalid_channel_id_returns_incomplete(self) -> None:
        """Live: update_live_item with a bad channel ID returns an incomplete item."""

        from resources.lib.mediaitem import MediaItem
        item = MediaItem(
            "Invalid Channel",
            "https://api.nlziet.nl/v9/epg/programlocations/live?channel=invalid-xyz-9999",
        )
        result = self.channel.update_live_item(item)
        self.assertFalse(result.complete)


# =============================================================================
# Mocked channel tests (run without live credentials)
# =============================================================================

class TestNlzietChannelMocked(TestNlzietChannelLive):
    """Mocked counterpart to TestNlzietChannelLive — runs without credentials."""

    @classmethod
    def setUpClass(cls) -> None:
        # Skip the live-credential check; inject mock tokens directly.
        super(TestNlzietChannelLive, cls).setUpClass()

        from resources.lib.addonsettings import AddonSettings, LOCAL
        from resources.lib.authentication.nlziethandler import (
            WEB_CLIENT_ID, AUTH_CLIENT_ID_KEY)
        from tests.authentication.nlziethandler_mocks import (
            MOCK_ACCESS_TOKEN, MOCK_REFRESH_TOKEN, MOCK_ID_TOKEN, MOCK_PROFILE_ID)

        prefix = "nlziet_oauth2_{}_".format(WEB_CLIENT_ID)
        AddonSettings.set_setting("{}access_token".format(prefix), MOCK_ACCESS_TOKEN, store=LOCAL)
        AddonSettings.set_setting("{}refresh_token".format(prefix), MOCK_REFRESH_TOKEN, store=LOCAL)
        AddonSettings.set_setting("{}id_token".format(prefix), MOCK_ID_TOKEN, store=LOCAL)
        AddonSettings.set_setting("{}expires_at".format(prefix), str(9999999999), store=LOCAL)
        AddonSettings.set_setting(AUTH_CLIENT_ID_KEY, WEB_CLIENT_ID, store=LOCAL)
        AddonSettings.set_setting("nlziet_profile_id", MOCK_PROFILE_ID, store=LOCAL)


    def setUp(self) -> None:
        # Bypass the live-credentials gate in TestNlzietChannelLive.setUp.
        super(TestNlzietChannelLive, self).setUp()

        from resources.lib.addonsettings import AddonSettings, LOCAL
        from tests.authentication.nlziethandler_mocks import MOCK_ACCESS_TOKEN, MOCK_EMAIL

        AddonSettings.set_channel_setting(self.channel.guid, "nlziet_username", MOCK_EMAIL,
                                          store=LOCAL)

        select_profile_patcher = unittest.mock.patch(
            "chn_nlziet.Channel._select_profile",
            return_value=True)
        verify_token_patcher = unittest.mock.patch(
            "resources.lib.authentication.nlziethandler.NLZIETHandler.verify_token",
            return_value="valid")
        # Prevent any real network call: return the mock token directly so
        # refresh_access_token() never falls through to _refresh_token_grant().
        refresh_token_patcher = unittest.mock.patch(
            "resources.lib.authentication.nlziethandler.NLZIETHandler.refresh_access_token",
            return_value=MOCK_ACCESS_TOKEN)

        select_profile_patcher.start()
        verify_token_patcher.start()
        refresh_token_patcher.start()
        self.addCleanup(select_profile_patcher.stop)
        self.addCleanup(verify_token_patcher.stop)
        self.addCleanup(refresh_token_patcher.stop)

        with unittest.mock.patch("xbmcgui.Dialog.select", return_value=0):
            if not self.channel.log_on():
                self.skipTest("Mocked NLZIET login failed — check mock token setup.")

    def test_process_folder_list_returns_live_channels(self) -> None:
        """Mocked: process_folder_list returns channel items from a mock EPG response."""

        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=json.dumps(MOCK_EPG_LIVE_RESPONSE)), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            items = self.channel.process_folder_list(None)
        self.assertGreater(len(items), 0)

    def test_update_live_item_returns_stream_url(self) -> None:
        """Mocked: update_live_item completes with a mock handshake response."""

        from resources.lib.mediaitem import MediaItem
        item = MediaItem(
            "Test Channel",
            "https://api.nlziet.nl/v9/epg/programlocations/live?channel=test-live-1",
        )
        handshake_response = json.dumps({
            "manifestUrl": "https://example.com/stream.mpd",
            "drm": {"licenseUrl": "https://license.example.com/", "headers": {}},
        })
        with patch("resources.lib.urihandler.UriHandler.open", return_value=handshake_response), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.streams.mpd.Mpd.get_license_key", return_value="key"), \
                patch("resources.lib.streams.mpd.Mpd.set_input_stream_addon_input"):
            updated = self.channel.update_live_item(item)
        self.assertTrue(updated.complete)

    def test_update_live_item_invalid_channel_id_returns_incomplete(self) -> None:
        """Mocked: update_live_item with an API error response returns an incomplete item."""

        from resources.lib.mediaitem import MediaItem
        item = MediaItem(
            "Invalid Channel",
            "https://api.nlziet.nl/v9/epg/programlocations/live?channel=invalid-xyz-9999",
        )
        error_response = json.dumps({
            "errors": [{"type": "ChannelNotFound", "message": "Channel not found"}],
        })
        with patch("resources.lib.urihandler.UriHandler.open", return_value=error_response), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""):
            result = self.channel.update_live_item(item)
        self.assertFalse(result.complete)
