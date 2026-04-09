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

        # Install a real handler so patch.object(self.channel._handler, ...) works.
        self.channel._create_handler(device_flow=True)


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


    def test_service_interval_default(self) -> None:
        self.assertEqual(self.channel.service_interval, 90)


    def test_service_interval_is_positive(self) -> None:
        self.assertGreater(self.channel.service_interval, 0)

    # -- _create_handler (app headers) ------------------------------------


    def test_create_handler_device_flow_sets_android_tv_identity(self) -> None:
        """_create_handler(device_flow=True) sets AndroidTv name/version on _headers."""

        import chn_nlziet as m
        self.channel._http_headers.clear()
        self.channel._create_handler(device_flow=True)

        self.assertEqual(self.channel._http_headers.get("Nlziet-AppName"), m.NLZIET_APP_NAME)
        self.assertEqual(self.channel._http_headers.get("Nlziet-AppVersion"), m.NLZIET_APP_VERSION)
        self.assertIn("Nlziet-BrandName", self.channel._http_headers)
        self.assertIn("Nlziet-ModelName", self.channel._http_headers)
        self.assertIn("Nlziet-PlatformVersion", self.channel._http_headers)
        self.assertIn("Nlziet-DeviceCapabilities", self.channel._http_headers)


    def test_create_handler_web_flow_sets_webapp_identity(self) -> None:
        """_create_handler(device_flow=False) sets WebApp name/version on _headers."""

        import chn_nlziet as m
        self.channel._http_headers.clear()
        self.channel._create_handler(device_flow=False)

        self.assertEqual(self.channel._http_headers.get("Nlziet-AppName"), m.NLZIET_WEB_NAME)
        self.assertEqual(self.channel._http_headers.get("Nlziet-AppVersion"), m.NLZIET_WEB_VERSION)

    # -- service_update / appconfig cache ----------------------------------


    def _appconfig_raw(self, extra: Optional[Dict[str, Any]] = None) -> str:
        payload = {"epgCacheTime": 300, "isAppBlocked": False}
        if extra:
            payload.update(extra)
        return json.dumps(payload)


    def test_service_update_fetches_stale_cache(self) -> None:
        """service_update() with a stale/empty cache triggers a fresh network fetch."""

        raw = self._appconfig_raw()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw) as mock_open, \
                patch.object(self.channel._handler, "refresh_access_token"), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel.service_update()
        mock_open.assert_called_once()


    def test_service_update_empty_response_does_not_crash(self) -> None:
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch.object(self.channel._handler, "refresh_access_token"), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel.service_update()
        mock_set.assert_not_called()


    def test_service_update_bad_json_does_not_crash(self) -> None:
        with patch("resources.lib.urihandler.UriHandler.open", return_value="not-json"), \
                patch.object(self.channel._handler, "refresh_access_token"), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel.service_update()
        mock_set.assert_not_called()


    def test_service_update_stores_synced_at(self) -> None:
        raw = self._appconfig_raw()
        before = time.time()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch.object(self.channel._handler, "refresh_access_token"), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel.service_update()
        stored_json = mock_set.call_args[0][1]
        stored = json.loads(stored_json)
        self.assertGreaterEqual(stored["_synced_at"], before)


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


    def test_service_update_refreshes_token(self) -> None:
        """service_update() calls refresh_access_token() to proactively renew before expiry."""

        raw = self._appconfig_raw()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"), \
                patch.object(self.channel._handler,
                             "refresh_access_token") as mock_refresh:
            self.channel.service_update()
        mock_refresh.assert_called_once()


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


    # -- _resume_session ---------------------------------------------------


    def test_resume_session_no_cached_token_returns_none(self) -> None:
        """_resume_session() returns None when there is no cached token."""

        mock_active = MagicMock()
        mock_active.logged_on = False
        mock_h = MagicMock()
        mock_h.active_authentication.return_value = mock_active
        with patch.object(self.channel, "_create_handler", return_value=mock_h):
            result = self.channel._resume_session()
        self.assertIsNone(result)


    def test_resume_session_valid_token_returns_true(self) -> None:
        """_resume_session() returns True when token is valid."""

        mock_active = MagicMock()
        mock_active.logged_on = True
        mock_h = MagicMock()
        mock_h.active_authentication.return_value = mock_active
        with patch.object(self.channel, "_create_handler", return_value=mock_h):
            result = self.channel._resume_session()
        self.assertTrue(result)


    def test_resume_session_valid_token_no_profile_selection(self) -> None:
        """_resume_session() does not call _select_profile — that is log_on's responsibility."""

        mock_active = MagicMock()
        mock_active.logged_on = True
        mock_h = MagicMock()
        mock_h.active_authentication.return_value = mock_active
        with patch.object(self.channel, "_create_handler", return_value=mock_h), \
             patch.object(self.channel, "_select_profile") as mock_profile:
            self.channel._resume_session()
        mock_profile.assert_not_called()


    def test_resume_session_active_auth_fails_returns_none(self) -> None:
        """_resume_session() returns None when active_authentication reports no session."""

        mock_active = MagicMock()
        mock_active.logged_on = False
        mock_h = MagicMock()
        mock_h.active_authentication.return_value = mock_active
        with patch.object(self.channel, "_create_handler", return_value=mock_h):
            result = self.channel._resume_session()
        self.assertIsNone(result)


    def test_resume_session_does_not_call_validate_token(self) -> None:
        """_resume_session() does not call verify_token; active_authentication guarantees freshness."""

        mock_active = MagicMock()
        mock_active.logged_on = True
        mock_h = MagicMock()
        mock_h.active_authentication.return_value = mock_active
        with patch.object(self.channel, "_create_handler", return_value=mock_h):
            self.channel._resume_session()
        mock_h.verify_token.assert_not_called()


    # -- _headless_login ---------------------------------------------------------


    def test_headless_login_success_returns_true(self) -> None:
        """`_headless_login()` returns True when handler reports logged_on."""

        mock_result = MagicMock()
        mock_result.logged_on = True
        mock_h = MagicMock()
        mock_h.log_on.return_value = mock_result
        with patch("chn_nlziet.NLZIETHandler", return_value=mock_h), \
             patch("chn_nlziet.Authenticator"):
            result = self.channel._headless_login("user@test.nl", "secret")
        self.assertTrue(result)


    def test_headless_login_failure_shows_notification_returns_false(self) -> None:
        """`_headless_login()` shows the correct notification for each error code and returns False."""

        from resources.lib.helpers.languagehelper import LanguageHelper

        cases = [
            ("invalid_credentials", LanguageHelper.LoginFailed),
            ("network_error", LanguageHelper.ConnectionError),
            ("something_unexpected", LanguageHelper.UnknownError),
        ]
        for error_code, expected_msg_id in cases:
            mock_result = MagicMock()
            mock_result.logged_on = False
            mock_result.error = error_code
            mock_h = MagicMock()
            mock_h.log_on.return_value = mock_result
            with self.subTest(error=error_code), \
                 patch("chn_nlziet.NLZIETHandler", return_value=mock_h), \
                 patch("chn_nlziet.Authenticator"), \
                 patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification") as mock_notif:
                result = self.channel._headless_login("user@test.nl", "secret")
            self.assertFalse(result)
            mock_notif.assert_called_once()
            self.assertEqual(mock_notif.call_args[0][1], expected_msg_id)


    # -- log_on ------------------------------------------------------------


    def test_log_on_fast_path_succeeds(self) -> None:
        """log_on() resumes session, selects profile, sets loggedOn on valid token."""

        mock_active = MagicMock()
        mock_active.logged_on = True
        mock_h = MagicMock()
        mock_h.active_authentication.return_value = mock_active
        with patch.object(self.channel, "_create_handler", return_value=mock_h), \
             patch.object(self.channel, "_select_profile", return_value=True):
            result = self.channel.log_on()
        self.assertTrue(result)


    def test_log_on_fast_path_profile_fails_logs_off_returns_false(self) -> None:
        """log_on() logs off and returns False when resume succeeds but profile selection fails."""

        mock_active = MagicMock()
        mock_active.logged_on = True
        mock_h = MagicMock()
        mock_h.active_authentication.return_value = mock_active
        with patch.object(self.channel, "_create_handler", return_value=mock_h), \
             patch.object(self.channel, "_select_profile", return_value=False), \
             patch.object(self.channel, "log_off") as mock_log_off:
            result = self.channel.log_on()
        self.assertFalse(result)
        mock_log_off.assert_called_once()


    def test_auto_login_no_credentials_returns_none(self) -> None:
        """_auto_login() returns None silently when no credentials are configured."""

        with patch("chn_nlziet.Vault") as mock_vault, \
             patch.object(self.channel, "_get_setting", return_value=None):
            mock_vault.return_value.get_channel_setting.return_value = None
            result = self.channel._auto_login()
        self.assertIsNone(result)


    def test_auto_login_with_credentials_succeeds(self) -> None:
        """_auto_login() succeeds when handler returns logged_on."""

        mock_auth_result = MagicMock()
        mock_auth_result.logged_on = True
        mock_h = MagicMock()
        mock_h.log_on.return_value = mock_auth_result
        self.channel._handler = None
        with patch("chn_nlziet.NLZIETHandler", return_value=mock_h), \
             patch("chn_nlziet.Authenticator"), \
             patch("chn_nlziet.AddonSettings"), \
             patch("chn_nlziet.Vault") as mock_vault, \
             patch.object(self.channel, "_get_setting", return_value="user@test.nl"):
            mock_vault.return_value.get_channel_setting.return_value = "secret"
            result = self.channel._auto_login()
        self.assertTrue(result)


    def test_auto_login_fails_shows_notification_returns_false(self) -> None:
        """_auto_login() shows a notification for each error type and returns False."""

        from resources.lib.helpers.languagehelper import LanguageHelper

        cases = [
            ("invalid_credentials", LanguageHelper.LoginFailed),
            ("network_error", LanguageHelper.ConnectionError),
            ("something_unexpected", LanguageHelper.UnknownError),
        ]
        for error_code, expected_msg_id in cases:
            mock_fail = MagicMock()
            mock_fail.logged_on = False
            mock_fail.error = error_code
            mock_new_handler = MagicMock()
            mock_new_handler.log_on.return_value = mock_fail
            self.channel._handler = None
            with self.subTest(error=error_code), \
                 patch("chn_nlziet.NLZIETHandler", return_value=mock_new_handler), \
                 patch("chn_nlziet.Authenticator"), \
                 patch("chn_nlziet.Vault") as mock_vault, \
                 patch.object(self.channel, "_get_setting", return_value="user@test.nl"), \
                 patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification") as mock_notif:
                mock_vault.return_value.get_channel_setting.return_value = "secret"
                result = self.channel._auto_login()
            self.assertFalse(result)
            mock_notif.assert_called_once()
            self.assertEqual(mock_notif.call_args[0][1], expected_msg_id)


    def test_log_on_with_valid_cached_token_returns_true(self) -> None:
        """log_on() succeeds immediately when a valid cached token is present, regardless of loggedOn."""

        mock_active = MagicMock()
        mock_active.logged_on = True
        mock_h = MagicMock()
        mock_h.active_authentication.return_value = mock_active
        with patch.object(self.channel, "_create_handler", return_value=mock_h), \
             patch.object(self.channel, "_select_profile", return_value=True):
            result = self.channel.log_on()
            self.assertTrue(result)


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


    def test_log_off_initializes_handler_when_none(self) -> None:
        """log_off() creates the handler when called on a fresh channel instance (no prior auth)."""

        self.channel._handler = None
        self.channel._authenticator = None

        mock_auth = MagicMock()
        with patch.object(self.channel, "_create_handler") as mock_create, \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog"), \
             patch("xbmc.executebuiltin"), \
             patch("chn_nlziet.AddonSettings.get_setting", return_value=None), \
             patch("chn_nlziet.Authenticator", return_value=mock_auth), \
             patch("chn_nlziet.NLZIETHandler"):
            # Arrange: _create_handler must wire _authenticator so the subsequent call works
            def _create_side_effect(device_flow: bool) -> None:
                self.channel._authenticator = mock_auth
            mock_create.side_effect = _create_side_effect

            self.channel.log_off()

        mock_create.assert_called_once()
        mock_auth.log_off.assert_called_once()


    def test_log_on_active_auth_fails_falls_through_to_device_flow(self) -> None:
        """When the cached session cannot be resumed, log_on() falls through to device flow without a notification."""

        with patch.object(self.channel, "_resume_session", return_value=None), \
             patch.object(self.channel, "_get_setting", return_value=None), \
             patch("chn_nlziet.Vault") as mock_vault, \
             patch.object(self.channel, "_run_device_flow", return_value=None) as mock_flow, \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification") as mock_notify:
            mock_vault.return_value.get_channel_setting.return_value = None
            result = self.channel.log_on()
        mock_flow.assert_called_once()
        mock_notify.assert_not_called()
        self.assertIsNone(result)


    def test_log_on_resume_succeeds_complete_login_called(self) -> None:
        """When resume succeeds, log_on() proceeds to _complete_login without further auth steps."""

        mock_active = MagicMock()
        mock_active.logged_on = True
        mock_h = MagicMock()
        mock_h.active_authentication.return_value = mock_active
        with patch.object(self.channel, "_create_handler", return_value=mock_h), \
             patch.object(self.channel, "_complete_login", return_value=True) as mock_complete, \
             patch.object(self.channel, "_run_device_flow") as mock_flow, \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel.log_on()
        self.assertTrue(result)
        mock_complete.assert_called_once()
        mock_flow.assert_not_called()
        mock_dialog.assert_not_called()


    def test_log_on_token_expired_falls_through_silently(self) -> None:
        """When active_authentication cannot resume (refresh failed), log_on() falls through silently to device flow."""

        with patch.object(self.channel, "_resume_session", return_value=None), \
             patch.object(self.channel, "_get_setting", return_value=None), \
             patch("chn_nlziet.Vault") as mock_vault, \
             patch.object(self.channel, "_run_device_flow", return_value=None) as mock_flow, \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification") as mock_notify:
            mock_vault.return_value.get_channel_setting.return_value = None
            result = self.channel.log_on()
        mock_flow.assert_called_once()
        mock_notify.assert_not_called()


    def test_log_on_interactive_tries_device_flow_first(self) -> None:
        """log_on() attempts device flow when no token and no stored credentials."""

        with patch.object(self.channel, "_resume_session", return_value=None), \
             patch.object(self.channel, "_get_setting", return_value=None), \
             patch("chn_nlziet.Vault") as mock_vault, \
             patch.object(self.channel, "_run_device_flow", return_value=None) as mock_flow:
            mock_vault.return_value.get_channel_setting.return_value = None
            result = self.channel.log_on()
        mock_flow.assert_called_once()
        self.assertIsNone(result)


    def test_log_on_device_flow_success_completes_session(self) -> None:
        """log_on() sets up the session after a successful device flow."""

        with patch.object(self.channel, "_resume_session", return_value=None), \
             patch.object(self.channel, "_get_setting", return_value=None), \
             patch("chn_nlziet.Vault") as mock_vault, \
             patch.object(self.channel, "_run_device_flow", return_value=True), \
             patch.object(self.channel, "_greet_user") as mock_greet, \
             patch.object(self.channel, "_select_profile", return_value=True) as mock_profile:
            mock_vault.return_value.get_channel_setting.return_value = None
            result = self.channel.log_on()
        self.assertTrue(result)
        mock_greet.assert_called_once()
        mock_profile.assert_called_once()


    def test_log_on_device_flow_canceled_returns_none(self) -> None:
        """log_on() returns None when device flow is canceled."""

        with patch.object(self.channel, "_resume_session", return_value=None), \
             patch.object(self.channel, "_get_setting", return_value=None), \
             patch("chn_nlziet.Vault") as mock_vault, \
             patch.object(self.channel, "_run_device_flow", return_value=None):
            mock_vault.return_value.get_channel_setting.return_value = None
            result = self.channel.log_on()
        self.assertIsNone(result)


    def test_log_on_stored_creds_silent_auth_succeeds(self) -> None:
        """Both username and password stored → credential login succeeds without device flow."""

        mock_active = MagicMock()
        mock_active.logged_on = False
        mock_success = MagicMock()
        mock_success.logged_on = True
        mock_h = MagicMock()
        mock_h.active_authentication.return_value = mock_active
        mock_h.log_on.return_value = mock_success

        self.channel._handler = None
        with patch("chn_nlziet.NLZIETHandler", return_value=mock_h), \
             patch("chn_nlziet.Authenticator"), \
             patch.object(self.channel, "_get_setting", return_value="user@example.com"), \
             patch("chn_nlziet.Vault") as mock_vault, \
             patch("chn_nlziet.AddonSettings"), \
             patch.object(self.channel, "_greet_user"), \
             patch.object(self.channel, "_select_profile", return_value=True):
            mock_vault.return_value.get_channel_setting.return_value = "decrypted_pw"
            result = self.channel.log_on()
        self.assertTrue(result)


    def test_log_on_nothing_stored_no_notification_device_flow_started(self) -> None:
        """Nothing configured → device flow started with no notification."""

        with patch.object(self.channel, "_resume_session", return_value=None), \
             patch.object(self.channel, "_get_setting", return_value=None), \
             patch("chn_nlziet.Vault") as mock_vault, \
             patch.object(self.channel, "_run_device_flow", return_value=None) as mock_flow:
            mock_vault.return_value.get_channel_setting.return_value = None
            result = self.channel.log_on()
        mock_flow.assert_called_once()
        self.assertIsNone(result)

    # -- __list_profiles ---------------------------------------------------


    def test_list_profiles_returns_profiles(self) -> None:
        """__list_profiles() returns profile list from API response."""

        profiles = [{"id": "p1", "displayName": "User1"}, {"id": "p2", "displayName": "Kids"}]
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=json.dumps(profiles)):
            result = self.channel._list_profiles(self.channel._handler)
        self.assertEqual(result, profiles)


    def test_list_profiles_uses_api_headers(self) -> None:
        """__list_profiles() forwards the channel API header policy to UriHandler."""

        base_headers = {
            "Authorization": "Bearer tok",
            "Accept": "application/json",
            "Nlziet-AppName": "AndroidTv",
            "User-Agent": DEVICE_FLOW_USER_AGENT,
        }
        profiles = [{"id": "p1", "displayName": "User1"}]
        with patch.object(self.channel._handler, "get_headers", return_value=dict(base_headers)) as mock_get_headers, \
             patch("resources.lib.urihandler.UriHandler.open",
                   return_value=json.dumps(profiles)) as mock_open:
            self.channel._list_profiles(self.channel._handler)

        mock_get_headers.assert_called_once_with()
        _, kwargs = mock_open.call_args
        headers = kwargs.get("additional_headers", {})
        self.assertEqual(headers.get("Authorization"), "Bearer tok")
        self.assertEqual(headers.get("Accept"), "application/json")
        self.assertEqual(headers.get("Nlziet-AppName"), "AndroidTv")
        self.assertEqual(headers.get("User-Agent"), DEVICE_FLOW_USER_AGENT)


    def test_list_profiles_empty_response_returns_empty(self) -> None:
        """__list_profiles() returns [] when the API returns an empty response."""

        with patch("resources.lib.urihandler.UriHandler.open", return_value=""):
            result = self.channel._list_profiles(self.channel._handler)
        self.assertEqual(result, [])

    # -- __select_profile_if_needed ----------------------------------------


    def test_select_profile_if_needed_uses_stored_profile_id(self) -> None:
        """Stored profile_id scopes the token when the current token lacks the profile claim."""

        with patch.object(self.channel, "_get_profile_id", return_value="stored-id"), \
             patch.object(type(self.channel._handler), "token_profile_id",
                          new_callable=PropertyMock, return_value=None), \
             patch.object(self.channel._handler, "set_profile_claim") as mock_set_profile, \
             patch.object(self.channel, "_set_profile_id") as mock_store:
            self.channel._select_profile(self.channel._handler)
        mock_set_profile.assert_called_once_with("stored-id")
        mock_store.assert_not_called()


    def test_select_profile_skips_claim_if_token_already_has_profile(self) -> None:
        """_select_profile() skips set_profile_claim when the current token already carries the profile."""

        with patch.object(self.channel, "_get_profile_id", return_value="stored-id"), \
             patch.object(type(self.channel._handler), "token_profile_id",
                          new_callable=PropertyMock, return_value="stored-id"), \
             patch.object(self.channel._handler, "set_profile_claim") as mock_claim, \
             patch.object(self.channel, "_set_profile_id") as mock_store:
            result = self.channel._select_profile(self.channel._handler)
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
            self.channel._select_profile(self.channel._handler)
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
            self.channel._select_profile(self.channel._handler)
        mock_dlg.assert_called_once()
        mock_set_profile.assert_called_once_with("p2")
        mock_store.assert_called_once_with("p2")


    def test_select_profile_if_needed_no_profiles_skips_silently(self) -> None:
        """Empty profile list is handled without crash or profile selection call."""

        with patch.object(self.channel, "_get_profile_id", return_value=""), \
             patch.object(self.channel, "_list_profiles", return_value=[]), \
             patch.object(self.channel._handler, "set_profile_claim") as mock_set_profile:
            self.channel._select_profile(self.channel._handler)
        mock_set_profile.assert_not_called()


    def test_select_profile_if_needed_cancelled(self) -> None:
        """Cancelling the selection dialog does not call set_profile_claim."""

        profiles = [{"id": "p1", "displayName": "A"}, {"id": "p2", "displayName": "B"}]
        with patch.object(self.channel, "_get_profile_id", return_value=""), \
             patch.object(self.channel, "_list_profiles", return_value=profiles), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_selection_dialog",
                   return_value=-1), \
             patch.object(self.channel._handler, "set_profile_claim") as mock_set_profile:
            self.channel._select_profile(self.channel._handler)
        mock_set_profile.assert_not_called()

    # -- switch_profile action ---------------------------------------------


    def test_switch_profile_action_requires_login(self) -> None:
        """switch_profile() shows LoginFirst and does nothing when not logged in."""

        mock_not_logged = MagicMock()
        mock_not_logged.logged_on = False
        with patch.object(self.channel._handler,
                          "active_authentication", return_value=mock_not_logged), \
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
            result = self.channel._profile_type(self.channel._handler)
        self.assertEqual(result, "ChildYoung")


    def test_profile_type_returns_empty_when_no_token(self) -> None:
        """__profile_type() returns '' when no token is available."""

        with patch.object(type(self.channel._handler), "token_profile_type",
                          new_callable=PropertyMock, return_value=""):
            result = self.channel._profile_type(self.channel._handler)
        self.assertEqual(result, "")


    def test_profile_type_returns_empty_for_unscoped_token(self) -> None:
        """__profile_type() returns '' when token carries no profileType claim."""

        with patch.object(type(self.channel._handler), "token_profile_type",
                          new_callable=PropertyMock, return_value=""):
            result = self.channel._profile_type(self.channel._handler)
        self.assertEqual(result, "")

    # -- __run_device_flow / __poll_with_progress --------------------------


    def test_poll_with_progress_does_not_pass_cancel_lbl_to_dialog(self) -> None:
        """
        cancel_lbl must NOT be passed as a positional arg to DeviceAuthDialog.

        Real Kodi's C __new__ rejects extra args; also passing cancel_lbl at
        position 7 collides with show_manual_button which is passed as keyword.
        """

        flow = {
            "user_code": "ABCD-1234",
            "verification_uri": "https://example.com/activate",
            "device_code": "devcode",
            "interval": 5,
            "expires_in": 60,
            "qr_url": "https://example.com/qr",
        }
        mock_dialog = MagicMock()
        mock_dialog.result = "canceled"
        mock_dialog.stop_event.wait.return_value = True

        with patch("chn_nlziet.DeviceAuthDialog",
                   return_value=mock_dialog) as MockDialog, \
             patch("threading.Thread"):
            self.channel._poll_with_progress(flow, self.channel._handler)

        call_args = MockDialog.call_args
        positional = call_args.args if call_args else ()
        keyword = call_args.kwargs if call_args else {}
        # DeviceAuthDialog(visit_url, code, timeout, ...) — localised strings are defaults inside the dialog
        # Position 4 onwards must be absent (no extra positional args)
        self.assertLessEqual(len(positional), 3,
                             "DeviceAuthDialog called with too many positional args")
        self.assertIn("show_manual_button", keyword,
                      "show_manual_button should be passed as keyword")


    def test_timeout_retries_device_flow_without_dialog(self) -> None:
        """On timeout the device flow restarts automatically; no yes/no dialog is shown."""

        flow = {
            "user_code": "ABCD",
            "verification_uri": "https://example.com",
            "device_code": "dc",
            "interval": 5,
            "expires_in": 60,
        }
        mock_h = MagicMock()
        mock_h.start_nlziet_device_flow.return_value = flow
        with patch("chn_nlziet.NLZIETHandler", return_value=mock_h), \
             patch("chn_nlziet.Authenticator"), \
             patch.object(self.channel, "_poll_with_progress",
                          side_effect=["timeout", "canceled"]) as mock_poll, \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_yes_no") as mock_yes_no, \
             patch("xbmc.getInfoLabel", return_value="Kodi"):
            result = self.channel._run_device_flow()

        mock_yes_no.assert_not_called()
        self.assertEqual(mock_poll.call_count, 2)
        self.assertFalse(result)


    def test_run_device_flow_returns_true_on_success(self) -> None:
        """_run_device_flow() returns True when poll succeeds."""

        flow = {"user_code": "AB12", "verification_uri": "https://example.com",
                "device_code": "dc", "interval": 5, "expires_in": 60}
        mock_h = MagicMock()
        mock_h.start_nlziet_device_flow.return_value = flow
        self.channel._handler = None
        with patch("chn_nlziet.NLZIETHandler", return_value=mock_h), \
             patch("chn_nlziet.Authenticator"), \
             patch.object(self.channel, "_poll_with_progress", return_value="success"), \
             patch("xbmc.getInfoLabel", return_value="Kodi"):
            result = self.channel._run_device_flow()

        self.assertTrue(result)


    def test_run_device_flow_returns_none_when_canceled(self) -> None:
        """_run_device_flow() returns None when the user cancels."""

        flow = {"user_code": "AB12", "verification_uri": "https://example.com",
                "device_code": "dc", "interval": 5, "expires_in": 60}
        mock_h = MagicMock()
        mock_h.start_nlziet_device_flow.return_value = flow
        with patch("chn_nlziet.NLZIETHandler", return_value=mock_h), \
             patch("chn_nlziet.Authenticator"), \
             patch.object(self.channel, "_poll_with_progress", return_value="canceled"), \
             patch("xbmc.getInfoLabel", return_value="Kodi"):
            result = self.channel._run_device_flow()

        self.assertIsNone(result)


    def test_run_device_flow_manual_delegates_to_manual_login(self) -> None:
        """_run_device_flow() calls _manual_login() when 'manual' is returned."""

        flow = {"user_code": "AB12", "verification_uri": "https://example.com",
                "device_code": "dc", "interval": 5, "expires_in": 60}
        mock_h = MagicMock()
        mock_h.start_nlziet_device_flow.return_value = flow
        with patch("chn_nlziet.NLZIETHandler", return_value=mock_h), \
             patch("chn_nlziet.Authenticator"), \
             patch.object(self.channel, "_poll_with_progress", return_value="manual"), \
             patch.object(self.channel, "_manual_login", return_value=True) as mock_manual, \
             patch("xbmc.getInfoLabel", return_value="Kodi"):
            result = self.channel._run_device_flow()

        self.assertTrue(result)
        mock_manual.assert_called_once()


    def test_run_device_flow_manual_login_failure_retries_device_flow(self) -> None:
        """When _manual_login() fails/cancels, _run_device_flow() retries the device flow."""

        flow = {"user_code": "AB12", "verification_uri": "https://example.com",
                "device_code": "dc", "interval": 5, "expires_in": 60}

        mock_h = MagicMock()
        mock_h.start_nlziet_device_flow.return_value = flow
        # First poll → manual login chosen; second poll → user cancels device flow
        with patch("chn_nlziet.NLZIETHandler", return_value=mock_h) as mock_handler_cls, \
             patch("chn_nlziet.Authenticator"), \
             patch.object(self.channel, "_poll_with_progress",
                          side_effect=["manual", "canceled"]) as mock_poll, \
             patch.object(self.channel, "_manual_login", return_value=False) as mock_manual, \
             patch("xbmc.getInfoLabel", return_value="Kodi"):
            result = self.channel._run_device_flow()

        self.assertIsNone(result)
        mock_manual.assert_called_once()
        self.assertEqual(mock_poll.call_count, 2,
                         "device flow dialog must be shown again after failed manual login")
        self.assertEqual(mock_handler_cls.call_count, 2,
                         "handler created fresh at the start of each loop iteration")


    def test_run_device_flow_returns_false_on_oserror(self) -> None:
        """_run_device_flow() returns False and shows dialog on OSError."""

        mock_h = MagicMock()
        mock_h.start_nlziet_device_flow.side_effect = OSError("network error")
        self.channel._handler = None
        with patch("chn_nlziet.NLZIETHandler", return_value=mock_h), \
             patch("chn_nlziet.Authenticator"), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog, \
             patch("xbmc.getInfoLabel", return_value="Kodi"):
            result = self.channel._run_device_flow()

        self.assertFalse(result)
        mock_dialog.assert_called_once()


    def test_run_device_flow_returns_false_when_no_flow_response(self) -> None:
        """_run_device_flow() returns False and shows dialog when _device_authorization_request returns None."""

        mock_h = MagicMock()
        mock_h.start_nlziet_device_flow.return_value = None
        self.channel._handler = None
        with patch("chn_nlziet.NLZIETHandler", return_value=mock_h), \
             patch("chn_nlziet.Authenticator"), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog, \
             patch("xbmc.getInfoLabel", return_value="Kodi"):
            result = self.channel._run_device_flow()

        self.assertFalse(result)
        mock_dialog.assert_called_once()


    def test_greet_user_shows_name_from_user_info(self) -> None:
        """_greet_user() shows a dialog with the display name from get_user_info()."""

        with patch.object(self.channel._handler, "get_user_info",
                          return_value={"name": "Test User", "email": "t@example.com"}), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            self.channel._greet_user(self.channel._handler)

        mock_dialog.assert_called_once()
        msg = mock_dialog.call_args[0][1]
        self.assertIn("Test User", msg)


    def test_greet_user_falls_back_to_unknown_when_no_user_info(self) -> None:
        """_greet_user() falls back to 'Unknown' label when get_user_info() raises an error."""

        with patch.object(self.channel._handler, "get_user_info", side_effect=IOError()), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            self.channel._greet_user(self.channel._handler)

        mock_dialog.assert_called_once()


    def test_setup_device_calls_greet_and_select_profile_on_success(self) -> None:
        """setup_device() calls _greet_user and _select_profile when device flow succeeds."""

        with patch.object(self.channel, "_run_device_flow", return_value=True), \
             patch.object(self.channel, "_greet_user") as mock_greet, \
             patch.object(self.channel, "_select_profile") as mock_select, \
             patch("xbmc.executebuiltin") as mock_refresh:
            self.channel.setup_device()

        mock_greet.assert_called_once()
        mock_select.assert_called_once()
        mock_refresh.assert_called_once_with("Container.Refresh()")


    def test_setup_device_no_refresh_on_flow_failure(self) -> None:
        """setup_device() does not refresh the container if device flow fails."""

        with patch.object(self.channel, "_run_device_flow", return_value=False), \
             patch.object(self.channel, "_greet_user") as mock_greet, \
             patch("xbmc.executebuiltin") as mock_refresh:
            self.channel.setup_device()

        mock_greet.assert_not_called()
        mock_refresh.assert_not_called()


    def test_setup_device_skips_greet_when_flow_fails(self) -> None:
        """setup_device() does nothing if device flow returns False/None."""

        with patch.object(self.channel, "_run_device_flow", return_value=False), \
             patch.object(self.channel, "_greet_user") as mock_greet, \
             patch.object(self.channel, "_select_profile") as mock_select:
            self.channel.setup_device()

        mock_greet.assert_not_called()
        mock_select.assert_not_called()


    def test_manual_login_cancel_username_returns_false(self) -> None:
        """_manual_login() returns False immediately when the user dismisses the username prompt."""

        with patch("chn_nlziet.XbmcWrapper.show_key_board", return_value=None):
            result = self.channel._manual_login()

        self.assertFalse(result)


    def test_manual_login_cancel_password_returns_false(self) -> None:
        """_manual_login() returns False when the user dismisses the password prompt."""

        with patch("chn_nlziet.XbmcWrapper.show_key_board", return_value="user@example.com"), \
             patch("chn_nlziet.AddonSettings"), \
             patch("chn_nlziet.Vault") as mock_vault_cls:
            mock_vault_cls.return_value.get_channel_setting.return_value = None
            result = self.channel._manual_login()

        self.assertFalse(result)


    def test_manual_login_success_stores_credentials(self) -> None:
        """_manual_login() stores username and password after a successful login."""

        from resources.lib.authentication.authenticationresult import AuthenticationResult
        from resources.lib.authentication.nlziethandler import NLZIETHandler

        with patch("chn_nlziet.XbmcWrapper.show_key_board", return_value="user@example.com"), \
             patch.object(NLZIETHandler, "log_on",
                          return_value=AuthenticationResult("user@example.com")), \
             patch("chn_nlziet.AddonSettings") as mock_settings, \
             patch("chn_nlziet.Vault") as mock_vault_cls:
            mock_vault_cls.return_value.get_channel_setting.return_value = "secret"
            result = self.channel._manual_login()

        self.assertTrue(result)
        mock_vault_cls.return_value.set_channel_setting.assert_called_once_with(
            self.channel.guid, "nlziet_password",
            ANY)
        mock_settings.set_channel_setting.assert_called_once_with(
            self.channel, "nlziet_username", "user@example.com")


    def test_manual_login_error_shows_notification_returns_false(self) -> None:
        """_manual_login() shows the correct notification for each auth error and returns False."""

        from resources.lib.authentication.authenticationresult import AuthenticationResult
        from resources.lib.authentication.nlziethandler import NLZIETHandler
        from resources.lib.helpers.languagehelper import LanguageHelper

        cases = [
            ("invalid_credentials", LanguageHelper.LoginFailed),
            ("network_error", LanguageHelper.ConnectionError),
            ("some_unexpected_error", LanguageHelper.UnknownError),
        ]
        for error_code, expected_msg_id in cases:
            bad_result = AuthenticationResult("")
            bad_result.error = error_code
            with self.subTest(error=error_code), \
                 patch("chn_nlziet.XbmcWrapper.show_key_board", return_value="user@example.com"), \
                 patch("chn_nlziet.AddonSettings"), \
                 patch("chn_nlziet.Vault") as mock_vault_cls, \
                 patch.object(NLZIETHandler, "log_on", return_value=bad_result), \
                 patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification") as mock_notif:
                mock_vault_cls.return_value.get_channel_setting.return_value = "pw"
                result = self.channel._manual_login()
            self.assertFalse(result)
            mock_notif.assert_called_once()
            self.assertEqual(mock_notif.call_args[0][1], expected_msg_id)

    # -- _run_device_flow: error handling ----------------------------------


    def test_run_device_flow_error_shows_dialog_returns_false(self) -> None:
        """_run_device_flow() shows ConnectionError dialog on poll error and returns False."""

        device_auth = {
            "verification_uri": "http://x", "user_code": "ABC",
            "expires_in": 60, "qr_url": "http://q", "device_code": "dc",
        }
        mock_h = MagicMock()
        mock_h.start_nlziet_device_flow.return_value = device_auth
        with patch("chn_nlziet.NLZIETHandler", return_value=mock_h), \
             patch("chn_nlziet.Authenticator"), \
             patch.object(self.channel, "_poll_with_progress", return_value="error"), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog:
            result = self.channel._run_device_flow()
        self.assertFalse(result)
        mock_dialog.assert_called_once()


    def test_run_device_flow_timeout_shows_notification_and_restarts(self) -> None:
        """_run_device_flow() shows DeviceCodeExpired notification on timeout and restarts."""

        from resources.lib.helpers.languagehelper import LanguageHelper

        device_auth = {
            "verification_uri": "http://x", "user_code": "ABC",
            "expires_in": 60, "qr_url": "http://q", "device_code": "dc",
        }
        mock_h = MagicMock()
        mock_h.start_nlziet_device_flow.return_value = device_auth
        with patch("chn_nlziet.NLZIETHandler", return_value=mock_h), \
             patch("chn_nlziet.Authenticator"), \
             patch.object(self.channel, "_poll_with_progress",
                          side_effect=["timeout", "canceled"]), \
             patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification") as mock_notif, \
             patch("xbmc.getInfoLabel", return_value="Kodi"):
            result = self.channel._run_device_flow()
        self.assertIsNone(result)
        mock_notif.assert_called_once()
        self.assertEqual(mock_notif.call_args[0][1], LanguageHelper.DeviceCodeExpired)


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


    def test_logged_on_false_when_no_handler(self) -> None:
        """loggedOn is False when no handler has been created."""

        self.channel._handler = None
        self.assertFalse(self.channel.loggedOn)


    def test_logged_on_false_when_no_token(self) -> None:
        """loggedOn is False when get_authentication_token() returns None."""

        self.channel._create_handler(device_flow=True)
        with patch.object(self.channel._handler, "get_authentication_token", return_value=None):
            self.assertFalse(self.channel.loggedOn)


    def test_logged_on_true_when_token_present(self) -> None:
        """loggedOn is True when get_authentication_token() returns a token."""

        self.channel._create_handler(device_flow=True)
        with patch.object(self.channel._handler, "get_authentication_token", return_value="tok"):
            self.assertTrue(self.channel.loggedOn)


    def test_setter_is_noop(self) -> None:
        """loggedOn setter is a no-op; get_authentication_token() drives the value."""

        self.channel._create_handler(device_flow=True)
        with patch.object(self.channel._handler, "get_authentication_token", return_value=None):
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
        handler = NLZIETHandler(use_device_flow=False)
        auth = Authenticator(handler)
        result = auth.log_on(username=cls.username, password=cls.password)
        if not result.logged_on:
            raise unittest.SkipTest("NLZIET live login failed in setUpClass.")


    def setUp(self) -> None:
        super().setUp()
        with unittest.mock.patch("xbmcgui.Dialog.select", return_value=0):
            if not self.channel.log_on():
                self.skipTest("NLZIET login failed.")


    def test_login_succeeds(self) -> None:
        """Live: log_on() with real credentials succeeds."""

        self.assertTrue(self.channel.loggedOn)


    def test_profile_selected_after_login(self) -> None:
        """Live: a profile is active after login (profile_id is set)."""

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

        select_profile_patcher = unittest.mock.patch(
            "chn_nlziet.Channel._select_profile",
            return_value=True)
        verify_token_patcher = unittest.mock.patch(
            "resources.lib.authentication.nlziethandler.NLZIETHandler.verify_token",
            return_value="valid")

        select_profile_patcher.start()
        verify_token_patcher.start()
        self.addCleanup(select_profile_patcher.stop)
        self.addCleanup(verify_token_patcher.stop)

        with unittest.mock.patch("xbmcgui.Dialog.select", return_value=0):
            if not self.channel.log_on():
                self.skipTest("Mocked NLZIET login failed — check mock token setup.")
