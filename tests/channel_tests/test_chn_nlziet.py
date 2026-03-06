# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, "channels/channel.nlziet/nlziet")
import api  # noqa: E402

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

    # -- Channel metadata --------------------------------------------------

    def test_channel_exists(self):
        self.assertIsNotNone(self.channel)

    def test_service_interval_default(self):
        self.assertEqual(self.channel.service_interval, 90)

    def test_service_interval_is_positive(self):
        self.assertGreater(self.channel.service_interval, 0)

    def test_mainlist_uri_is_live_endpoint(self):
        self.assertIn(api.API_V9_EPG_LIVE, self.channel.mainListUri)

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

    def test_on_service_skips_fresh_cache(self):
        """on_service() with a fresh cache should NOT make a network call."""
        payload = {"_fetched_at": time.time(), "epgCacheTime": 300}
        cached_json = json.dumps(payload)
        with patch("resources.lib.urihandler.UriHandler.open") as mock_open, \
                patch("resources.lib.addonsettings.AddonSettings.get_setting",
                      return_value=cached_json), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel.on_service()
        mock_open.assert_not_called()

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

    def test_on_service_stores_fetched_at(self):
        raw = self._appconfig_raw()
        before = time.time()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel.on_service()
        stored_json = mock_set.call_args[0][1]
        stored = json.loads(stored_json)
        self.assertGreaterEqual(stored["_fetched_at"], before)

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
                         chn_nlziet._APPCONFIG_HEARTBEAT_DEFAULT)

    # -- log_on ------------------------------------------------------------

    def test_log_on_cached_token_succeeds(self):
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

    # -- create_live_channel_item ------------------------------------------

    def _live_result_set(self, channel_id="npo1", title="NPO 1",
                         logo_url="https://example.com/npo1.png",
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
        self.assertIn("channel=npo1", item.url)
        self.assertEqual(item.thumb, "https://example.com/npo1.png")
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
        appconfig = {"liveStreamRestartStartPadding": 180}
        with patch("resources.lib.urihandler.UriHandler.open",
                   side_effect=[json.dumps(appconfig), handshake_response]), \
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
        AddonSettings.set_setting(NLZIETOAuth2Handler.AUTH_METHOD_SETTING, "web", store=LOCAL)
        handler = NLZIETOAuth2Handler(use_device_flow=False)
        auth = Authenticator(handler)
        result = auth.log_on(username=cls.username, password=cls.password)
        if not result.logged_on:
            raise unittest.SkipTest("NLZIET live login failed in setUpClass.")
        profiles = handler.list_profiles()
        if profiles:
            handler.set_profile(profiles[0]["id"])

    def setUp(self):
        super().setUp()
        if not self.channel.log_on(self.username, self.password):
            self.skipTest("NLZIET login failed.")

    def test_login_succeeds(self):
        """Live: log_on() with real credentials succeeds."""
        self.assertTrue(self.channel.loggedOn)

    def test_live_channels_returned(self):
        """Live: process_folder_list(None) returns a non-empty list after login."""
        items = self.channel.process_folder_list(None)
        self.assertIsNotNone(items)
        self.assertGreater(len(items), 0)
