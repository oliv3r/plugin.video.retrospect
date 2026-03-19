# SPDX-License-Identifier: GPL-3.0-or-later

import os
import tempfile
import time
import unittest
from typing import TYPE_CHECKING, Any, Tuple
from unittest.mock import MagicMock, patch

if TYPE_CHECKING:
    from resources.lib.service import _ChannelTask, RetroService


class TestRetroService(unittest.TestCase):
    """Unit tests for resources/lib/service.RetroService."""

    @classmethod
    def setUpClass(cls) -> None:
        from resources.lib.logger import Logger
        Logger.create_logger(None, str(cls), min_log_level=0)

    @classmethod
    def tearDownClass(cls) -> None:
        from resources.lib.addonsettings import AddonSettings
        AddonSettings.clear_cached_addon_settings_object()
        from resources.lib.logger import Logger
        if Logger.exists():
            Logger.instance().close_log()

    def setUp(self) -> None:
        pass

    def test_run_calls_setup_iptvsimple_at_startup(self) -> None:
        """_run() calls IptvSimpleHelper.setup_iptvsimple() before entering the loop."""
        from resources.lib.service import RetroService

        instance = RetroService()
        instance.waitForAbort = MagicMock(return_value=True)

        with patch('resources.lib.helpers.iptvsimplehelper.IptvSimpleHelper.setup_iptvsimple') as mock_setup, \
                patch('resources.lib.service.RetroService._iptv_active', return_value=True), \
                patch.object(instance, '_enroll_channels', return_value=[]):
            instance._run()

        mock_setup.assert_called_once_with()

    def test_run_exits_when_no_service_channels(self) -> None:
        """_run() exits without entering the waitForAbort loop when no tasks are enrolled."""
        from resources.lib.service import RetroService

        instance = RetroService()
        instance.waitForAbort = MagicMock(return_value=True)

        with patch('resources.lib.helpers.iptvsimplehelper.IptvSimpleHelper.setup_iptvsimple'), \
                patch.object(instance, '_enroll_channels', return_value=[]):
            instance._run()

        instance.waitForAbort.assert_not_called()

    def test_run_enters_loop_when_channels_exist(self) -> None:
        """_run() enters the waitForAbort loop when at least one task is enrolled."""
        from resources.lib.service import RetroService, _ChannelTask, _CHECK_INTERVAL

        instance = RetroService()
        instance.waitForAbort = MagicMock(return_value=True)
        task = _ChannelTask("Ch", MagicMock(), 0.0, 10)

        with patch('resources.lib.helpers.iptvsimplehelper.IptvSimpleHelper.setup_iptvsimple'), \
                patch.object(instance, '_enroll_channels', return_value=[task]):
            instance._run()

        instance.waitForAbort.assert_called_once_with(_CHECK_INTERVAL)

    def test_run_exits_when_all_task_intervals_are_none(self) -> None:
        """_run() exits without looping when every enrolled task has interval=None."""
        from resources.lib.service import RetroService, _ChannelTask

        instance = RetroService()
        instance.waitForAbort = MagicMock(return_value=True)
        task = _ChannelTask("Ch", MagicMock(), 0.0, None)

        with patch('resources.lib.helpers.iptvsimplehelper.IptvSimpleHelper.setup_iptvsimple'), \
                patch.object(instance, '_enroll_channels', return_value=[task]):
            instance._run()

        instance.waitForAbort.assert_not_called()

    def test_tick_calls_service_update_when_interval_elapsed(self) -> None:
        """_tick() calls service_update() for channels whose interval has passed."""
        from resources.lib.service import RetroService, _ChannelTask

        instance = RetroService()
        instance.abortRequested = MagicMock(return_value=False)
        fake_channel = MagicMock()
        fake_channel.service_update.__name__ = 'service_update'
        tasks = [_ChannelTask("TestCh", fake_channel.service_update, 0.0, 1)]

        instance._tick(tasks)

        fake_channel.service_update.assert_called_once()

    def test_tick_skips_channel_before_interval(self) -> None:
        """_tick() does not call service_update() when the interval has not elapsed."""
        from resources.lib.service import RetroService, _ChannelTask

        instance = RetroService()
        instance.abortRequested = MagicMock(return_value=False)
        fake_channel = MagicMock()
        fake_channel.service_update.__name__ = 'service_update'
        tasks = [_ChannelTask("TestCh", fake_channel.service_update, time.time(), 3600)]

        instance._tick(tasks)

        fake_channel.service_update.assert_not_called()

    def test_tick_stops_on_abort(self) -> None:
        """_tick() stops processing further tasks when abort is requested."""
        from resources.lib.service import RetroService, _ChannelTask

        instance = RetroService()
        instance.abortRequested = MagicMock(return_value=True)
        cb = MagicMock()
        cb.__name__ = 'cb'
        tasks = [_ChannelTask("Ch", cb, 0.0, 1)]

        instance._tick(tasks)

        cb.assert_not_called()

    def test_tick_logs_warning_on_channel_error(self) -> None:
        """_tick() logs a warning but continues when service_update() raises an exception."""
        from resources.lib.service import RetroService, _ChannelTask

        instance = RetroService()
        instance.abortRequested = MagicMock(return_value=False)
        bad_cb = MagicMock(side_effect=RuntimeError("boom"))
        bad_cb.__name__ = 'bad_cb'
        tasks = [_ChannelTask("ErrCh", bad_cb, 0.0, 1)]

        with patch('resources.lib.service.Logger') as mock_logger:
            instance._tick(tasks)

        mock_logger.warning.assert_called_once()

    def test_tick_fires_tasks_independently(self) -> None:
        """_tick() fires elapsed tasks and skips non-elapsed ones independently."""
        from resources.lib.service import RetroService, _ChannelTask

        instance = RetroService()
        instance.abortRequested = MagicMock(return_value=False)
        fast_cb = MagicMock()
        fast_cb.__name__ = 'fast_cb'
        slow_cb = MagicMock()
        slow_cb.__name__ = 'slow_cb'
        tasks = [
            _ChannelTask("FastCh", fast_cb, 0.0, 1),
            _ChannelTask("SlowCh", slow_cb, time.time(), 3600),
        ]

        instance._tick(tasks)

        fast_cb.assert_called_once()
        slow_cb.assert_not_called()

    def _make_channel_info(self, name: str, guid: str,
                           service_interval: Any = None,
                           has_iptv: bool = False) -> Tuple[MagicMock, MagicMock]:
        """Build a minimal ChannelInfo-like mock."""
        ci = MagicMock()
        ci.channelName = name
        ci.guid = guid
        ci.has_iptv = has_iptv
        channel = MagicMock()
        channel.service_interval = service_interval
        ci.get_channel.return_value = channel
        return ci, channel

    def test_enroll_registers_valid_channel(self) -> None:
        """_enroll_channels() registers a channel with a valid interval."""
        from resources.lib.service import RetroService

        ci, ch = self._make_channel_info("TestCh", "guid-ok", service_interval=60)

        with patch('resources.lib.service.ChannelIndex') as mock_ci:
            mock_ci.get_register.return_value.get_channels.return_value = [ci]
            instance = RetroService()
            tasks = instance._enroll_channels()

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].interval, 60)
        self.assertEqual(tasks[0].channel_name, "TestCh")

    def test_enroll_registers_iptv_only_channel(self) -> None:
        """_enroll_channels() registers an iptv task for a channel with has_iptv=True."""
        from resources.lib.service import RetroService, IPTV_INTERVAL_DEFAULT

        ci, ch = self._make_channel_info("IptvCh", "guid-iptv",
                                         service_interval=None, has_iptv=True)
        ch.iptv_refresh_interval = IPTV_INTERVAL_DEFAULT

        with patch('resources.lib.service.ChannelIndex') as mock_ci, \
                patch('resources.lib.service.RetroService._iptv_active', return_value=True):
            mock_ci.get_register.return_value.get_channels.return_value = [ci]
            instance = RetroService()
            tasks = instance._enroll_channels()

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].interval, IPTV_INTERVAL_DEFAULT)
        self.assertEqual(tasks[0].channel_name, "IptvCh")

    def test_enroll_iptv_channel_uses_default_when_interval_is_none(self) -> None:
        """_enroll_channels() falls back to IPTV_INTERVAL_DEFAULT when iptv_refresh_interval is None."""
        from resources.lib.service import RetroService, IPTV_INTERVAL_DEFAULT

        ci, ch = self._make_channel_info("IptvCh", "guid-iptv",
                                         service_interval=None, has_iptv=True)
        ch.iptv_refresh_interval = None

        with patch('resources.lib.service.ChannelIndex') as mock_ci, \
                patch('resources.lib.service.RetroService._iptv_active', return_value=True):
            mock_ci.get_register.return_value.get_channels.return_value = [ci]
            instance = RetroService()
            tasks = instance._enroll_channels()

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].interval, IPTV_INTERVAL_DEFAULT)

    def test_enroll_clamps_interval_above_max(self) -> None:
        """_enroll_channels() clamps intervals exceeding MAX_SERVICE_INTERVAL."""
        from resources.lib.service import RetroService, MAX_SERVICE_INTERVAL

        ci, ch = self._make_channel_info("SlowCh", "guid-slow",
                                         service_interval=MAX_SERVICE_INTERVAL + 100)

        with patch('resources.lib.service.ChannelIndex') as mock_ci:
            mock_ci.get_register.return_value.get_channels.return_value = [ci]
            instance = RetroService()
            tasks = instance._enroll_channels()

        self.assertEqual(tasks[0].interval, MAX_SERVICE_INTERVAL)

    def test_enroll_clamps_interval_below_min(self) -> None:
        """_enroll_channels() clamps intervals below MIN_SERVICE_INTERVAL."""
        from resources.lib.service import RetroService, MIN_SERVICE_INTERVAL

        ci, ch = self._make_channel_info("TinyCh", "guid-tiny",
                                         service_interval=MIN_SERVICE_INTERVAL - 1)

        with patch('resources.lib.service.ChannelIndex') as mock_ci:
            mock_ci.get_register.return_value.get_channels.return_value = [ci]
            instance = RetroService()
            tasks = instance._enroll_channels()

        self.assertEqual(tasks[0].interval, MIN_SERVICE_INTERVAL)

    def test_enroll_skips_invalid_interval(self) -> None:
        """_enroll_channels() skips channels with a non-integer service_interval."""
        from resources.lib.service import RetroService

        ci, ch = self._make_channel_info("BadCh", "guid-bad", service_interval="bad")

        with patch('resources.lib.service.ChannelIndex') as mock_ci:
            mock_ci.get_register.return_value.get_channels.return_value = [ci]
            instance = RetroService()
            tasks = instance._enroll_channels()

        self.assertEqual(len(tasks), 0)

    def test_enroll_skips_channel_without_service_interval(self) -> None:
        """_enroll_channels() ignores channels that do not set service_interval."""
        from resources.lib.service import RetroService

        ci, _ch = self._make_channel_info("NormalCh", "guid-n", service_interval=None)

        with patch('resources.lib.service.ChannelIndex') as mock_ci:
            mock_ci.get_register.return_value.get_channels.return_value = [ci]
            instance = RetroService()
            tasks = instance._enroll_channels()

        self.assertEqual(len(tasks), 0)


    def test_enroll_skips_channel_when_get_channel_returns_none(self) -> None:
        """_enroll_channels() skips a channel entry when get_channel() returns None."""
        from resources.lib.service import RetroService

        ci = MagicMock()
        ci.channelName = "BrokenCh"
        ci.get_channel.return_value = None

        with patch('resources.lib.service.ChannelIndex') as mock_ci:
            mock_ci.get_register.return_value.get_channels.return_value = [ci]
            instance = RetroService()
            tasks = instance._enroll_channels()

        self.assertEqual(len(tasks), 0)


    def test_tick_updates_last_run_on_exception(self) -> None:
        """_tick() updates last_run via finally even when service_update() raises."""
        from resources.lib.service import RetroService, _ChannelTask

        instance = RetroService()
        instance.abortRequested = MagicMock(return_value=False)
        bad_cb = MagicMock(side_effect=RuntimeError("boom"))
        bad_cb.__name__ = 'bad_cb'
        task = _ChannelTask("ErrCh", bad_cb, 0.0, 1)

        before = time.time()
        with patch('resources.lib.service.Logger'):
            instance._tick([task])

        self.assertGreaterEqual(task.last_run, before)


    def test_tick_reraises_keyboard_interrupt(self) -> None:
        """_tick() lets KeyboardInterrupt propagate out of the callback."""
        from resources.lib.service import RetroService, _ChannelTask

        instance = RetroService()
        instance.abortRequested = MagicMock(return_value=False)
        ki_cb = MagicMock(side_effect=KeyboardInterrupt)
        ki_cb.__name__ = 'ki_cb'
        task = _ChannelTask("KICh", ki_cb, 0.0, 1)

        with self.assertRaises(KeyboardInterrupt):
            instance._tick([task])


    # -- _write_channel_iptv_files ------------------------------------------

    def test_write_channel_iptv_files_writes_streams_and_epg(self) -> None:
        """_write_channel_iptv_files() calls write_playlist and write_epg when both are provided."""
        from resources.lib.service import RetroService

        instance = RetroService()
        channel_entry = MagicMock()
        channel_entry.id = "test.ch"
        channel = MagicMock()
        streams = [{"id": "ch1", "name": "N", "logo": "", "group": "G", "stream": "plugin://..."}]
        channel.create_iptv_streams.return_value = streams
        channel.create_iptv_epg.return_value = {"ch1": []}

        with tempfile.TemporaryDirectory() as tmp, \
                patch('resources.lib.service.Config.profileDir', tmp), \
                patch('resources.lib.service.ActionParser'), \
                patch('resources.lib.service.IptvSimpleHelper.write_playlist') as mock_m3u, \
                patch('resources.lib.service.IptvSimpleHelper.write_epg') as mock_epg:
            instance._write_channel_iptv_files(channel_entry, channel)

        mock_m3u.assert_called_once_with(streams, "test.ch")
        mock_epg.assert_called_once()


    def test_write_channel_iptv_files_skips_playlist_when_no_streams(self) -> None:
        """_write_channel_iptv_files() skips write_playlist when create_iptv_streams returns empty."""
        from resources.lib.service import RetroService

        instance = RetroService()
        channel_entry = MagicMock()
        channel = MagicMock()
        channel.create_iptv_streams.return_value = []
        channel.create_iptv_epg.return_value = None

        with tempfile.TemporaryDirectory() as tmp, \
                patch('resources.lib.service.Config.profileDir', tmp), \
                patch('resources.lib.service.ActionParser'), \
                patch('resources.lib.service.IptvSimpleHelper.write_playlist') as mock_m3u, \
                patch('resources.lib.service.IptvSimpleHelper.write_epg') as mock_epg:
            instance._write_channel_iptv_files(channel_entry, channel)

        mock_m3u.assert_not_called()
        mock_epg.assert_not_called()


    def test_write_channel_iptv_files_skips_epg_when_none(self) -> None:
        """_write_channel_iptv_files() writes playlist but skips epg when create_iptv_epg returns None."""
        from resources.lib.service import RetroService

        instance = RetroService()
        channel_entry = MagicMock()
        channel_entry.id = "test.ch"
        channel = MagicMock()
        streams = [{"id": "ch1", "name": "N", "logo": "", "group": "G", "stream": "plugin://..."}]
        channel.create_iptv_streams.return_value = streams
        channel.create_iptv_epg.return_value = None

        with tempfile.TemporaryDirectory() as tmp, \
                patch('resources.lib.service.Config.profileDir', tmp), \
                patch('resources.lib.service.ActionParser'), \
                patch('resources.lib.service.IptvSimpleHelper.write_playlist') as mock_m3u, \
                patch('resources.lib.service.IptvSimpleHelper.write_epg') as mock_epg:
            instance._write_channel_iptv_files(channel_entry, channel)

        mock_m3u.assert_called_once_with(streams, "test.ch")
        mock_epg.assert_not_called()


    def test_write_channel_iptv_files_returns_early_on_makedirs_failure(self) -> None:
        """_write_channel_iptv_files() returns without writing when makedirs fails."""
        from resources.lib.service import RetroService

        instance = RetroService()
        channel_entry = MagicMock()
        channel = MagicMock()

        with patch('resources.lib.service.Config.profileDir', '/nonexistent_base_xyz'), \
                patch('resources.lib.service.os.path.isdir', return_value=False), \
                patch('resources.lib.service.os.makedirs', side_effect=OSError("no space")), \
                patch('resources.lib.service.IptvSimpleHelper.write_playlist') as mock_m3u:
            instance._write_channel_iptv_files(channel_entry, channel)

        mock_m3u.assert_not_called()


    # -- onNotification -----------------------------------------------------

    def test_notification_wrong_method_is_ignored(self) -> None:
        """onNotification() does nothing for non-OnAddonEnabled events."""
        from resources.lib.service import RetroService

        instance = RetroService()
        with patch('resources.lib.service.IptvSimpleHelper.setup_iptvsimple') as mock_setup:
            instance.onNotification("sender", "System.OnAddonDisabled", '{"id":"pvr.iptvsimple"}')

        mock_setup.assert_not_called()


    def test_notification_empty_data_is_ignored(self) -> None:
        """onNotification() does nothing when data is empty."""
        from resources.lib.service import RetroService

        instance = RetroService()
        with patch('resources.lib.service.IptvSimpleHelper.setup_iptvsimple') as mock_setup:
            instance.onNotification("sender", "System.OnAddonEnabled", "")

        mock_setup.assert_not_called()


    def test_notification_pvr_iptvsimple_triggers_setup(self) -> None:
        """onNotification() calls setup_iptvsimple() when pvr.iptvsimple is enabled."""
        from resources.lib.service import RetroService

        instance = RetroService()
        with patch('resources.lib.service.IptvSimpleHelper.setup_iptvsimple') as mock_setup, \
                patch('resources.lib.service.RetroService._iptv_active', return_value=True):
            instance.onNotification("sender", "System.OnAddonEnabled", '{"id":"pvr.iptvsimple"}')

        mock_setup.assert_called_once_with()


    def test_notification_iptv_manager_triggers_setup(self) -> None:
        """onNotification() calls setup_iptvsimple() when service.iptv.manager is enabled."""
        from resources.lib.service import RetroService

        instance = RetroService()
        with patch('resources.lib.service.IptvSimpleHelper.setup_iptvsimple') as mock_setup, \
                patch('resources.lib.service.RetroService._iptv_active', return_value=True):
            instance.onNotification("sender", "System.OnAddonEnabled",
                                    '{"id":"service.iptv.manager"}')

        mock_setup.assert_called_once_with()


    def test_notification_unrelated_addon_is_ignored(self) -> None:
        """onNotification() does nothing when an unrelated addon is enabled."""
        from resources.lib.service import RetroService

        instance = RetroService()
        with patch('resources.lib.service.IptvSimpleHelper.setup_iptvsimple') as mock_setup:
            instance.onNotification("sender", "System.OnAddonEnabled", '{"id":"other.addon"}')

        mock_setup.assert_not_called()


class TestChnClassServiceInterface(unittest.TestCase):
    """Service callback interface on the Channel base class."""

    @classmethod
    def setUpClass(cls) -> None:
        from resources.lib.logger import Logger
        Logger.create_logger(None, str(cls), min_log_level=0)

    @classmethod
    def tearDownClass(cls) -> None:
        from resources.lib.addonsettings import AddonSettings
        AddonSettings.clear_cached_addon_settings_object()
        from resources.lib.logger import Logger
        if Logger.exists():
            Logger.instance().close_log()

    def test_service_interval_default_is_none(self) -> None:
        """Channel.service_interval defaults to None (opts out of service callbacks)."""
        from resources.lib.chn_class import Channel
        self.assertIsNone(Channel.service_interval)

    def test_subclass_can_set_service_interval(self) -> None:
        """Subclass service_interval does not affect the base class attribute."""
        from resources.lib.chn_class import Channel

        class _MyChannel(Channel):
            service_interval = 300

        self.assertEqual(_MyChannel.service_interval, 300)
        self.assertIsNone(Channel.service_interval)

    def test_subclass_can_override_service_update(self) -> None:
        """Subclass service_update() override is called correctly."""
        from resources.lib.chn_class import Channel

        called = []

        class _MyChannel(Channel):
            def service_update(self) -> None:
                called.append(True)

        object.__new__(_MyChannel).service_update()
        self.assertEqual(called, [True])


    def test_base_service_update_is_noop(self) -> None:
        """Base Channel.service_update() is a no-op and does not raise."""
        from resources.lib.chn_class import Channel

        instance = object.__new__(Channel)
        instance.service_update()  # must not raise

    def test_run_skips_setup_iptvsimple_when_iptv_active_false(self) -> None:
        """SUCCESS → setup_iptvsimple() not called when _iptv_active returns False."""
        from resources.lib.service import RetroService

        instance = RetroService()
        instance.waitForAbort = MagicMock(return_value=True)

        with patch('resources.lib.service.RetroService._iptv_active', return_value=False), \
                patch('resources.lib.service.IptvSimpleHelper') as mock_helper, \
                patch.object(instance, '_enroll_channels', return_value=[]):
            instance._run()

        mock_helper.return_value.setup_iptvsimple.assert_not_called()

    def test_enroll_skips_iptv_task_when_iptv_active_false(self) -> None:
        """SUCCESS → IPTV task not enrolled when _iptv_active returns False."""
        from resources.lib.service import RetroService

        ci = MagicMock()
        ci.channelName = "IptvCh"
        ci.has_iptv = True
        ch = MagicMock()
        ch.service_interval = None
        ci.get_channel.return_value = ch

        with patch('resources.lib.service.RetroService._iptv_active', return_value=False), \
                patch('resources.lib.service.ChannelIndex') as mock_ci:
            mock_ci.get_register.return_value.get_channels.return_value = [ci]
            tasks = RetroService()._enroll_channels()

        self.assertEqual(len(tasks), 0)

    def test_notification_pvr_iptvsimple_skips_setup_when_iptv_active_false(self) -> None:
        """SUCCESS → setup_iptvsimple() not called for pvr.iptvsimple when _iptv_active is False."""
        from resources.lib.service import RetroService

        instance = RetroService()
        with patch('resources.lib.service.RetroService._iptv_active', return_value=False), \
                patch('resources.lib.service.IptvSimpleHelper') as mock_helper:
            instance.onNotification("sender", "System.OnAddonEnabled",
                                    '{"id":"pvr.iptvsimple"}')

        mock_helper.return_value.setup_iptvsimple.assert_not_called()

    def test_notification_iptv_manager_skips_setup_when_iptv_active_false(self) -> None:
        """SUCCESS → setup_iptvsimple() not called for service.iptv.manager when _iptv_active is False."""
        from resources.lib.service import RetroService

        instance = RetroService()
        with patch('resources.lib.service.RetroService._iptv_active', return_value=False), \
                patch('resources.lib.service.IptvSimpleHelper') as mock_helper:
            instance.onNotification("sender", "System.OnAddonEnabled",
                                    '{"id":"service.iptv.manager"}')

        mock_helper.return_value.setup_iptvsimple.assert_not_called()
