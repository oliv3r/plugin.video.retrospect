# coding=utf-8  # NOSONAR
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import tempfile
import threading
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

os.environ.setdefault("KODI_INTERACTIVE", "0")
os.environ.setdefault("KODI_HOME", "tests/home")


def setUpModule():  # noqa: N802
    from resources.lib.logger import Logger
    Logger.create_logger(None, "test_retroservice", min_log_level=0)
    from resources.lib.textures import TextureHandler
    from resources.lib.textures.local import Local
    TextureHandler._TextureHandler__TextureHandler = Local(Logger.instance())


def tearDownModule():  # noqa: N802
    from resources.lib.addonsettings import AddonSettings
    AddonSettings.clear_cached_addon_settings_object()
    from resources.lib.logger import Logger
    if Logger.exists():
        Logger.instance().close_log()

class TestChnClassServiceInterface(unittest.TestCase):
    """Service callback interface on the Channel base class."""

    def test_service_interval_default_is_none(self):
        from resources.lib.chn_class import Channel
        self.assertIsNone(Channel.service_interval)

    def test_on_service_returns_none(self):
        from resources.lib.chn_class import Channel
        channel = object.__new__(Channel)
        self.assertIsNone(channel.on_service())

    def test_subclass_can_set_service_interval(self):
        from resources.lib.chn_class import Channel

        class _MyChannel(Channel):
            service_interval = 300

        self.assertEqual(_MyChannel.service_interval, 300)
        self.assertIsNone(Channel.service_interval)  # base class unchanged

    def test_subclass_can_override_on_service(self):
        from resources.lib.chn_class import Channel

        called = []

        class _MyChannel(Channel):
            def on_service(self):
                called.append(True)

        object.__new__(_MyChannel).on_service()
        self.assertEqual(called, [True])


class _RetroServiceTestBase(unittest.TestCase):
    """Shared helpers for RetroService unit tests."""

    def _make_service(self):
        """RetroService instance with empty channel registry, bypassing __init__."""
        import retroservice
        svc = object.__new__(retroservice.RetroService)
        svc._service_channels = {}
        svc.abortRequested = lambda: False
        return svc

    @staticmethod
    def _make_channel(interval):
        ch = Mock()
        ch.service_interval = interval
        return ch


class TestRetroServiceTick(_RetroServiceTestBase):
    """RetroService._tick() dispatch logic."""

    def test_channel_called_on_first_tick(self):
        """last_run=0.0 means interval always elapsed → fires immediately."""
        svc = self._make_service()
        ch = self._make_channel(30)
        svc._service_channels["g1"] = [ch, 0.0, 30]

        with patch("retroservice.time.time", return_value=100.0):
            svc._tick()

        ch.on_service.assert_called_once()

    def test_channel_not_called_before_interval(self):
        """Channel is skipped when interval has not yet elapsed."""
        svc = self._make_service()
        ch = self._make_channel(300)
        svc._service_channels["g1"] = [ch, 80.0, 300]

        with patch("retroservice.time.time", return_value=110.0):  # only 30 s elapsed
            svc._tick()

        ch.on_service.assert_not_called()

    def test_channel_called_after_interval(self):
        """Channel fires once the full interval has elapsed."""
        svc = self._make_service()
        ch = self._make_channel(300)
        svc._service_channels["g1"] = [ch, 50.0, 300]

        with patch("retroservice.time.time", return_value=360.0):  # 310 s elapsed
            svc._tick()

        ch.on_service.assert_called_once()

    def test_last_run_updated_after_successful_call(self):
        """entry[1] is set to the current timestamp after on_service() succeeds."""
        svc = self._make_service()
        ch = self._make_channel(30)
        entry = [ch, 0.0, 30]
        svc._service_channels["g1"] = entry

        with patch("retroservice.time.time", return_value=100.0):
            svc._tick()

        self.assertEqual(entry[1], 100.0)

    def test_exception_swallowed_other_channels_still_fire(self):
        """Exception in one on_service() does not prevent others from running."""
        svc = self._make_service()
        failing = self._make_channel(30)
        failing.on_service.side_effect = RuntimeError("boom")
        ok = self._make_channel(30)
        svc._service_channels["g-fail"] = [failing, 0.0, 30]
        svc._service_channels["g-ok"] = [ok, 0.0, 30]

        with patch("retroservice.time.time", return_value=100.0):
            svc._tick()  # must not raise

        ok.on_service.assert_called_once()

    def test_last_run_updated_even_on_exception(self):
        """last_run is updated even when on_service() raises, to prevent hammering."""
        svc = self._make_service()
        ch = self._make_channel(30)
        ch.on_service.side_effect = RuntimeError("boom")
        entry = [ch, 0.0, 30]
        svc._service_channels["g1"] = entry

        with patch("retroservice.time.time", return_value=100.0):
            svc._tick()

        self.assertEqual(entry[1], 100.0)

    def test_empty_registry_is_noop(self):
        """_tick() with no registered channels does not raise."""
        svc = self._make_service()
        with patch("retroservice.time.time", return_value=100.0):
            svc._tick()

    def test_base_noop_on_service_called_cleanly(self):
        """Channel sets service_interval but does not override on_service() — base no-op
        fires cleanly and last_run is updated."""
        from resources.lib.chn_class import Channel

        class _OptInChannel(Channel):
            service_interval = 30

        ch = object.__new__(_OptInChannel)
        entry = [ch, 0.0, 30]
        svc = self._make_service()
        svc._service_channels["g1"] = entry

        with patch("retroservice.time.time", return_value=100.0):
            svc._tick()  # must not raise

        self.assertEqual(entry[1], 100.0)

    def test_base_exception_swallowed(self):
        """SystemExit from on_service() is caught; other channels still fire."""
        svc = self._make_service()
        failing = self._make_channel(30)
        failing.on_service.side_effect = SystemExit(1)
        ok = self._make_channel(30)
        svc._service_channels["g-fail"] = [failing, 0.0, 30]
        svc._service_channels["g-ok"] = [ok, 0.0, 30]

        with patch("retroservice.time.time", return_value=100.0):
            svc._tick()  # must not raise

        ok.on_service.assert_called_once()

    def test_keyboard_interrupt_propagates(self):
        """KeyboardInterrupt from on_service() is not swallowed."""
        svc = self._make_service()
        ch = self._make_channel(30)
        ch.on_service.side_effect = KeyboardInterrupt()
        svc._service_channels["g1"] = [ch, 0.0, 30]

        with patch("retroservice.time.time", return_value=100.0):
            with self.assertRaises(KeyboardInterrupt):
                svc._tick()

    def test_effective_interval_used_in_tick(self):
        """_tick() uses entry[2] (effective interval), not channel.service_interval."""
        svc = self._make_service()
        ch = self._make_channel(7200)   # channel declares 2 h
        entry = [ch, 0.0, 3600]        # but effective interval is clamped to 1 h

        svc._service_channels["g1"] = entry

        # At t=3601 the effective interval (3600 s) has elapsed
        with patch("retroservice.time.time", return_value=3601.0):
            svc._tick()

        ch.on_service.assert_called_once()


class TestRetroServiceLoadChannels(_RetroServiceTestBase):
    """RetroService._load_service_channels() filtering and registration logic."""

    def _make_channel_info(self, name, guid, interval):
        """Return a mock ChannelInfo and its backing fake module.

        :param interval: service_interval value (None → opts out; any other value → opts in).
        """
        ci = Mock()
        ci.path = "/fake/path"
        ci.moduleName = "fake_mod_%s" % guid.replace("-", "_")
        ci.channelName = name
        ci.guid = guid

        ch_instance = Mock()
        ch_instance.service_interval = interval
        ci.get_channel.return_value = ch_instance

        fake_mod = Mock()
        fake_mod.Channel.service_interval = interval
        ci._fake_mod = fake_mod

        return ci

    def _run_load(self, svc, channel_infos):
        """Invoke _load_service_channels() with all external deps mocked."""
        import importlib as _importlib
        fake_mods = {ci.moduleName: ci._fake_mod for ci in channel_infos}
        _real_import = _importlib.import_module  # capture before patching

        def fake_import(name):
            # Return fake module for channel modules; fall through for everything else.
            return fake_mods.get(name) or _real_import(name)

        mock_index = Mock()
        mock_index.get_channels.return_value = channel_infos

        with patch("resources.lib.helpers.channelimporter.ChannelIndex") as MockCI:
            MockCI.get_register.return_value = mock_index
            with patch("retroservice.importlib.import_module",
                       side_effect=fake_import):
                svc._load_service_channels()

    def test_channel_without_interval_not_registered(self):
        svc = self._make_service()
        ci = self._make_channel_info("NoService", "guid-no", None)
        self._run_load(svc, [ci])
        self.assertEqual(len(svc._service_channels), 0)

    def test_channel_with_interval_is_registered(self):
        svc = self._make_service()
        ci = self._make_channel_info("WithService", "guid-yes", 60)
        self._run_load(svc, [ci])
        self.assertIn("guid-yes", svc._service_channels)

    def test_registered_channel_instance_is_correct(self):
        svc = self._make_service()
        ci = self._make_channel_info("WithService", "guid-yes", 60)
        self._run_load(svc, [ci])
        self.assertIs(svc._service_channels["guid-yes"][0], ci.get_channel.return_value)

    def test_registered_channel_last_run_is_zero(self):
        svc = self._make_service()
        ci = self._make_channel_info("WithService", "guid-yes", 60)
        self._run_load(svc, [ci])
        self.assertEqual(svc._service_channels["guid-yes"][1], 0.0)

    def test_registered_channel_effective_interval_stored(self):
        svc = self._make_service()
        ci = self._make_channel_info("WithService", "guid-yes", 60)
        self._run_load(svc, [ci])
        self.assertEqual(svc._service_channels["guid-yes"][2], 60)

    def test_get_channel_returning_none_is_skipped(self):
        svc = self._make_service()
        ci = self._make_channel_info("BadChannel", "guid-bad", 60)
        ci.get_channel.return_value = None
        self._run_load(svc, [ci])
        self.assertEqual(len(svc._service_channels), 0)

    def test_import_error_skips_channel_without_raising(self):
        svc = self._make_service()
        ci = self._make_channel_info("Broken", "guid-broken", 60)

        mock_index = Mock()
        mock_index.get_channels.return_value = [ci]

        with patch("resources.lib.helpers.channelimporter.ChannelIndex") as MockCI:
            MockCI.get_register.return_value = mock_index
            with patch("retroservice.importlib.import_module",
                       side_effect=ImportError("missing")):
                svc._load_service_channels()  # must not raise

        self.assertEqual(len(svc._service_channels), 0)

    def test_mixed_channels_only_interval_ones_registered(self):
        svc = self._make_service()
        ci_a = self._make_channel_info("A", "guid-a", 60)
        ci_b = self._make_channel_info("B", "guid-b", None)
        ci_c = self._make_channel_info("C", "guid-c", 60)
        self._run_load(svc, [ci_a, ci_b, ci_c])

        self.assertIn("guid-a", svc._service_channels)
        self.assertNotIn("guid-b", svc._service_channels)
        self.assertIn("guid-c", svc._service_channels)

    def test_zero_interval_skipped(self):
        svc = self._make_service()
        ci = self._make_channel_info("ZeroInterval", "guid-zero", 0)
        self._run_load(svc, [ci])
        self.assertEqual(len(svc._service_channels), 0)

    def test_negative_interval_skipped(self):
        svc = self._make_service()
        ci = self._make_channel_info("NegInterval", "guid-neg", -30)
        self._run_load(svc, [ci])
        self.assertEqual(len(svc._service_channels), 0)

    def test_string_interval_skipped(self):
        svc = self._make_service()
        ci = self._make_channel_info("StrInterval", "guid-str", "300")
        self._run_load(svc, [ci])
        self.assertEqual(len(svc._service_channels), 0)

    def test_over_max_interval_clamped(self):
        """Interval > 3600 s is clamped to MAX_SERVICE_INTERVAL."""
        import retroservice
        svc = self._make_service()
        ci = self._make_channel_info("HugeInterval", "guid-huge", 7200)
        self._run_load(svc, [ci])
        self.assertIn("guid-huge", svc._service_channels)
        self.assertEqual(svc._service_channels["guid-huge"][2],
                         retroservice.MAX_SERVICE_INTERVAL)

    def test_over_warn_interval_accepted(self):
        """Interval > 600 s but <= 3600 s is accepted unchanged."""
        svc = self._make_service()
        ci = self._make_channel_info("SlowInterval", "guid-slow", 900)
        self._run_load(svc, [ci])
        self.assertIn("guid-slow", svc._service_channels)
        self.assertEqual(svc._service_channels["guid-slow"][2], 900)


class TestRetroServiceAbortGuard(_RetroServiceTestBase):
    """RetroService._tick() early-exit when abort is requested."""

    def test_tick_skips_all_channels_when_aborting(self):
        """_tick() returns immediately without calling on_service() if abort is set."""
        from unittest.mock import patch
        svc = self._make_service()
        ch = self._make_channel(30)
        svc._service_channels["g1"] = [ch, 0.0, 30]

        with patch.object(svc, 'abortRequested', return_value=True):
            with patch("retroservice.time.time", return_value=100.0):
                svc._tick()

        ch.on_service.assert_not_called()

    def test_tick_calls_channels_when_not_aborting(self):
        """_tick() proceeds normally when abort is not requested."""
        from unittest.mock import patch
        svc = self._make_service()
        ch = self._make_channel(30)
        svc._service_channels["g1"] = [ch, 0.0, 30]

        with patch.object(svc, 'abortRequested', return_value=False):
            with patch("retroservice.time.time", return_value=100.0):
                svc._tick()

        ch.on_service.assert_called_once()


class TestRetroServiceIptvConfig(unittest.TestCase):
    """Tests for _set_xml_setting and _configure_pvr_instance helpers."""

    @staticmethod
    def _set_xml_setting(*args, **kwargs):
        import retroservice
        return retroservice._set_xml_setting(*args, **kwargs)

    @staticmethod
    def _configure_pvr_instance(*args, **kwargs):
        import retroservice
        return retroservice._configure_pvr_instance(*args, **kwargs)

    # --- _set_xml_setting ---

    def test_set_xml_setting_updates_existing(self):
        content = '<settings version="2">\n    <setting id="foo">old</setting>\n</settings>'
        result = self._set_xml_setting(content, "foo", "new")
        self.assertIn('<setting id="foo">new</setting>', result)
        self.assertNotIn("old", result)

    def test_set_xml_setting_inserts_missing(self):
        content = '<settings version="2">\n</settings>'
        result = self._set_xml_setting(content, "bar", "baz")
        self.assertIn('<setting id="bar">baz</setting>', result)
        self.assertIn("</settings>", result)

    def test_set_xml_setting_strips_default_attr(self):
        content = ('<settings version="2">\n'
                   '    <setting id="foo" default="true">old</setting>\n'
                   '</settings>')
        result = self._set_xml_setting(content, "foo", "new")
        self.assertIn('<setting id="foo">new</setting>', result)
        self.assertNotIn('default="true"', result)

    def test_set_xml_setting_escapes_xml_special_chars(self):
        content = '<settings version="2">\n</settings>'
        result = self._set_xml_setting(content, "genresPath", "/tmp/Drama & Comedy <test>")
        root = ET.fromstring(result)
        self.assertEqual(root.find('./setting[@id="genresPath"]').text, "/tmp/Drama & Comedy <test>")

    def test_merge_genre_xmls_escapes_xml_special_chars(self):
        import retroservice

        with tempfile.TemporaryDirectory() as tmp:
            channel_dir = os.path.join(tmp, "channels", "channel.nlziet", "nlziet")
            os.makedirs(channel_dir)
            genre_file = os.path.join(channel_dir, "genres.xml")
            with open(genre_file, "w", encoding="utf-8") as fh:
                fh.write(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<genres>\n'
                    '  <genre genreId="0x10">Drama &amp; Comedy &lt;Live&gt;</genre>\n'
                    '</genres>\n'
                )

            with patch("retroservice.Config.rootDir", tmp):
                merged = retroservice._merge_genre_xmls()

        self.assertIsNotNone(merged)
        root = ET.fromstring(merged)
        self.assertEqual(
            root.find('./genre[@genreId="0x10"]').text,
            "Drama & Comedy <Live>"
        )

    def test_nlziet_genre_override_file_contains_expected_mappings(self):
        repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
        genres_path = os.path.join(repo_root, "channels", "channel.nlziet", "nlziet", "genres.xml")

        self.assertTrue(os.path.isfile(genres_path))

        root = ET.parse(genres_path).getroot()
        mappings = {elem.text: elem.get("genreId") for elem in root.findall("genre")}

        self.assertEqual(mappings.get("Talkshow"), "0x33")
        self.assertEqual(mappings.get("Jeugd/Familie"), "0x50")
        self.assertEqual(mappings.get("Culinair"), "0xA5")

    # --- _configure_pvr_instance ---

    def test_configure_pvr_instance_missing_dir_is_noop(self):
        """Missing pvr_data directory → returns without creating any files."""
        self._configure_pvr_instance("/nonexistent/pvr_data_path_xyz", genres_path=None)
        # No exception = pass

    def test_configure_pvr_instance_finds_by_name(self):
        """Phase 1: configures our own file and leaves other files untouched."""
        import tempfile
        with tempfile.TemporaryDirectory() as pvr_data:
            owned = os.path.join(pvr_data, "instance-settings-1.xml")
            with open(owned, "w") as fh:
                fh.write('<?xml version="1.0"?>\n<settings version="2">\n'
                         '    <setting id="kodi_addon_instance_name">Retrospect</setting>\n'
                         '</settings>\n')
            other = os.path.join(pvr_data, "instance-settings-2.xml")
            with open(other, "w") as fh:
                fh.write('<?xml version="1.0"?>\n<settings version="2">\n</settings>\n')

            self._configure_pvr_instance(pvr_data, genres_path=None)

            with open(owned) as fh:
                owned_content = fh.read()
            with open(other) as fh:
                other_content = fh.read()

        self.assertIn('<setting id="kodi_addon_instance_name">Retrospect</setting>',
                      owned_content)
        self.assertIn('<setting id="catchupEnabled">true</setting>', owned_content)
        self.assertNotIn("catchupEnabled", other_content)

    def test_configure_pvr_instance_claims_iptv_manager_file(self):
        """Phase 2: claims a file created by service.iptv.manager."""
        import tempfile
        with tempfile.TemporaryDirectory() as pvr_data:
            iptv_file = os.path.join(pvr_data, "instance-settings-1.xml")
            with open(iptv_file, "w") as fh:
                fh.write('<?xml version="1.0"?>\n<settings version="2">\n'
                         '    <setting id="m3uPathType">2</setting>\n'
                         '    <!-- created by service.iptv.manager -->\n'
                         '</settings>\n')

            self._configure_pvr_instance(pvr_data, genres_path=None)

            with open(iptv_file) as fh:
                content = fh.read()

        self.assertIn('<setting id="kodi_addon_instance_name">Retrospect</setting>', content)
        self.assertIn('<setting id="catchupEnabled">true</setting>', content)

    def test_configure_pvr_instance_creates_fresh_when_none_found(self):
        """Phase 3: creates instance-settings-N.xml with the next unused N."""
        import tempfile
        with tempfile.TemporaryDirectory() as pvr_data:
            for n in (1, 2):
                path = os.path.join(pvr_data, "instance-settings-%d.xml" % n)
                with open(path, "w") as fh:
                    fh.write('<?xml version="1.0"?>\n<settings version="2">\n</settings>\n')

            self._configure_pvr_instance(pvr_data, genres_path=None)

            created = os.path.join(pvr_data, "instance-settings-3.xml")
            self.assertTrue(os.path.isfile(created))
            with open(created) as fh:
                content = fh.read()

        self.assertIn('<setting id="kodi_addon_instance_name">Retrospect</setting>', content)
        self.assertIn('<setting id="catchupEnabled">true</setting>', content)

    def test_configure_pvr_instance_serializes_fresh_create(self):
        """Concurrent fresh-create calls must not write the same new file in parallel."""
        import builtins

        real_open = builtins.open
        active_writers = [0]
        concurrent_write_detected = []
        active_lock = threading.Lock()

        class _TrackingFile(object):
            def __init__(self, fh):
                self._fh = fh

            def __enter__(self):
                self._fh.__enter__()
                return self

            def __exit__(self, exc_type, exc_val, exc_tb):
                try:
                    return self._fh.__exit__(exc_type, exc_val, exc_tb)
                finally:
                    with active_lock:
                        active_writers[0] -= 1

            def __getattr__(self, item):
                return getattr(self._fh, item)

        def slow_open(path, mode="r", *args, **kwargs):
            if "w" in mode and os.path.basename(path).startswith("instance-settings-"):
                with active_lock:
                    active_writers[0] += 1
                    if active_writers[0] > 1:
                        concurrent_write_detected.append(path)
                threading.Event().wait(0.1)
                return _TrackingFile(real_open(path, mode, *args, **kwargs))
            return real_open(path, mode, *args, **kwargs)

        errors = []
        start = threading.Barrier(2)

        def worker(pvr_data):
            try:
                start.wait()
                self._configure_pvr_instance(pvr_data, genres_path=None)
            except BaseException as exc:
                errors.append(exc)

        with tempfile.TemporaryDirectory() as pvr_data:
            with patch("builtins.open", side_effect=slow_open):
                t1 = threading.Thread(target=worker, args=(pvr_data,))
                t2 = threading.Thread(target=worker, args=(pvr_data,))
                t1.start()
                t2.start()
                t1.join()
                t2.join()

            created = [
                os.path.join(pvr_data, name)
                for name in os.listdir(pvr_data)
                if name.startswith("instance-settings-") and name.endswith(".xml")
            ]
            self.assertEqual(errors, [])
            self.assertEqual(len(created), 1)
            self.assertEqual(concurrent_write_detected, [])
