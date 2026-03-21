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
from . channeltest import ChannelTest
from tests.channel_tests.nlziet_mocks import MOCK_APPCONFIG_RESPONSE


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


    def test_initial_folder_items_returns_empty_when_not_logged_on(self) -> None:
        """get_initial_folder_items() returns no items when the user is not logged in."""

        with patch.object(type(self.channel), "loggedOn",
                          new_callable=PropertyMock, return_value=False):
            _, items = self.channel.get_initial_folder_items("")
        self.assertEqual(items, [])


    def test_initial_folder_items_returns_empty_when_logged_on(self) -> None:
        """get_initial_folder_items() returns no items even when logged in (skeleton stub)."""

        with patch.object(type(self.channel), "loggedOn",
                          new_callable=PropertyMock, return_value=True):
            _, items = self.channel.get_initial_folder_items("")
        self.assertEqual(items, [])


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
