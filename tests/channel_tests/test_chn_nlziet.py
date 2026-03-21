# SPDX-License-Identifier: GPL-3.0-or-later
import json
import time
import sys
from typing import Any, Dict, Optional
from unittest.mock import patch


import xbmcgui as _xbmcgui
if not hasattr(_xbmcgui, "WindowXMLDialog"):
    class _FakeWindowXMLDialog:
        def __new__(cls: "type[_FakeWindowXMLDialog]", *args: Any, **kwargs: Any) -> object: return object.__new__(cls)  # type: ignore[misc]
        def __init__(self, *args: Any, **kwargs: Any) -> None: pass
    _xbmcgui.WindowXMLDialog = _FakeWindowXMLDialog  # type: ignore[attr-defined]
    sys.modules["xbmcgui"].WindowXMLDialog = _FakeWindowXMLDialog  # type: ignore[attr-defined]

from resources.lib.urihandler import UriHandler, UriStatus
from . channeltest import ChannelTest


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


    def test_service_interval_default(self) -> None:
        self.assertEqual(self.channel.service_interval, 90)


    def test_service_interval_is_positive(self) -> None:
        self.assertGreater(self.channel.service_interval, 0)


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


    def test_service_update_updates_service_interval(self) -> None:
        raw = self._appconfig_raw({"heartbeatInterval": 120})
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw):
            self.channel.service_update()
        import chn_nlziet
        self.assertEqual(chn_nlziet.Channel.service_interval, 120)


    def test_service_update_uses_default_when_no_heartbeat(self) -> None:
        raw = self._appconfig_raw()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw):
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


    def test_stale_by_count_resets_to_defaults(self) -> None:
        """SUCCESS → reaching APPCONFIG_SYNC_MAX_FAIL errors resets channel state to defaults."""

        import chn_nlziet
        chn_nlziet.Channel.service_interval = 300
        chn_nlziet.Channel.is_blocked = True
        chn_nlziet.Channel._appconfig_fail_count = chn_nlziet.APPCONFIG_SYNC_MAX_FAIL - 1
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification"):
            UriHandler.instance().status = UriStatus(
                code=503, url=None, error=True, reason="Service Unavailable")
            self.channel._sync_appconfig()
        self.assertEqual(chn_nlziet.Channel.service_interval, chn_nlziet.APPCONFIG_HEARTBEAT_DEFAULT)
        self.assertFalse(chn_nlziet.Channel.is_blocked)
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


    def test_stale_by_time_resets_is_update_required_and_reason(self) -> None:
        """SUCCESS → time-based stale reset clears is_update_required and update_reason."""

        import chn_nlziet
        chn_nlziet.Channel.is_update_required = True
        chn_nlziet.Channel.update_reason = "Please upgrade"
        chn_nlziet.Channel._appconfig_last_synced_at = (
            time.time() - chn_nlziet.APPCONFIG_SYNC_MAX_AGE - 1)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.xbmcwrapper.XbmcWrapper.show_notification"):
            UriHandler.instance().status = UriStatus(
                code=503, url=None, error=True, reason="Service Unavailable")
            self.channel._sync_appconfig()
        self.assertFalse(chn_nlziet.Channel.is_update_required)
        self.assertEqual(chn_nlziet.Channel.update_reason, "")


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


    def setUp(self) -> None:
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


    def _sync_with(self, overrides: dict) -> None:
        """Call _sync_appconfig with a response built from the base mock + overrides."""

        base = {"heartbeatInterval": 90, "isAppBlocked": False, "isUpdateRequired": False}
        payload = json.dumps(dict(base, **overrides))
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
