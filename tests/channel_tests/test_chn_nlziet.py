# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import time
import unittest
from unittest.mock import patch

from . channeltest import ChannelTest
from tests.channel_tests.nlziet_mocks import MOCK_APPCONFIG_RESPONSE


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
        """__sync_appconfig() sets Channel.is_blocked when isAppBlocked is true."""
        payload = {"isAppBlocked": True}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel._Channel__sync_appconfig()
        import chn_nlziet
        self.assertTrue(chn_nlziet.Channel.is_blocked)

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
