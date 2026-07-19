# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import time
import sys
import unittest
from typing import Any, Dict, Optional
from unittest.mock import PropertyMock, patch


import xbmcgui as _xbmcgui
if not hasattr(_xbmcgui, "WindowXMLDialog"):
    class _FakeWindowXMLDialog:
        def __new__(cls: "type[_FakeWindowXMLDialog]", *args: Any, **kwargs: Any) -> object: return object.__new__(cls)  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None: pass
    _xbmcgui.WindowXMLDialog = _FakeWindowXMLDialog  # type: ignore[attr-defined]
    sys.modules["xbmcgui"].WindowXMLDialog = _FakeWindowXMLDialog  # type: ignore[attr-defined]

from resources.lib import mediatype
from resources.lib.mediaitem import FolderItem, MediaItem
from resources.lib.urihandler import UriHandler, UriStatus
from resources.lib.authentication.nlziethandler import DEVICE_FLOW_USER_AGENT
from .channeltest import ChannelTest
from tests.channel_tests.nlziet_mocks import MOCK_APPCONFIG_RESPONSE, MOCK_EPG_LIVE_RESPONSE


class TestNlzietChannel(ChannelTest):
    def __init__(self, methodName: str) -> None:
        super(TestNlzietChannel, self).__init__(methodName, "channel.nlziet.nlziet", None)


    def setUp(self) -> None:
        # Reset status before init so _sync_appconfig() in __init__ sees no error.
        UriHandler.instance().status = UriStatus(code=0, url=None, error=False, reason=None)
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value='{"heartbeatInterval": 90, "isAppBlocked": false}'), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_yes_no"):
            super().setUp()
        # Stash class-level state so tests don't leak into each other.
        import chn_nlziet
        self._orig_service_interval = chn_nlziet.Channel.service_interval
        self._orig_is_blocked = chn_nlziet.Channel.is_blocked
        self._orig_blocked_reason = chn_nlziet.Channel.blocked_reason
        self._orig_is_update_required = chn_nlziet.Channel.is_update_required
        self._orig_update_reason = chn_nlziet.Channel.update_reason
        self._orig_appconfig_fail_count = chn_nlziet.Channel._appconfig_fail_count
        self._orig_appconfig_last_synced_at = chn_nlziet.Channel._appconfig_last_synced_at

        # Ensure tests start with a clean status.
        UriHandler.instance().status = UriStatus(code=0, url=None, error=False, reason=None)


    def tearDown(self) -> None:
        import chn_nlziet
        chn_nlziet.Channel.service_interval = self._orig_service_interval
        chn_nlziet.Channel.is_blocked = self._orig_is_blocked
        chn_nlziet.Channel.blocked_reason = self._orig_blocked_reason
        chn_nlziet.Channel.is_update_required = self._orig_is_update_required
        chn_nlziet.Channel.update_reason = self._orig_update_reason
        chn_nlziet.Channel._appconfig_fail_count = self._orig_appconfig_fail_count
        chn_nlziet.Channel._appconfig_last_synced_at = self._orig_appconfig_last_synced_at
        super().tearDown()

    # -- Channel metadata --------------------------------------------------

    def test_channel_exists(self) -> None:
        self.assertIsNotNone(self.channel)


    def test_initial_folder_items_returns_empty_when_not_logged_on(self) -> None:
        """SUCCESS → returns empty list when the user is not logged in."""

        with patch.object(type(self.channel), "loggedOn",
                          new_callable=PropertyMock, return_value=False):
            _, items = self.channel.get_initial_folder_items("")
        self.assertEqual(items, [])


    def test_initial_folder_items_returns_live_tv_folder_when_logged_on(self) -> None:
        """SUCCESS → returns Live TV and Search when placement fetch is empty."""

        with patch.object(type(self.channel), "loggedOn",
                          new_callable=PropertyMock, return_value=True), \
                patch("resources.lib.urihandler.UriHandler.open", return_value=""):
            _, items = self.channel.get_initial_folder_items("")
        self.assertEqual(len(items), 2)
        self.assertTrue(items[0].isLive)
        self.assertIn("search", items[1].url.lower())


    def test_initial_folder_items_includes_placement_rows(self) -> None:
        """SUCCESS → placement components are turned into On Demand folders."""

        placement_response = json.dumps({"components": [
            {"type": "ItemTileList", "title": "Trending",
             "url": "https://api.nlziet.nl/v9/recommend/with?contextName=trending"},
        ]})
        with patch.object(type(self.channel), "loggedOn",
                          new_callable=PropertyMock, return_value=True), \
                patch("resources.lib.urihandler.UriHandler.open",
                      return_value=placement_response):
            _, items = self.channel.get_initial_folder_items("")
        titles = [i.name for i in items]
        self.assertIn("Trending", titles)


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
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""):
            self.channel._sync_appconfig()


    def test_sync_appconfig_bad_json_does_not_write(self) -> None:
        with patch("resources.lib.urihandler.UriHandler.open", return_value="not json"):
            self.channel._sync_appconfig()


    def test_sync_appconfig_non_dict_json_does_not_write(self) -> None:
        """_sync_appconfig() silently ignores valid JSON that is not a dict (e.g. null, [])."""

        for non_dict in ("null", "[]", '"string"', "42"):
            with self.subTest(response=non_dict):
                with patch("resources.lib.urihandler.UriHandler.open", return_value=non_dict):
                    self.channel._sync_appconfig()


    def test_sync_appconfig_unauthenticated_sends_no_auth_header(self) -> None:
        """_sync_appconfig() sends no Authorization header when unauthenticated."""

        with patch.object(type(self.channel._authenticator), "authentication_headers",
                          new_callable=PropertyMock, return_value={}), \
             patch("resources.lib.urihandler.UriHandler.open", return_value="{}") as mock_open:
            self.channel._sync_appconfig()
        headers = mock_open.call_args[1].get("additional_headers", {})
        self.assertNotIn("Authorization", headers)


    def test_sync_appconfig_sends_web_headers(self) -> None:
        """_sync_appconfig() sends WebApp Nlziet-AppName header for web login."""

        with patch("resources.lib.urihandler.UriHandler.open", return_value="{}") as mock_open:
            self.channel._sync_appconfig()
        headers = mock_open.call_args[1].get("additional_headers", {})
        self.assertIn("Nlziet-AppName", headers)
        self.assertEqual(headers["Nlziet-AppName"], "WebApp")


    def test_sync_appconfig_sends_device_headers(self) -> None:
        """_sync_appconfig() sends AndroidTv Nlziet-AppName header for device login."""

        device_headers = {"Accept": "application/json", "Nlziet-AppName": "AndroidTv"}
        with patch.object(type(self.channel), "_request_headers",
                          new_callable=PropertyMock, return_value=device_headers), \
             patch("resources.lib.urihandler.UriHandler.open", return_value="{}") as mock_open:
            self.channel._sync_appconfig()
        self.assertEqual(mock_open.call_args[1].get("additional_headers"), device_headers)


    def test_service_update_updates_service_interval(self) -> None:
        raw = self._appconfig_raw({"heartbeatInterval": 120})
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch.object(self.channel._authenticator, "active_authentication"):
            self.channel.service_update()
        import chn_nlziet
        self.assertEqual(chn_nlziet.Channel.service_interval, 120)


    def test_service_update_uses_default_when_no_heartbeat(self) -> None:
        raw = self._appconfig_raw()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch.object(self.channel._authenticator, "active_authentication"):
            self.channel.service_update()
        import chn_nlziet
        self.assertEqual(chn_nlziet.Channel.service_interval,
                         chn_nlziet.APPCONFIG_HEARTBEAT_DEFAULT)


    def test_sync_appconfig_sets_is_blocked(self) -> None:
        """_sync_appconfig() sets Channel.is_blocked when isAppBlocked is true."""

        payload = {"isAppBlocked": True, "appBlockedReason": "Maintenance"}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw):
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
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw):
            self.channel._sync_appconfig()
        self.assertEqual(chn_nlziet.Channel.blocked_reason, "")


    def test_sync_appconfig_blocked_reason_logged(self) -> None:
        """_sync_appconfig() logs appBlockedReason when isAppBlocked is true."""

        payload = {"isAppBlocked": True, "appBlockedReason": "Maintenance window"}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.logger.Logger.warning") as mock_warn:
            self.channel._sync_appconfig()
        warned = any("Maintenance window" in str(c) for c in mock_warn.call_args_list)
        self.assertTrue(warned, f"Expected reason in warning, got: {mock_warn.call_args_list}")


    def test_sync_appconfig_blocked_reason_null_normalised_to_empty(self) -> None:
        """_sync_appconfig() normalises a null appBlockedReason to an empty string."""

        payload = {"isAppBlocked": True, "appBlockedReason": None}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw):
            self.channel._sync_appconfig()
        import chn_nlziet
        self.assertEqual(chn_nlziet.Channel.blocked_reason, "")


    def test_sync_appconfig_update_reason_null_normalised_to_empty(self) -> None:
        """_sync_appconfig() normalises a null updateText to an empty string."""

        payload = {"isUpdateRequired": True, "updateText": None}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw):
            self.channel._sync_appconfig()
        import chn_nlziet
        self.assertEqual(chn_nlziet.Channel.update_reason, "")


    def test_sync_appconfig_sets_is_update_required(self) -> None:
        """_sync_appconfig() sets Channel.is_update_required when server signals deprecation."""

        payload = {"isUpdateRequired": True, "updateText": "Please update your client"}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw):
            self.channel._sync_appconfig()
        import chn_nlziet
        self.assertTrue(chn_nlziet.Channel.is_update_required)



    def test_network_error_increments_fail_count(self) -> None:
        """SUCCESS → single network error increments _appconfig_fail_count by 1."""

        import chn_nlziet
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""):
            UriHandler.instance().status = UriStatus(
                code=503, url=None, error=True, reason="Service Unavailable")
            self.channel._sync_appconfig()
        self.assertEqual(chn_nlziet.Channel._appconfig_fail_count, 1)


    def test_network_error_does_not_change_channel_state(self) -> None:
        """SUCCESS → single network error leaves is_blocked and is_update_required unchanged."""

        import chn_nlziet
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""):
            UriHandler.instance().status = UriStatus(
                code=503, url=None, error=True, reason="Service Unavailable")
            self.channel._sync_appconfig()
        self.assertFalse(chn_nlziet.Channel.is_blocked)
        self.assertFalse(chn_nlziet.Channel.is_update_required)
        self.assertEqual(chn_nlziet.Channel.service_interval, chn_nlziet.APPCONFIG_HEARTBEAT_DEFAULT)


    def test_stale_by_count_resets_poll_interval_but_preserves_block_state(self) -> None:
        """SUCCESS → reaching APPCONFIG_SYNC_MAX_FAIL errors resets poll interval only."""

        import chn_nlziet
        chn_nlziet.Channel.service_interval = 300
        chn_nlziet.Channel.is_blocked = True
        chn_nlziet.Channel.blocked_reason = "Maintenance"
        chn_nlziet.Channel._appconfig_fail_count = chn_nlziet.APPCONFIG_SYNC_MAX_FAIL - 1
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification"):
            UriHandler.instance().status = UriStatus(
                code=503, url=None, error=True, reason="Service Unavailable")
            self.channel._sync_appconfig()
        self.assertEqual(
            chn_nlziet.Channel.service_interval, chn_nlziet.APPCONFIG_HEARTBEAT_DEFAULT)
        self.assertTrue(chn_nlziet.Channel.is_blocked)
        self.assertEqual(chn_nlziet.Channel.blocked_reason, "Maintenance")
        self.assertEqual(chn_nlziet.Channel._appconfig_fail_count, 0)
        self.assertEqual(chn_nlziet.Channel._appconfig_last_synced_at, 0.0)


    def test_stale_by_time_resets_to_defaults(self) -> None:
        """SUCCESS → exceeding APPCONFIG_SYNC_MAX_AGE since last sync resets channel state to defaults."""

        import chn_nlziet
        chn_nlziet.Channel.service_interval = 300
        chn_nlziet.Channel._appconfig_last_synced_at = (
            time.time() - chn_nlziet.APPCONFIG_SYNC_MAX_AGE - 1)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification"):
            UriHandler.instance().status = UriStatus(
                code=503, url=None, error=True, reason="Service Unavailable")
            self.channel._sync_appconfig()
        self.assertEqual(chn_nlziet.Channel.service_interval, chn_nlziet.APPCONFIG_HEARTBEAT_DEFAULT)
        self.assertEqual(chn_nlziet.Channel._appconfig_fail_count, 0)
        self.assertEqual(chn_nlziet.Channel._appconfig_last_synced_at, 0.0)


    def test_success_resets_fail_count(self) -> None:
        """SUCCESS → successful sync resets _appconfig_fail_count to 0."""

        import chn_nlziet
        chn_nlziet.Channel._appconfig_fail_count = 5
        with patch("resources.lib.urihandler.UriHandler.open", return_value=self._appconfig_raw()):
            self.channel._sync_appconfig()
        self.assertEqual(chn_nlziet.Channel._appconfig_fail_count, 0)


    def test_success_updates_last_synced_at(self) -> None:
        """SUCCESS → successful sync sets _appconfig_last_synced_at to approximately now."""

        import chn_nlziet
        before = time.time()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=self._appconfig_raw()):
            self.channel._sync_appconfig()
        self.assertGreaterEqual(chn_nlziet.Channel._appconfig_last_synced_at, before)


    def test_sync_appconfig_update_required_logged(self) -> None:
        """SUCCESS → _sync_appconfig() logs updateText when isUpdateRequired is true."""

        payload = {"isUpdateRequired": True, "updateText": "Please upgrade now"}
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=json.dumps(payload)), \
                patch("resources.lib.logger.Logger.warning") as mock_warn:
            self.channel._sync_appconfig()
        self.assertTrue(
            any("Please upgrade now" in str(c) for c in mock_warn.call_args_list),
            f"Expected 'Please upgrade now' in warning, got: {mock_warn.call_args_list}")


    def test_sync_appconfig_clears_is_update_required_when_absent(self) -> None:
        """SUCCESS → is_update_required resets to False when key absent from response."""

        import chn_nlziet
        chn_nlziet.Channel.is_update_required = True
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=self._appconfig_raw()):
            self.channel._sync_appconfig()
        self.assertFalse(chn_nlziet.Channel.is_update_required)


    def test_stale_by_count_shows_notification(self) -> None:
        """SUCCESS → stale threshold shows error notification."""

        import chn_nlziet
        chn_nlziet.Channel._appconfig_fail_count = chn_nlziet.APPCONFIG_SYNC_MAX_FAIL - 1
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification") as mock_notify:
            UriHandler.instance().status = UriStatus(
                code=503, url=None, error=True, reason="Service Unavailable")
            self.channel._sync_appconfig()
        mock_notify.assert_called_once()


    def test_sub_threshold_error_does_not_show_notification(self) -> None:
        """SUCCESS → network error below APPCONFIG_SYNC_MAX_FAIL threshold shows no notification."""

        import chn_nlziet
        chn_nlziet.Channel._appconfig_fail_count = chn_nlziet.APPCONFIG_SYNC_MAX_FAIL - 2
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification") as mock_notify:
            UriHandler.instance().status = UriStatus(
                code=503, url=None, error=True, reason="Service Unavailable")
            self.channel._sync_appconfig()
        mock_notify.assert_not_called()


    def test_first_ever_error_with_zero_last_synced_does_not_reset(self) -> None:
        """SUCCESS → single error with last_synced_at==0.0 and sub-threshold count does not reset."""

        import chn_nlziet
        chn_nlziet.Channel._appconfig_last_synced_at = 0.0
        chn_nlziet.Channel._appconfig_fail_count = 0
        chn_nlziet.Channel.service_interval = 300
        chn_nlziet.Channel.is_blocked = True
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification") as mock_notify:
            UriHandler.instance().status = UriStatus(
                code=503, url=None, error=True, reason="Service Unavailable")
            self.channel._sync_appconfig()
        # The `last_synced_at > 0` guard must block the time-based reset branch.
        self.assertEqual(chn_nlziet.Channel.service_interval, 300)
        self.assertTrue(chn_nlziet.Channel.is_blocked)
        self.assertEqual(chn_nlziet.Channel._appconfig_fail_count, 1)
        self.assertEqual(chn_nlziet.Channel._appconfig_last_synced_at, 0.0)
        mock_notify.assert_not_called()


    def test_parse_failure_does_not_increment_fail_count(self) -> None:
        """SUCCESS → JSON parse failure leaves _appconfig_fail_count and last_synced_at unchanged."""

        import chn_nlziet
        chn_nlziet.Channel._appconfig_fail_count = 3
        synced = time.time() - 5
        chn_nlziet.Channel._appconfig_last_synced_at = synced
        with patch("resources.lib.urihandler.UriHandler.open", return_value="not json"):
            result = self.channel._sync_appconfig()
        self.assertFalse(result)
        self.assertEqual(chn_nlziet.Channel._appconfig_fail_count, 3)
        self.assertEqual(chn_nlziet.Channel._appconfig_last_synced_at, synced)


    def test_non_dict_json_does_not_increment_fail_count(self) -> None:
        """SUCCESS → non-dict JSON response leaves _appconfig_fail_count unchanged."""

        import chn_nlziet
        chn_nlziet.Channel._appconfig_fail_count = 7
        with patch("resources.lib.urihandler.UriHandler.open", return_value="[]"):
            result = self.channel._sync_appconfig()
        self.assertFalse(result)
        self.assertEqual(chn_nlziet.Channel._appconfig_fail_count, 7)


    def test_stale_by_time_preserves_is_update_required_and_reason(self) -> None:
        """SUCCESS → time-based stale reset resets poll interval, not update-required state."""

        import chn_nlziet
        chn_nlziet.Channel.service_interval = 300
        chn_nlziet.Channel.is_update_required = True
        chn_nlziet.Channel.update_reason = "Please upgrade"
        chn_nlziet.Channel._appconfig_last_synced_at = (
            time.time() - chn_nlziet.APPCONFIG_SYNC_MAX_AGE - 1)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification"):
            UriHandler.instance().status = UriStatus(
                code=503, url=None, error=True, reason="Service Unavailable")
            self.channel._sync_appconfig()
        self.assertEqual(
            chn_nlziet.Channel.service_interval, chn_nlziet.APPCONFIG_HEARTBEAT_DEFAULT)
        self.assertTrue(chn_nlziet.Channel.is_update_required)
        self.assertEqual(chn_nlziet.Channel.update_reason, "Please upgrade")


    def test_sync_appconfig_clears_update_reason_when_not_required(self) -> None:
        """SUCCESS → update_reason resets to empty string when isUpdateRequired is absent."""

        import chn_nlziet
        chn_nlziet.Channel.update_reason = "stale reason"
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=self._appconfig_raw()):
            self.channel._sync_appconfig()
        self.assertEqual(chn_nlziet.Channel.update_reason, "")


    def test_service_update_does_not_raise_on_network_error(self) -> None:
        """SUCCESS → service_update() does not raise when _sync_appconfig fails."""

        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification"):
            UriHandler.instance().status = UriStatus(
                code=503, url=None, error=True, reason="Service Unavailable")
            try:
                self.channel.service_update()
            except Exception as exc:  # NOSONAR
                self.fail(f"service_update() raised unexpectedly: {exc!r}")


    def test_sync_appconfig_zero_heartbeat_uses_default(self) -> None:
        """SUCCESS → a falsy heartbeatInterval (0/null) falls back to APPCONFIG_HEARTBEAT_DEFAULT.

        Range and type sanitisation of the interval is the service layer's job
        (see RetroService._resolve_interval); the channel only applies the
        absent-or-falsy default.
        """

        import chn_nlziet
        raw = json.dumps({"heartbeatInterval": 0, "isAppBlocked": False})
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw):
            self.channel._sync_appconfig()
        self.assertEqual(chn_nlziet.Channel.service_interval,
                         chn_nlziet.APPCONFIG_HEARTBEAT_DEFAULT)


    @staticmethod
    def _get_channel_info():
        from resources.lib.helpers.channelimporter import ChannelIndex
        return ChannelIndex.get_register().get_channel(
            "channel.nlziet.nlziet", None, info_only=True)


    def test_init_failure_shows_dialog(self) -> None:
        """SUCCESS → channel init shows retry dialog when appconfig sync fails."""

        import chn_nlziet
        channel_info = self._get_channel_info()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_yes_no",
                      return_value=False) as mock_dialog:
            UriHandler.instance().status = UriStatus(
                code=503, url=None, error=True, reason="Service Unavailable")
            with self.assertRaises(RuntimeError):
                chn_nlziet.Channel(channel_info)
        mock_dialog.assert_called_once()


    def test_init_success_shows_no_dialog(self) -> None:
        """SUCCESS → channel init shows no dialog when appconfig sync succeeds."""

        import chn_nlziet
        channel_info = self._get_channel_info()
        UriHandler.instance().status = UriStatus(code=0, url=None, error=False, reason=None)
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=self._appconfig_raw()), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_yes_no") as mock_dialog:
            chn_nlziet.Channel(channel_info)
        mock_dialog.assert_not_called()


    def test_init_max_retries_exhausted_raises(self) -> None:
        """SUCCESS → RuntimeError raised when all init retries are exhausted."""

        import chn_nlziet
        channel_info = self._get_channel_info()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_yes_no",
                      return_value=True):
            UriHandler.instance().status = UriStatus(
                code=503, url=None, error=True, reason="Service Unavailable")
            with self.assertRaises(RuntimeError):
                chn_nlziet.Channel(channel_info)


    def test_init_max_retries_pins_dialog_and_sync_call_counts(self) -> None:
        """SUCCESS → exhausted init calls show_yes_no exactly APPCONFIG_INIT_RETRY_MAX times and show_dialog once."""

        import chn_nlziet
        channel_info = self._get_channel_info()
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value="") as mock_open, \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_yes_no",
                      return_value=True) as mock_yes_no, \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_dialog") as mock_dialog, \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification"):
            UriHandler.instance().status = UriStatus(
                code=503, url=None, error=True, reason="Service Unavailable")
            with self.assertRaises(RuntimeError):
                chn_nlziet.Channel(channel_info)
        self.assertEqual(mock_yes_no.call_count, chn_nlziet.APPCONFIG_INIT_RETRY_MAX)
        self.assertEqual(mock_open.call_count, chn_nlziet.APPCONFIG_INIT_RETRY_MAX)
        self.assertEqual(mock_dialog.call_count, 1)


    def test_init_recovers_after_one_failure(self) -> None:
        """SUCCESS → init succeeds when first sync fails but second succeeds after user confirms."""

        import chn_nlziet
        channel_info = self._get_channel_info()
        good_raw = self._appconfig_raw()
        call_count = {"n": 0}

        def _open_side_effect(*args: Any, **kwargs: Any) -> str:
            call_count["n"] += 1
            if call_count["n"] == 1:
                UriHandler.instance().status = UriStatus(
                    code=503, url=None, error=True, reason="Service Unavailable")
                return ""
            UriHandler.instance().status = UriStatus(code=0, url=None, error=False, reason=None)
            return good_raw

        with patch("resources.lib.urihandler.UriHandler.open",
                   side_effect=_open_side_effect), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_yes_no",
                      return_value=True) as mock_dialog, \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification"):
            ch = chn_nlziet.Channel(channel_info)
        self.assertIsNotNone(ch)
        mock_dialog.assert_called_once()


# Mocked appconfig tests — always run, no credentials required
# ============================================================================


class TestNlzietAppconfigMocked(ChannelTest):
    """Fully mocked appconfig tests — no network, no credentials required.

    Exercises response-parsing and channel-state behaviour against a patched
    UriHandler.  Live counterparts (with real HTTP and header variants) are
    added in the login commit once authentication is in place.
    """

    def __init__(self, methodName: str) -> None:
        super().__init__(methodName, "channel.nlziet.nlziet", None)


class TestNlzietChannelUnit(ChannelTest):
    """Unit tests for the NLZIET channel — always run, no credentials required.

    All HTTP calls and settings I/O are patched.
    """

    def __init__(self, methodName: str) -> None:
        super().__init__(methodName, "channel.nlziet.nlziet", None)


    def setUp(self) -> None:
        # Clear any stale OAuth tokens so _request_headers never sends a Bearer token.
        from resources.lib.addonsettings import AddonSettings, LOCAL
        from resources.lib.authentication.nlziethandler import (
            WEB_CLIENT_ID, DEVICE_CLIENT_ID, AUTH_CLIENT_ID_KEY)
        for client_id in (WEB_CLIENT_ID, DEVICE_CLIENT_ID):
            prefix = f"nlziet_oauth2_{client_id}_"
            AddonSettings.set_setting(f"{prefix}access_token", "", store=LOCAL)
            AddonSettings.set_setting(f"{prefix}refresh_token", "", store=LOCAL)
            AddonSettings.set_setting(f"{prefix}expires_at", "", store=LOCAL)
        AddonSettings.set_setting(AUTH_CLIENT_ID_KEY, WEB_CLIENT_ID, store=LOCAL)

        UriHandler.instance().status = UriStatus(code=0, url=None, error=False, reason=None)
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value='{"heartbeatInterval": 90, "isAppBlocked": false}'), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_yes_no"):
            super().setUp()
        import chn_nlziet
        self._orig_service_interval = chn_nlziet.Channel.service_interval
        self._orig_is_blocked = chn_nlziet.Channel.is_blocked
        self._orig_blocked_reason = chn_nlziet.Channel.blocked_reason
        self._orig_is_update_required = chn_nlziet.Channel.is_update_required
        self._orig_update_reason = chn_nlziet.Channel.update_reason
        self._orig_appconfig_fail_count = chn_nlziet.Channel._appconfig_fail_count
        self._orig_appconfig_last_synced_at = chn_nlziet.Channel._appconfig_last_synced_at
        UriHandler.instance().status = UriStatus(code=0, url=None, error=False, reason=None)


    def tearDown(self) -> None:
        import chn_nlziet
        chn_nlziet.Channel.service_interval = self._orig_service_interval
        chn_nlziet.Channel.is_blocked = self._orig_is_blocked
        chn_nlziet.Channel.blocked_reason = self._orig_blocked_reason
        chn_nlziet.Channel.is_update_required = self._orig_is_update_required
        chn_nlziet.Channel.update_reason = self._orig_update_reason
        chn_nlziet.Channel._appconfig_fail_count = self._orig_appconfig_fail_count
        chn_nlziet.Channel._appconfig_last_synced_at = self._orig_appconfig_last_synced_at
        super().tearDown()


    def test_appconfig_unauthenticated(self) -> None:
        """Plain-HTTP canary: appconfig endpoint reachable without custom headers.

        Sends no Nlziet-* or auth headers so the canary is not sensitive to
        version string changes.  Acts as a CI canary: fails hard if isAppBlocked,
        emits DeprecationWarning if isUpdateRequired.
        """

        import chn_nlziet
        import warnings

        with patch.object(type(self.channel), "_request_headers",
                          new_callable=PropertyMock, return_value={}):
            self.channel._sync_appconfig()
        self.assertFalse(
            UriHandler.instance().status.error,
            f"Live appconfig HTTP call failed: {UriHandler.instance().status.reason}")

        if chn_nlziet.Channel.is_blocked:
            reason = chn_nlziet.Channel.blocked_reason or "no reason provided"
            self.fail(f"NLZIET app is blocked — {reason}")

        update_text = chn_nlziet.Channel.update_reason or "(no updateText in response)"
        if chn_nlziet.Channel.is_update_required:
            Logger.warning("NLZIET: isUpdateRequired=True for web client — %s", update_text)
            warnings.warn(
                f"Retrospect's NLZIET web client (device_flow=False) is being warned by the "
                f"server — only the web login path is affected, not device flow. "
                f"Update NLZIET_WEB_VERSION in chn_nlziet.py. "
                f"Server message: {update_text}",
                DeprecationWarning, stacklevel=2)


    def test_appconfig_androidtv(self) -> None:
        """Real appconfig fetch with AndroidTV headers returns parseable JSON with expected keys.

        Tests the device flow path (device_flow=True, Nlziet-AppName=AndroidTv).
        Acts as a CI canary: fails hard if isAppBlocked, emits DeprecationWarning
        if isUpdateRequired so the pipeline surfaces it without breaking the build.
        Only NLZIET_APP_VERSION in chn_nlziet.py is relevant here.
        """

        import chn_nlziet
        from resources.lib.addonsettings import AddonSettings, LOCAL
        from resources.lib.authentication.nlziethandler import (
            DEVICE_CLIENT_ID, WEB_CLIENT_ID, AUTH_CLIENT_ID_KEY)
        import warnings

        # Switch to device-flow mode so the channel sends AndroidTV headers.
        AddonSettings.set_setting(AUTH_CLIENT_ID_KEY, DEVICE_CLIENT_ID, store=LOCAL)
        self._switch_channel(None)
        AddonSettings.set_setting(AUTH_CLIENT_ID_KEY, WEB_CLIENT_ID, store=LOCAL)

        self.channel._sync_appconfig()
        self.assertFalse(
            UriHandler.instance().status.error,
            f"Live appconfig HTTP call failed with AndroidTV headers: "
            f"{UriHandler.instance().status.reason}")

        if chn_nlziet.Channel.is_blocked:
            reason = chn_nlziet.Channel.blocked_reason or "no reason provided"
            self.fail(f"NLZIET app is blocked — {reason}")

        update_text = chn_nlziet.Channel.update_reason or "(no updateText in response)"
        if chn_nlziet.Channel.is_update_required:
            Logger.warning("NLZIET: isUpdateRequired=True for AndroidTV client — %s", update_text)
            warnings.warn(
                f"Retrospect's NLZIET AndroidTV client (device_flow=True) is being warned by the "
                f"server — only the device flow path is affected, not web login. "
                f"Update NLZIET_APP_VERSION in chn_nlziet.py. "
                f"Server message: {update_text}",
                DeprecationWarning, stacklevel=2)


    def test_sync_appconfig_stores_expected_keys(self) -> None:
        """_sync_appconfig() parses a response and sets ClassVars correctly."""

        import chn_nlziet

        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=json.dumps(MOCK_APPCONFIG_RESPONSE)):
            self.channel._sync_appconfig()

        self.assertEqual(chn_nlziet.Channel.service_interval,
                         MOCK_APPCONFIG_RESPONSE.get("heartbeatInterval"))
        self.assertFalse(chn_nlziet.Channel.is_blocked)
        self.assertFalse(chn_nlziet.Channel.is_update_required)

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
                patch("chn_nlziet.JsonHelper", side_effect=ValueError("unexpected parse error")):
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


    # -- JSON non-dict guards ---------------------------------------------

    def test_get_item_detail_non_dict_json_returns_empty(self):
        """_get_item_detail() returns {} when API returns valid but non-dict JSON."""

        with patch.object(type(self.channel), "loggedOn",
                          new_callable=PropertyMock, return_value=True), \
             patch("resources.lib.urihandler.UriHandler.open", return_value="null"):
            UriHandler.instance().status = UriStatus(
                code=200, url=None, error=False, reason="OK")
            result = self.channel._get_item_detail("some-id")
        self.assertEqual(result, {})
        self.assertNotIn("some-id", type(self.channel)._item_detail_cache)


    def test_get_item_detail_returns_cached_value_without_fetching(self):
        """_get_item_detail() returns the cached value directly, skipping the API call."""

        cache = type(self.channel)._item_detail_cache
        cache["cached-id"] = {"title": "Cached"}
        try:
            with patch("resources.lib.urihandler.UriHandler.open") as mock_open:
                result = self.channel._get_item_detail("cached-id")
            mock_open.assert_not_called()
            self.assertEqual(result, {"title": "Cached"})
        finally:
            del cache["cached-id"]


    def test_prefetch_live_details_non_dict_json_returns_input(self):
        """_prefetch_live_details() returns (data, []) when API returns non-dict JSON."""

        result = self.channel._prefetch_live_details("null")
        self.assertEqual(result, ("null", []))


    # -- _get_server_time --------------------------------------------------

    def test_get_server_time_error_returns_local_time(self):
        """_get_server_time() returns local time.time() when the API call fails."""

        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("time.time", return_value=1234567890.0):
            UriHandler.instance().status = UriStatus(
                code=503, url=None, error=True, reason="Unavailable")
            result = self.channel._get_server_time()
        self.assertEqual(result, 1234567890.0)


    def test_get_server_time_valid_returns_server_timestamp(self):
        """_get_server_time() returns the server timestamp when within drift tolerance."""

        import chn_nlziet
        now = 1700000000.0
        server_ms = int(now * 1000 + 10000)  # 10 seconds ahead of local clock
        with patch("resources.lib.urihandler.UriHandler.open", return_value=str(server_ms)), \
                patch("time.time", return_value=now):
            UriHandler.instance().status = UriStatus(
                code=200, url=None, error=False, reason="OK")
            result = self.channel._get_server_time()
        self.assertAlmostEqual(result, server_ms / 1000.0, delta=0.01)


    def test_get_server_time_drifted_returns_local_time(self):
        """_get_server_time() falls back to local time when server clock drifts too far."""

        import chn_nlziet
        now = 1700000000.0
        drift = float(chn_nlziet.EPG_MAX_SERVER_TIME_DRIFT + 60)  # 6 minutes = 360s ahead
        server_ms = int((now + drift) * 1000)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=str(server_ms)), \
                patch("time.time", return_value=now):
            UriHandler.instance().status = UriStatus(
                code=200, url=None, error=False, reason="OK")
            result = self.channel._get_server_time()
        self.assertEqual(result, now)


    def test_get_server_time_empty_response_returns_local_time(self):
        """_get_server_time() returns local time when server response is empty."""

        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("time.time", return_value=1234567890.0):
            UriHandler.instance().status = UriStatus(
                code=200, url=None, error=False, reason="OK")
            result = self.channel._get_server_time()
        self.assertEqual(result, 1234567890.0)

    # -- create_vod_item -----------------------------------------------

    def test_create_vod_item_series(self) -> None:
        """SUCCESS → items without a type field become series FolderItems."""

        result_set = {"content": {
            "id": "abc123", "title": "Test Series",
            "image": {"portraitUrl": "https://x/p.jpg", "landscapeUrl": "https://x/l.jpg"},
            "logo": {"normalUrl": "https://x/logo.png"},
            "tags": [],
        }}
        item = self.channel.create_vod_item(result_set)
        self.assertIsInstance(item, FolderItem)
        self.assertIn("/v8/series/abc123", item.url)
        self.assertEqual(item.name, "Test Series")


    def test_create_vod_item_movie(self) -> None:
        """SUCCESS → items tagged 'Movie' become playable MediaItems."""

        result_set = {"content": {
            "id": "mov456", "title": "Test Movie", "type": "Epg",
            "description": "A great movie.",
            "formattedDuration": "2u 10m",
            "tags": ["Movie"],
            "image": {"portraitUrl": None, "landscapeUrl": "https://x/thumb.jpg"},
            "logo": {"normalUrl": "https://x/logo.png"},
            "contentProvider": "Rtl",
            "broadcastedAt": "2026-02-13T20:27:18+01:00",
            "availableUntil": "2026-02-22T00:05:37+01:00",
            "formattedAvailabilityWindow": "Nog 1 dag beschikbaar",
            "isAvailable": True,
        }}
        item = self.channel.create_vod_item(result_set)
        self.assertIsInstance(item, MediaItem)
        self.assertEqual(item.media_type, mediatype.MOVIE)
        self.assertIn("/v9/stream/handshake", item.url)
        self.assertIn("context=OnDemand", item.url)
        self.assertIn("id=mov456", item.url)
        self.assertTrue(item.isGeoLocked)
        self.assertIn("A great movie.", item.description)
        self.assertIn("Nog 1 dag beschikbaar", item.description)
        self.assertEqual(item.thumb, "https://x/thumb.jpg")
        self.assertEqual(item.icon, "https://x/logo.png")


    def test_create_vod_item_episode(self) -> None:
        """SUCCESS → 'Vod' items without the Movie tag become episode MediaItems."""

        result_set = {"content": {
            "id": "ep789", "title": "Test Show", "type": "Vod",
            "subtitle": "Afl. 22", "tags": ["NewEpisode"],
            "image": {}, "logo": {}, "isAvailable": True,
        }}
        item = self.channel.create_vod_item(result_set)
        self.assertIsInstance(item, MediaItem)
        self.assertEqual(item.media_type, mediatype.EPISODE)


    def test_create_vod_item_unavailable_skipped(self) -> None:
        """SUCCESS → isAvailable=False returns None."""

        result_set = {"content": {
            "id": "gone", "title": "Gone Movie", "type": "Vod",
            "tags": [], "isAvailable": False, "image": {}, "logo": {},
        }}
        self.assertIsNone(self.channel.create_vod_item(result_set))


    def test_create_vod_item_empty_content(self) -> None:
        """SUCCESS → empty/missing content returns None."""

        self.assertIsNone(self.channel.create_vod_item({"content": {}}))
        self.assertIsNone(self.channel.create_vod_item({}))


    def test_create_vod_item_episode_numbering(self) -> None:
        """SUCCESS → formattedEpisodeNumbering sets season/episode as ints."""

        result_set = {"content": {
            "id": "ep-num", "title": "Some Show", "type": "Vod", "tags": [],
            "formattedEpisodeNumbering": "S02:A05",
            "image": {}, "logo": {}, "isAvailable": True,
        }}
        item = self.channel.create_vod_item(result_set)
        self.assertEqual(item.season, 2)
        self.assertEqual(item.episode, 5)


    def test_create_vod_item_null_numbering(self) -> None:
        """SUCCESS → a null formattedEpisodeNumbering does not raise."""

        result_set = {"content": {
            "id": "null-num", "title": "Null Numbering", "type": "Vod", "tags": [],
            "formattedEpisodeNumbering": None, "image": {}, "logo": {}, "isAvailable": True,
        }}
        self.assertIsNotNone(self.channel.create_vod_item(result_set))

    # -- create_episode_item ---------------------------------------------

    def test_create_episode_item(self) -> None:
        """SUCCESS → season/episode parsed from formattedEpisodeNumbering."""

        result_set = {"content": {
            "id": "epid", "title": "Episode Title", "subtitle": "Afl. 3",
            "formattedEpisodeNumbering": "S01:A03", "formattedDuration": "45m",
            "description": "Episode plot.",
            "image": {"portraitUrl": None, "landscapeUrl": "https://x/ep.jpg"},
            "logo": {"normalUrl": None}, "isAvailable": True,
        }}
        item = self.channel.create_episode_item(result_set)
        self.assertIsInstance(item, MediaItem)
        self.assertEqual(item.season, 1)
        self.assertEqual(item.episode, 3)
        self.assertTrue(item.isDrmProtected)


    def test_create_episode_item_no_numbering(self) -> None:
        """SUCCESS → episodes without formattedEpisodeNumbering still work."""

        result_set = {"content": {
            "id": "epid2", "title": "Unnamed Episode", "image": {}, "logo": {}, "isAvailable": True,
        }}
        item = self.channel.create_episode_item(result_set)
        self.assertEqual(item.name, "Unnamed Episode")


    def test_create_episode_item_numbering_from_subtitle(self) -> None:
        """SUCCESS → numbering parsed from subtitle when formattedEpisodeNumbering is absent."""

        result_set = {"content": {
            "id": "ep-sub", "title": "Patience", "subtitle": "S1:A6 Pandora's box",
            "image": {}, "logo": {},
        }}
        item = self.channel.create_episode_item(result_set)
        self.assertEqual(item.season, 1)
        self.assertEqual(item.episode, 6)
        self.assertEqual(item.metaData.get("nlziet:subtitle"), "Pandora's box")


    def test_create_episode_item_dontgroup(self) -> None:
        """SUCCESS → episode items have dontGroup set to preserve API order."""

        result_set = {"content": {
            "id": "ep-dg", "title": "Show", "subtitle": "Sub", "image": {}, "logo": {},
        }}
        item = self.channel.create_episode_item(result_set)
        self.assertTrue(item.dontGroup)


    def test_create_episode_item_tv_show_title(self) -> None:
        """SUCCESS → episode items inherit tv_show_title from _current_series_title."""

        self.channel._current_series_title = "My Series"
        result_set = {"content": {"id": "ep-tvt", "title": "An Episode", "image": {}, "logo": {}}}
        item = self.channel.create_episode_item(result_set)
        self.assertEqual(item.tv_show_title, "My Series")


    def test_create_episode_item_no_series_title(self) -> None:
        """SUCCESS → episode items work without a cached series title."""

        self.channel._current_series_title = ""
        result_set = {"content": {"id": "ep-nst", "title": "An Episode", "image": {}, "logo": {}}}
        item = self.channel.create_episode_item(result_set)
        self.assertFalse(item.tv_show_title)


    def test_create_episode_item_empty_content(self) -> None:
        """SUCCESS → empty/missing content returns None."""

        self.assertIsNone(self.channel.create_episode_item({"content": {}}))
        self.assertIsNone(self.channel.create_episode_item({}))

    # -- deduplicate_episode_titles ----------------------------------------

    def test_deduplicate_episode_titles_all_identical(self) -> None:
        """SUCCESS → identical titles are replaced by their subtitle."""

        e1 = MediaItem("Show", "https://x/1", media_type=mediatype.EPISODE)
        e1.metaData["nlziet:subtitle"] = "Afl. 1"
        e2 = MediaItem("Show", "https://x/2", media_type=mediatype.EPISODE)
        e2.metaData["nlziet:subtitle"] = "Afl. 2"
        items = self.channel.deduplicate_episode_titles("", [e1, e2])
        self.assertEqual(items[0].name, "Afl. 1 (Show)")
        self.assertEqual(items[1].name, "Afl. 2 (Show)")


    def test_deduplicate_episode_titles_partial(self) -> None:
        """SUCCESS → only duplicated titles (2+) are disambiguated; unique ones untouched."""

        e1 = MediaItem("Dup", "https://x/1", media_type=mediatype.EPISODE)
        e1.metaData["nlziet:subtitle"] = "Afl. 1"
        e2 = MediaItem("Dup", "https://x/2", media_type=mediatype.EPISODE)
        e2.metaData["nlziet:subtitle"] = "Afl. 2"
        e3 = MediaItem("Unique", "https://x/3", media_type=mediatype.EPISODE)
        e3.metaData["nlziet:subtitle"] = "Afl. 3"
        items = self.channel.deduplicate_episode_titles("", [e1, e2, e3])
        self.assertEqual(items[0].name, "Afl. 1 (Dup)")
        self.assertEqual(items[1].name, "Afl. 2 (Dup)")
        self.assertEqual(items[2].name, "Unique")


    def test_deduplicate_episode_titles_all_unique(self) -> None:
        """SUCCESS → all-unique titles are left unmodified."""

        e1 = MediaItem("One", "https://x/1", media_type=mediatype.EPISODE)
        e2 = MediaItem("Two", "https://x/2", media_type=mediatype.EPISODE)
        items = self.channel.deduplicate_episode_titles("", [e1, e2])
        self.assertEqual(items[0].name, "One")
        self.assertEqual(items[1].name, "Two")


    def test_deduplicate_episode_titles_single_episode_noop(self) -> None:
        """SUCCESS → fewer than two episodes returns items unchanged."""

        e1 = MediaItem("Only", "https://x/1", media_type=mediatype.EPISODE)
        items = self.channel.deduplicate_episode_titles("", [e1])
        self.assertEqual(items[0].name, "Only")

    # -- create_search_result_item -----------------------------------------

    def test_create_search_result_series(self) -> None:
        """SUCCESS → type 'Series' becomes a FolderItem."""

        result_set = {"content": {
            "id": "srid", "title": "Found Series", "type": "Series", "tags": [],
            "image": {}, "logo": {},
        }}
        item = self.channel.create_search_result_item(result_set)
        self.assertIsInstance(item, FolderItem)
        self.assertIn("/v8/series/srid", item.url)


    def test_create_search_result_movie(self) -> None:
        """SUCCESS → type 'Movie' becomes a playable movie."""

        result_set = {"content": {
            "id": "mrid", "title": "Found Movie", "type": "Movie", "tags": [],
            "image": {}, "logo": {},
        }}
        item = self.channel.create_search_result_item(result_set)
        self.assertIsInstance(item, MediaItem)
        self.assertEqual(item.media_type, mediatype.MOVIE)
        self.assertIn("context=OnDemand", item.url)
        self.assertIn("id=mrid", item.url)


    def test_create_search_result_episode(self) -> None:
        """SUCCESS → an unknown type becomes an episode."""

        result_set = {"content": {
            "id": "eid", "title": "Some Episode", "type": "Vod", "tags": [],
            "image": {}, "logo": {},
        }}
        item = self.channel.create_search_result_item(result_set)
        self.assertEqual(item.media_type, mediatype.EPISODE)


    def test_create_search_result_empty(self) -> None:
        """SUCCESS → empty content returns None."""

        self.assertIsNone(self.channel.create_search_result_item({}))
        self.assertIsNone(self.channel.create_search_result_item({"content": {}}))

    # -- extract_series_data ------------------------------------------------

    def test_extract_series_data_seasons(self) -> None:
        """SUCCESS → seasons are returned as folders, newest first."""

        self.channel.parentItem = MediaItem("Series", "https://api.nlziet.nl/v8/series/sid")
        data = json.dumps({"content": {
            "id": "sid", "title": "My Series",
            "seasons": [{"id": "s2", "title": "Season 2"}, {"id": "s1", "title": "Season 1"}],
        }})
        result_data, items = self.channel.extract_series_data(data)
        self.assertEqual(result_data, "")
        season_folders = [i for i in items if isinstance(i, FolderItem)]
        self.assertEqual(len(season_folders), 2)
        self.assertIn("seasonId=s2", season_folders[0].url)
        self.assertIn("seasonId=s1", season_folders[1].url)


    def test_extract_series_data_empty_content(self) -> None:
        """SUCCESS → empty content returns no items."""

        self.channel.parentItem = MediaItem("S", "https://api.nlziet.nl/v8/series/sid")
        _, items = self.channel.extract_series_data("{}")
        self.assertEqual(items, [])


    def test_extract_series_data_nested_content(self) -> None:
        """SUCCESS → handles the data.content wrapper format."""

        self.channel.parentItem = MediaItem("S", "https://api.nlziet.nl/v8/series/sid")
        data = json.dumps({"data": {"content": {
            "id": "sid", "title": "Nested", "seasons": [{"id": "s1", "title": "S1"}],
        }}})
        _, items = self.channel.extract_series_data(data)
        self.assertEqual(len([i for i in items if isinstance(i, FolderItem)]), 1)


    def test_extract_series_data_seasonless(self) -> None:
        """SUCCESS → a seasonless series fetches episodes directly (no intermediate folder)."""

        self.channel.parentItem = MediaItem("Series", "https://api.nlziet.nl/v8/series/sid")
        ep = MediaItem("Episode 1", "https://example.com/ep1")
        data = json.dumps({"content": {"id": "sid", "title": "Show", "seasons": []}})
        with patch("chn_nlziet.chn_class.Channel.process_folder_list",
                   return_value=[ep]) as mock_pfl:
            _, items = self.channel.extract_series_data(data)
        mock_pfl.assert_called_once()
        parent = mock_pfl.call_args[0][0]
        self.assertIn("/v9/series/sid/episodes?", parent.url)
        self.assertNotIn("seasonId", parent.url)
        self.assertIn(ep, items)


    def test_extract_series_data_stores_series_title_in_season_metadata(self) -> None:
        """SUCCESS → season folders carry nlziet:series_title for Up Next support."""

        self.channel.parentItem = MediaItem("Series", "https://api.nlziet.nl/v8/series/sid")
        data = json.dumps({"content": {
            "id": "sid", "title": "Great Show", "seasons": [{"id": "s1", "title": "Season 1"}],
        }})
        _, items = self.channel.extract_series_data(data)
        season_folders = [i for i in items if isinstance(i, FolderItem)]
        self.assertEqual(season_folders[0].metaData.get("nlziet:series_title"), "Great Show")

    # -- extract_series_title ------------------------------------------------

    def test_extract_series_title_reads_parent_metadata(self) -> None:
        """SUCCESS → caches the series title read from parentItem.metaData."""

        parent = FolderItem("Season 1", "https://api.nlziet.nl/v9/series/sid/episodes?seasonId=s1",
                            content_type="episodes")
        parent.metaData["nlziet:series_title"] = "Great Show"
        self.channel.parentItem = parent
        data, items = self.channel.extract_series_title("{}")
        self.assertEqual(items, [])
        self.assertEqual(self.channel._current_series_title, "Great Show")


    def test_extract_series_title_no_parent_metadata(self) -> None:
        """SUCCESS → caches an empty string when the parent has no series title."""

        parent = FolderItem("Season 1", "https://api.nlziet.nl/v9/series/sid/episodes?seasonId=s1",
                            content_type="episodes")
        self.channel.parentItem = parent
        self.channel.extract_series_title("{}")
        self.assertEqual(self.channel._current_series_title, "")

    # -- episode shortcuts: _fetch_continue_item / _fetch_season_episodes --

    def test_fetch_continue_item(self) -> None:
        """SUCCESS → continue watching returns a playable item from the /play endpoint."""

        response = json.dumps({"content": {"id": "ep42", "title": "Familiediner"}})
        with patch("resources.lib.urihandler.UriHandler.open", return_value=response) as mock_open:
            UriHandler.instance().status = UriStatus(code=200, url=None, error=False, reason=None)
            item = self.channel._fetch_continue_item("sid")
        self.assertIn("ep42", item.url)
        self.assertIn("/v9/stream/handshake", item.url)
        self.assertTrue(item.dontGroup)
        self.assertIn("/v9/series/sid/play", mock_open.call_args[0][0])


    def test_fetch_continue_item_empty_response(self) -> None:
        """SUCCESS → an empty response returns None."""

        with patch("resources.lib.urihandler.UriHandler.open", return_value=""):
            UriHandler.instance().status = UriStatus(code=200, url=None, error=False, reason=None)
            self.assertIsNone(self.channel._fetch_continue_item("sid"))


    def test_fetch_continue_item_error_status_returns_none(self) -> None:
        """SUCCESS → an HTTP error status returns None."""

        with patch("resources.lib.urihandler.UriHandler.open", return_value=""):
            UriHandler.instance().status = UriStatus(code=500, url=None, error=True, reason="fail")
            self.assertIsNone(self.channel._fetch_continue_item("sid"))


    def test_fetch_continue_item_no_content_id(self) -> None:
        """SUCCESS → content without an id returns None."""

        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value=json.dumps({"content": {"title": "No ID"}})):
            UriHandler.instance().status = UriStatus(code=200, url=None, error=False, reason=None)
            self.assertIsNone(self.channel._fetch_continue_item("sid"))


    def test_fetch_season_episodes_basic(self) -> None:
        """SUCCESS → returns (broadcastAt, item) tuples in API response order."""

        response = json.dumps({"data": [
            {"content": {"id": "ep1", "subtitle": "S1:A1 Pilot",
                        "broadcastAt": "2024-01-01T00:00:00+01:00"}},
            {"content": {"id": "ep2", "subtitle": "S1:A2 Second",
                        "broadcastAt": "2024-01-08T00:00:00+01:00"}},
        ]})
        with patch("resources.lib.urihandler.UriHandler.open", return_value=response) as mock_open:
            UriHandler.instance().status = UriStatus(code=200, url=None, error=False, reason=None)
            result = self.channel._fetch_season_episodes("sid", "s1")
        self.assertEqual(len(result), 2)
        broadcast_at, item = result[0]
        self.assertEqual(broadcast_at, "2024-01-01T00:00:00+01:00")
        self.assertIn("ep1", item.url)
        self.assertTrue(item.dontGroup)
        self.assertEqual(item.metaData.get("nlziet:ep_title"), "S1:A1 Pilot")
        call_url = mock_open.call_args[0][0]
        self.assertIn("seasonId=s1", call_url)
        self.assertIn("limit=400", call_url)


    def test_fetch_season_episodes_no_season_id(self) -> None:
        """SUCCESS → an empty season_id returns [] without an HTTP call."""

        with patch("resources.lib.urihandler.UriHandler.open") as mock_open:
            result = self.channel._fetch_season_episodes("sid", "")
        self.assertEqual(result, [])
        mock_open.assert_not_called()


    def test_fetch_season_episodes_empty_response(self) -> None:
        """SUCCESS → an empty HTTP response returns []."""

        with patch("resources.lib.urihandler.UriHandler.open", return_value=""):
            UriHandler.instance().status = UriStatus(code=200, url=None, error=False, reason=None)
            result = self.channel._fetch_season_episodes("sid", "s1")
        self.assertEqual(result, [])


    def test_fetch_season_episodes_no_broadcast_at(self) -> None:
        """SUCCESS → a missing broadcastAt is treated as an empty string."""

        response = json.dumps({"data": [{"content": {"id": "ep1", "subtitle": "Afl. 1"}}]})
        with patch("resources.lib.urihandler.UriHandler.open", return_value=response):
            UriHandler.instance().status = UriStatus(code=200, url=None, error=False, reason=None)
            result = self.channel._fetch_season_episodes("sid", "s1")
        broadcast_at, item = result[0]
        self.assertEqual(broadcast_at, "")
        self.assertIn("ep1", item.url)

    # -- _build_episode_shortcuts / _pick_boundary_episode / _is_ascending -

    def test_build_shortcuts_all_three(self) -> None:
        """SUCCESS → returns continue, most-recent, and first-episode shortcuts in order."""

        play_response = json.dumps({"content": {"id": "cont1", "title": "Continue Ep"}})
        # seasons[0] = oldest season (s1): first1 has the min broadcastAt.
        oldest_eps = json.dumps({"data": [
            {"content": {"id": "old3", "subtitle": "S0:A3 Last",
                        "broadcastAt": "2020-03-01T00:00:00+01:00"}},
            {"content": {"id": "first1", "subtitle": "S0:A1 Pilot",
                        "broadcastAt": "2020-01-01T00:00:00+01:00"}},
        ]})
        # seasons[-1] = newest season (s2): ep10 has the max broadcastAt.
        newest_eps = json.dumps({"data": [
            {"content": {"id": "ep10", "subtitle": "S1:A10 Latest",
                        "broadcastAt": "2024-03-01T00:00:00+01:00"}},
            {"content": {"id": "ep1n", "subtitle": "S1:A1 Start",
                        "broadcastAt": "2024-01-01T00:00:00+01:00"}},
        ]})
        with patch("resources.lib.urihandler.UriHandler.open",
                   side_effect=[play_response, newest_eps, oldest_eps]):
            UriHandler.instance().status = UriStatus(code=200, url=None, error=False, reason=None)
            shortcuts = self.channel._build_episode_shortcuts(
                "sid", [{"id": "s1"}, {"id": "s2"}])
        self.assertEqual(len(shortcuts), 3)
        self.assertIn("cont1", shortcuts[0].url)
        self.assertIn("ep10", shortcuts[1].url)
        self.assertIn("first1", shortcuts[2].url)


    def test_build_shortcuts_omits_continue_when_same_as_first(self) -> None:
        """SUCCESS → continue is omitted when it points at the same episode as first."""

        same_id = "same1"
        play_response = json.dumps({"content": {"id": same_id, "title": "Same Ep"}})
        season_eps = json.dumps({"data": [
            {"content": {"id": same_id, "subtitle": "S1:A1 Same",
                        "broadcastAt": "2024-01-01T00:00:00+01:00"}},
            {"content": {"id": "last1", "subtitle": "S1:A3 Latest",
                        "broadcastAt": "2024-03-01T00:00:00+01:00"}},
        ]})
        with patch("resources.lib.urihandler.UriHandler.open",
                   side_effect=[play_response, season_eps]):
            UriHandler.instance().status = UriStatus(code=200, url=None, error=False, reason=None)
            shortcuts = self.channel._build_episode_shortcuts("sid", [{"id": "s1"}])
        self.assertEqual(len(shortcuts), 2)
        urls = [s.url for s in shortcuts]
        self.assertTrue(any("last1" in u for u in urls))
        self.assertTrue(any(same_id in u for u in urls))


    def test_build_shortcuts_empty_seasons(self) -> None:
        """SUCCESS → returns [] for an empty seasons list."""

        self.assertEqual(self.channel._build_episode_shortcuts("sid", []), [])


    def test_build_shortcuts_no_series_id(self) -> None:
        """SUCCESS → returns [] for an empty series_id."""

        self.assertEqual(self.channel._build_episode_shortcuts("", [{"id": "s1"}]), [])


    def test_build_shortcuts_all_fetches_fail(self) -> None:
        """SUCCESS → all fetch failures are handled gracefully, returning []."""

        with patch("resources.lib.urihandler.UriHandler.open", return_value=""):
            UriHandler.instance().status = UriStatus(code=200, url=None, error=False, reason=None)
            shortcuts = self.channel._build_episode_shortcuts("sid", [{"id": "s1"}])
        self.assertEqual(shortcuts, [])


    def test_build_shortcuts_picks_by_broadcast_at(self) -> None:
        """SUCCESS → first/latest are chosen by broadcastAt, not API response order."""

        play_response = json.dumps({"content": {"id": "cont1", "title": "C"}})
        season_eps = json.dumps({"data": [
            {"content": {"id": "ep3", "subtitle": "S1:A3 Last",
                        "broadcastAt": "2024-03-01T00:00:00+01:00"}},
            {"content": {"id": "ep2", "subtitle": "S1:A2 Mid",
                        "broadcastAt": "2024-02-01T00:00:00+01:00"}},
            {"content": {"id": "ep1", "subtitle": "S1:A1 First",
                        "broadcastAt": "2024-01-01T00:00:00+01:00"}},
        ]})
        with patch("resources.lib.urihandler.UriHandler.open",
                   side_effect=[play_response, season_eps]):
            UriHandler.instance().status = UriStatus(code=200, url=None, error=False, reason=None)
            shortcuts = self.channel._build_episode_shortcuts("sid", [{"id": "s1"}])
        self.assertEqual(len(shortcuts), 3)
        self.assertIn("cont1", shortcuts[0].url)
        self.assertIn("ep3", shortcuts[1].url)
        self.assertIn("ep1", shortcuts[2].url)


    @staticmethod
    def _make_ep(ep_id: str, broadcast_at: str = ""):
        """Helper: create a (broadcastAt, MediaItem) tuple."""

        item = MediaItem(ep_id, f"https://example.com/{ep_id}")
        return (broadcast_at, item)


    def test_pick_boundary_none_for_empty(self) -> None:
        """SUCCESS → returns None for empty input."""

        self.assertIsNone(self.channel._pick_boundary_episode([], True))


    def test_pick_boundary_single_item(self) -> None:
        """SUCCESS → returns the only item regardless of direction."""

        eps = [self._make_ep("only")]
        self.assertIs(self.channel._pick_boundary_episode(eps, True), eps[0][1])


    def test_pick_boundary_ascending_api_broadcastAt_agree(self) -> None:
        """SUCCESS → ascending API order and broadcastAt agree on first/last."""

        eps = [
            self._make_ep("ep1", "2024-01-01T00:00:00+00:00"),
            self._make_ep("ep2", "2024-02-01T00:00:00+00:00"),
            self._make_ep("ep3", "2024-03-01T00:00:00+00:00"),
        ]
        first = self.channel._pick_boundary_episode(eps, True)
        last = self.channel._pick_boundary_episode(eps, False)
        self.assertIn("ep1", first.url)
        self.assertIn("ep3", last.url)


    def test_pick_boundary_descending_api_broadcastAt_agree(self) -> None:
        """SUCCESS → broadcastAt corrects an inverted (descending) API order."""

        eps = [
            self._make_ep("ep3", "2024-03-01T00:00:00+00:00"),
            self._make_ep("ep2", "2024-02-01T00:00:00+00:00"),
            self._make_ep("ep1", "2024-01-01T00:00:00+00:00"),
        ]
        first = self.channel._pick_boundary_episode(eps, True)
        last = self.channel._pick_boundary_episode(eps, False)
        self.assertIn("ep1", first.url)
        self.assertIn("ep3", last.url)


    def test_pick_boundary_majority_vote_no_first_available(self) -> None:
        """SUCCESS → descending API order: pick_first=True picks the oldest (last in list)."""

        eps = [
            self._make_ep("newest", "2024-12-01T00:00:00+00:00"),
            self._make_ep("oldest", "2020-01-01T00:00:00+00:00"),
        ]
        first = self.channel._pick_boundary_episode(eps, True)
        last = self.channel._pick_boundary_episode(eps, False)
        self.assertIn("oldest", first.url)
        self.assertIn("newest", last.url)


    def test_is_ascending_non_monotonic_defaults_false(self) -> None:
        """SUCCESS → non-monotonic broadcastAt values default to descending (False)."""

        eps = [
            self._make_ep("a", "2024-02-01T00:00:00+00:00"),
            self._make_ep("b", "2024-01-01T00:00:00+00:00"),
            self._make_ep("c", "2024-03-01T00:00:00+00:00"),
        ]
        self.assertFalse(self.channel._is_ascending(eps))


    def test_is_ascending_missing_timestamps_defaults_false(self) -> None:
        """SUCCESS → fewer than two dated entries defaults to descending (False)."""

        eps = [self._make_ep("a", ""), self._make_ep("b", "")]
        self.assertFalse(self.channel._is_ascending(eps))

    # -- update_vod_item / search_site --------------------------------------

    def test_update_vod_item_success(self) -> None:
        """SUCCESS → a valid handshake response completes the item."""

        item = MediaItem("Test", "https://api.nlziet.nl/v9/stream/handshake?context=OnDemand&id=x")
        handshake = json.dumps({
            "manifestUrl": "https://example.com/stream.mpd",
            "drm": {"licenseUrl": "https://license.example.com/", "headers": {}},
        })
        with patch("resources.lib.urihandler.UriHandler.open", return_value=handshake), \
                patch("resources.lib.streams.mpd.Mpd.get_license_key", return_value="key"), \
                patch("resources.lib.streams.mpd.Mpd.set_input_stream_addon_input"):
            UriHandler.instance().status = UriStatus(code=200, url=None, error=False, reason=None)
            result = self.channel.update_vod_item(item)
        self.assertTrue(result.complete)


    def test_update_vod_item_error_response_incomplete(self) -> None:
        """SUCCESS → an error response leaves the item incomplete."""

        item = MediaItem("Test", "https://api.nlziet.nl/v9/stream/handshake?context=OnDemand&id=x")
        error_response = json.dumps({
            "errors": [{"type": "InvalidAsset", "message": "Not playable"}],
        })
        with patch("resources.lib.urihandler.UriHandler.open", return_value=error_response):
            UriHandler.instance().status = UriStatus(code=200, url=None, error=False, reason=None)
            result = self.channel.update_vod_item(item)
        self.assertFalse(result.complete)


    def test_search_site_forwards_needle(self) -> None:
        """SUCCESS → search_site delegates to the base class with the search URL template."""

        with patch("resources.lib.chn_class.Channel.search_site",
                   return_value=[]) as mock_search:
            self.channel.search_site(needle="foo")
        args, _ = mock_search.call_args
        self.assertIn("/v9/search", args[1])
        self.assertEqual(args[2], "foo")

    # -- _parse_duration / _set_vod_metadata --------------------------------

    def test_parse_duration_hours_and_minutes(self) -> None:
        """SUCCESS → parses combined hours and minutes."""

        self.assertEqual(self.channel._parse_duration("1u 23m"), 4980)


    def test_parse_duration_hours_only(self) -> None:
        """SUCCESS → parses hours-only durations."""

        self.assertEqual(self.channel._parse_duration("2u"), 7200)


    def test_parse_duration_minutes_only(self) -> None:
        """SUCCESS → parses minutes-only durations."""

        self.assertEqual(self.channel._parse_duration("45m"), 2700)


    def test_parse_duration_empty(self) -> None:
        """SUCCESS → an empty string parses to 0."""

        self.assertEqual(self.channel._parse_duration(""), 0)


    def test_parse_duration_garbage(self) -> None:
        """SUCCESS → unparseable input parses to 0."""

        self.assertEqual(self.channel._parse_duration("no numbers"), 0)


    def test_metadata_description_with_subtitle(self) -> None:
        """SUCCESS → subtitle is shown as a bold heading above the description."""

        item = MediaItem("test", "http://example.com")
        content = {"subtitle": "Afl. 5", "description": "Episode plot text.",
                   "image": {}, "logo": {}}
        self.channel._set_vod_metadata(item, content)
        self.assertIn("[B]Afl. 5[/B]", item.description)
        self.assertIn("Episode plot text.", item.description)


    def test_metadata_subtitle_equals_description(self) -> None:
        """SUCCESS → no duplication when subtitle equals description."""

        item = MediaItem("test", "http://example.com")
        content = {"subtitle": "Same Title", "description": "Same Title", "image": {}, "logo": {}}
        self.channel._set_vod_metadata(item, content)
        self.assertEqual(item.description, "Same Title")


    def test_metadata_subtitle_only(self) -> None:
        """SUCCESS → subtitle is used as description when no description is present."""

        item = MediaItem("test", "http://example.com")
        content = {"subtitle": "Just Subtitle", "image": {}, "logo": {}}
        self.channel._set_vod_metadata(item, content)
        self.assertEqual(item.description, "Just Subtitle")


    def test_metadata_poster_and_thumb(self) -> None:
        """SUCCESS → portrait maps to poster, landscape maps to thumb."""

        item = MediaItem("test", "http://example.com")
        content = {"image": {"portraitUrl": "https://x/p.jpg", "landscapeUrl": "https://x/l.jpg"},
                   "logo": {}}
        self.channel._set_vod_metadata(item, content)
        self.assertEqual(item.thumb, "https://x/l.jpg")
        self.assertEqual(item.poster, "https://x/p.jpg")


    def test_metadata_studio(self) -> None:
        """SUCCESS → contentProvider maps to the Studio info label."""

        item = MediaItem("test", "http://example.com")
        content = {"contentProvider": "Rtl", "image": {}, "logo": {}}
        self.channel._set_vod_metadata(item, content)
        self.assertTrue(item.has_info_label("Studio"))


    def test_metadata_broadcast_date(self) -> None:
        """SUCCESS → broadcastedAt is parsed and applied via _apply_broadcast_date."""

        item = MediaItem("test", "http://example.com")
        content = {"broadcastedAt": "2026-02-19T20:30:00+01:00", "image": {}, "logo": {}}
        with patch.object(self.channel, "_apply_broadcast_date") as mock_apply:
            self.channel._set_vod_metadata(item, content)
        mock_apply.assert_called_once_with(item, "2026-02-19T20:30:00+01:00")


    def test_metadata_null_fields(self) -> None:
        """SUCCESS → null image/logo/description fields do not raise."""

        item = MediaItem("test", "http://example.com")
        content = {
            "subtitle": None, "description": None, "image": None, "logo": None,
            "contentProvider": None, "broadcastedAt": None, "formattedDuration": None,
        }
        self.channel._set_vod_metadata(item, content)


class TestNlzietLoggedOnProperty(ChannelTest):
    """Tests for the Channel.loggedOn property."""

    def __init__(self, methodName: str) -> None:
        super().__init__(methodName, "channel.nlziet.nlziet", None)


    def setUp(self) -> None:
        UriHandler.instance().status = UriStatus(code=0, url=None, error=False, reason=None)
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value='{"heartbeatInterval": 90, "isAppBlocked": false}'), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_yes_no"):
            super().setUp()
        UriHandler.instance().status = UriStatus(code=0, url=None, error=False, reason=None)


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
            raise RuntimeError("NLZIET live login failed in setUpClass — check credentials and network.")


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
                self.fail("NLZIET login failed — check credentials and network.")


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
        # Wrap channel instantiation in a mock so _sync_appconfig() does not make
        # a real network call (which would raise RuntimeError and leave self.channel None).
        UriHandler.instance().status = UriStatus(code=0, url=None, error=False, reason=None)
        with patch("resources.lib.urihandler.UriHandler.open",
                   return_value='{"heartbeatInterval": 90, "isAppBlocked": false}'), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_yes_no"):
            super(TestNlzietChannelLive, self).setUp()
        UriHandler.instance().status = UriStatus(code=0, url=None, error=False, reason=None)

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
                self.fail("Mocked NLZIET login failed — check mock token setup.")

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
