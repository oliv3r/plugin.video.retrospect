# SPDX-License-Identifier: GPL-3.0-or-later
import json
import os
import time
import unittest
from typing import Optional
from unittest.mock import patch

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

    # -- service_update / appconfig cache ----------------------------------


    def _appconfig_raw(self, extra: "Optional[dict]" = None) -> str:
        payload = {"epgCacheTime": 300, "isAppBlocked": False}
        if extra:
            payload.update(extra)
        return json.dumps(payload)


    def test_service_update_fetches_stale_cache(self) -> None:
        """service_update() with a stale/empty cache triggers a fresh network fetch."""

        raw = self._appconfig_raw()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw) as mock_open, \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting"):
            self.channel.service_update()
        mock_open.assert_called_once()


    def test_service_update_empty_response_does_not_crash(self) -> None:
        with patch("resources.lib.urihandler.UriHandler.open", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel.service_update()
        mock_set.assert_not_called()


    def test_service_update_bad_json_does_not_crash(self) -> None:
        with patch("resources.lib.urihandler.UriHandler.open", return_value="not-json"), \
                patch("resources.lib.addonsettings.AddonSettings.get_setting", return_value=""), \
                patch("resources.lib.addonsettings.AddonSettings.set_setting") as mock_set:
            self.channel.service_update()
        mock_set.assert_not_called()


    def test_service_update_stores_synced_at(self) -> None:
        raw = self._appconfig_raw()
        before = time.time()
        with patch("resources.lib.urihandler.UriHandler.open", return_value=raw), \
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

    # -- appconfig: mocked equivalent of TestNlzietAppconfigLive -----------


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
    """Live integration tests for the /v7/appconfig endpoint.

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
