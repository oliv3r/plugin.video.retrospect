# SPDX-License-Identifier: GPL-3.0-or-later
import json
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, "channels/channel.nlziet/nlziet")

from . channeltest import ChannelTest


class TestNlzietChannel(ChannelTest):
    # noinspection PyPep8Naming
    def __init__(self, methodName):  # NOSONAR
        super(TestNlzietChannel, self).__init__(methodName, "channel.nlziet.nlziet", None)

    def setUp(self):
        super().setUp()
        # Stash the class-level service_interval so heartbeat tests don't leak state.
        import chn_nlziet
        self._orig_service_interval = chn_nlziet.Channel.service_interval

    def tearDown(self):
        import chn_nlziet
        chn_nlziet.Channel.service_interval = self._orig_service_interval
        super().tearDown()

    def test_channel_exists(self):
        self.assertIsNotNone(self.channel)

    def test_service_interval_default(self):
        self.assertEqual(self.channel.service_interval, 90)

    def test_service_interval_is_positive(self):
        self.assertIsNotNone(self.channel.service_interval)
        self.assertGreater(self.channel.service_interval, 0)

    def test_on_service_calls_refresh_appconfig(self):
        with patch.object(self.channel, "_refresh_appconfig") as mock_refresh:
            self.channel.on_service()
        mock_refresh.assert_called_once_with()

    def test_refresh_appconfig_returns_dict_on_success(self):
        payload = {"epgCacheTime": 300, "isAppBlocked": False}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            result = self.channel._refresh_appconfig()
        self.assertIsInstance(result, dict)
        self.assertEqual(result["epgCacheTime"], 300)

    def test_refresh_appconfig_updates_service_interval(self):
        payload = {"heartbeatInterval": 120, "isAppBlocked": False}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel._refresh_appconfig()
        import chn_nlziet
        self.assertEqual(chn_nlziet.Channel.service_interval, 120)

    def test_refresh_appconfig_uses_default_when_no_heartbeat(self):
        payload = {"isAppBlocked": False}
        raw = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel._refresh_appconfig()
        import chn_nlziet
        self.assertEqual(chn_nlziet.Channel.service_interval, chn_nlziet._APPCONFIG_HEARTBEAT_DEFAULT)

    def test_refresh_appconfig_stores_fetched_at(self):
        payload = {"isAppBlocked": False}
        raw = json.dumps(payload)
        before = time.time()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel._refresh_appconfig()
        stored_json = mock_set.call_args[0][1]
        stored = json.loads(stored_json)
        self.assertGreaterEqual(stored["_fetched_at"], before)

    def test_refresh_appconfig_empty_response_returns_empty_dict(self):
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            result = self.channel._refresh_appconfig()
        self.assertEqual(result, {})
        mock_set.assert_not_called()

    def test_refresh_appconfig_bad_json_returns_empty_dict(self):
        with patch("resources.lib.urihandler.UriHandler.open", return_value="not json"), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            result = self.channel._refresh_appconfig()
        self.assertEqual(result, {})
        mock_set.assert_not_called()
