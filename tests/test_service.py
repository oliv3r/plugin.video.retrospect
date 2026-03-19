# SPDX-License-Identifier: GPL-3.0-or-later

import threading
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
        # Reset class-level singleton state between tests
        from resources.lib import service as svc_mod
        svc_mod.RetroService._instance = None
        svc_mod.RetroService._thread = None


    def test_start_spawns_thread_on_first_call(self) -> None:
        """start() creates an instance and starts a daemon thread on first call."""
        from resources.lib.service import RetroService

        with patch.object(RetroService, '_run'):
            RetroService.start()

        self.assertIsNotNone(RetroService._thread)
        self.assertIsNotNone(RetroService._instance)

    def test_start_is_idempotent_when_thread_alive(self) -> None:
        """start() does nothing when the thread is already running."""
        from resources.lib.service import RetroService

        alive_thread = MagicMock(spec=threading.Thread)
        alive_thread.is_alive.return_value = True
        fake_instance = MagicMock()
        RetroService._thread = alive_thread
        RetroService._instance = fake_instance

        RetroService.start()

        self.assertIs(RetroService._thread, alive_thread)
        self.assertIs(RetroService._instance, fake_instance)

    def test_start_restarts_when_thread_dead(self) -> None:
        """start() replaces a dead thread without logging an error."""
        from resources.lib.service import RetroService

        dead_thread = MagicMock(spec=threading.Thread)
        dead_thread.is_alive.return_value = False
        RetroService._thread = dead_thread

        with patch('resources.lib.service.Logger') as mock_logger, \
                patch.object(RetroService, '_run'):
            RetroService.start()

        mock_logger.error.assert_not_called()
        self.assertIsNot(RetroService._thread, dead_thread)
        self.assertIsNotNone(RetroService._instance)

    def test_run_exits_when_no_service_channels(self) -> None:
        """_run() exits without entering the waitForAbort loop when no tasks are enrolled."""
        from resources.lib.service import RetroService

        instance = RetroService()
        instance.waitForAbort = MagicMock(return_value=True)

        with patch.object(instance, '_enroll_channels', return_value=[]):
            instance._run()

        instance.waitForAbort.assert_not_called()

    def test_run_enters_loop_when_channels_exist(self) -> None:
        """_run() enters the waitForAbort loop when at least one task is enrolled."""
        from resources.lib.service import RetroService, _ChannelTask

        instance = RetroService()
        instance.waitForAbort = MagicMock(return_value=True)
        task = _ChannelTask("Ch", MagicMock(), 0.0, 10)

        with patch.object(instance, '_enroll_channels', return_value=[task]):
            instance._run()

        instance.waitForAbort.assert_called_once_with(10)

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

        with patch('resources.lib.service.ChannelIndex') as mock_ci:
            mock_ci.get_register.return_value.get_channels.return_value = [ci]
            instance = RetroService()
            tasks = instance._enroll_channels()

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].interval, IPTV_INTERVAL_DEFAULT)
        self.assertEqual(tasks[0].channel_name, "IptvCh")

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
