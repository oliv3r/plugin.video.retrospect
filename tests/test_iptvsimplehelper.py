# SPDX-License-Identifier: GPL-3.0-or-later

import os
import tempfile
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

os.environ.setdefault("KODI_INTERACTIVE", "0")
os.environ.setdefault("KODI_HOME", "tests/home")

# Canonical upstream reference — "Piers" is the next Kodi release codename (= master/main).
# https://github.com/kodi-pvr/pvr.iptvsimple/blob/Piers/pvr.iptvsimple/resources/instance-settings.xml
_UPSTREAM_PVR_RAW_URL = (
    "https://raw.githubusercontent.com/kodi-pvr/pvr.iptvsimple"
    "/Piers/pvr.iptvsimple/resources/instance-settings.xml"
)


def setUpModule() -> None:
    from resources.lib.logger import Logger
    Logger.create_logger(None, "test_iptvsimplehelper", min_log_level=0)
    from resources.lib.textures import TextureHandler
    from resources.lib.textures.local import Local
    TextureHandler._TextureHandler__TextureHandler = Local(Logger.instance())  # type: ignore[attr-defined]


def tearDownModule() -> None:
    from resources.lib.addonsettings import AddonSettings
    AddonSettings.clear_cached_addon_settings_object()
    from resources.lib.logger import Logger
    if Logger.exists():
        Logger.instance().close_log()


class TestIptvSimpleHelper(unittest.TestCase):
    """Tests for the IPTV Simple helper functions."""

    _TEMPLATE_M3U_PATH = ""
    _TEMPLATE_EPG_PATH = ""

    _IPTV_MANAGER_SETUP_OVERRIDES = {
        "m3uPathType": "0",
        "m3uRefreshMode": "1",
        "m3uRefreshIntervalMins": "30",
        "epgPathType": "0",
        "epgCache": "true",
        "epgTimeShift": "0",
        "logoPathType": "0",
        "logoPath": "/",
        "catchupEnabled": "true",
        "allChannelsCatchupMode": "1",
        "catchupOnlyOnFinishedProgrammes": "false",
        "providerMappingFile": "",
        "retrospect_managed_channel": "",
        "retrospect_managed_version": "",
        "retrospect_managed_last_modification_time": "",
    }


    @staticmethod
    def _get_template_settings() -> Dict[str, str]:
        template_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "resources",
            "data",
            "iptv-instance-template.xml"
        )
        root = ET.parse(template_path).getroot()
        return {setting.get("id"): setting.text or "" for setting in root.findall("./setting")}  # type: ignore[misc]


    @classmethod
    def _flatten_upstream_instance_defaults(cls, xml_content: str) -> Dict[str, str]:
        root = ET.fromstring(xml_content)
        settings = {
            "kodi_addon_instance_name": "Retrospect",
            "kodi_addon_instance_enabled": "true",
        }
        for setting in root.iter("setting"):
            setting_id = setting.get("id")
            if not setting_id or setting_id in settings:
                continue
            settings[setting_id] = setting.findtext("default") or ""

        settings.update(cls._IPTV_MANAGER_SETUP_OVERRIDES)
        return settings


    @staticmethod
    def _set_xml_setting(*args: Any, **kwargs: Any) -> str:
        from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
        return IptvSimpleHelper._set_xml_setting(*args, **kwargs)


    @staticmethod
    def _install_channel_genres_file(*args: Any, **kwargs: Any) -> Optional[str]:
        from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
        return IptvSimpleHelper()._install_channel_genres_file(*args, **kwargs)


    @staticmethod
    def _configure_pvr_instances(pvr_data: str, channels: list) -> None:
        from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
        return IptvSimpleHelper()._configure_pvr_instances(pvr_data, channels)


    @staticmethod
    def _make_channel(path: str, channel_id: str = "channel.nos.nos2010.uzgjson",
                      name: str = "NPO Start") -> SimpleNamespace:
        return SimpleNamespace(id=channel_id, channelName=name, path=path)


    @staticmethod
    def _read_file(path: str) -> str:
        with open(path, encoding="utf-8") as fh:
            return fh.read()


    def test_set_xml_setting_updates_existing(self) -> None:
        content = '<settings version="2">\n    <setting id="foo">old</setting>\n</settings>'
        result = self._set_xml_setting(content, "foo", "new")
        self.assertIn('<setting id="foo">new</setting>', result)
        self.assertNotIn("old", result)


    def test_set_xml_setting_inserts_missing(self) -> None:
        content = '<settings version="2">\n</settings>'
        result = self._set_xml_setting(content, "bar", "baz")
        self.assertIn('<setting id="bar">baz</setting>', result)
        self.assertIn("</settings>", result)


    def test_set_xml_setting_strips_default_attr(self) -> None:
        content = ('<settings version="2">\n'
                   '    <setting id="foo" default="true">old</setting>\n'
                   '</settings>')
        result = self._set_xml_setting(content, "foo", "new")
        self.assertIn('<setting id="foo">new</setting>', result)
        self.assertNotIn('default="true"', result)


    def test_set_xml_setting_escapes_xml_special_chars(self) -> None:
        content = '<settings version="2">\n</settings>'
        result = self._set_xml_setting(content, "genresPath", "/tmp/Drama & Comedy <test>")
        root = ET.fromstring(result)
        self.assertEqual(
            root.find('./setting[@id="genresPath"]').text, "/tmp/Drama & Comedy <test>")  # type: ignore[union-attr]


    def test_iptv_instance_template_matches_upstream_defaults_and_overrides(self) -> None:
        """
        Track the shipped IPTV Simple baseline against upstream defaults.

        Source snapshot:
        ``kodi-pvr/pvr.iptvsimple`` branch ``Piers``
        ``pvr.iptvsimple/resources/instance-settings.xml``
        """

        template_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "resources",
            "data",
            "iptv-instance-template.xml"
        )
        root = ET.parse(template_path).getroot()
        actual = {setting.get("id"): setting.text or "" for setting in root.findall("./setting")}
        self.assertEqual(len(actual), 84)

        expected = {
            "kodi_addon_instance_name": "Retrospect",
            "kodi_addon_instance_enabled": "true",
            "m3uPathType": "0",
            "m3uPath": "",
            "m3uUrl": "",
            "m3uRefreshMode": "1",
            "m3uRefreshIntervalMins": "30",
            "providerMappingFile": "",
            "tvGroupMode": "0",
            "customTvGroupsFile": (
                "special://userdata/addon_data/pvr.iptvsimple/channelGroups/customTVGroups-example.xml"
            ),
            "epgPathType": "0",
            "epgPath": "",
            "epgCache": "true",
            "epgTimeShift": "0",
            "epgIgnoreCaseForChannelIds": "true",
            "genresPath": "special://userdata/addon_data/pvr.iptvsimple/genres/genreTextMappings/genres.xml",
            "logoPathType": "0",
            "logoPath": "/",
            "logoFromEpg": "1",
            "mediaEnabled": "true",
            "ffmpegdirectSettings": "",
            "catchupEnabled": "true",
            "catchupDays": "5",
            "allChannelsCatchupMode": "1",
            "catchupOnlyOnFinishedProgrammes": "false",
            "defaultMimeType": "",
        }
        for setting_id, value in expected.items():
            self.assertEqual(actual[setting_id], value)


    def test_iptv_instance_template_matches_upstream_source_file(self) -> None:
        """
        Track ``instance-settings.xml`` from the upstream Piers branch on GitHub.

        Piers is the current Kodi release codename (= master/main for pvr.iptvsimple).
        Skipped when the network is unavailable.
        """

        try:
            with urllib.request.urlopen(_UPSTREAM_PVR_RAW_URL, timeout=10) as response:
                xml_content = response.read().decode("utf-8")
        except (urllib.error.URLError, OSError):
            self.skipTest("Network unavailable — cannot fetch upstream instance-settings.xml")

        actual = self._get_template_settings()
        expected = self._flatten_upstream_instance_defaults(xml_content)
        self.assertEqual(actual, expected)


    def test_install_channel_genres_file_copies_source_xml(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            channel_dir = os.path.join(tmp, "channels", "channel.nos", "nos2010")
            os.makedirs(channel_dir)
            genre_file = os.path.join(channel_dir, "genres.xml")
            with open(genre_file, "w", encoding="utf-8") as fh:
                fh.write(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<genres>\n'
                    '  <genre genreId="0x10">Drama &amp; Comedy &lt;Live&gt;</genre>\n'
                    '</genres>\n'
                )

            channel = self._make_channel(channel_dir)
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                copied = self._install_channel_genres_file(channel)
                self.assertEqual(
                    copied,
                    os.path.join(profile_dir, "genres-channel.nos.nos2010.uzgjson.xml")
                )
                root = ET.parse(copied).getroot()  # type: ignore[arg-type]
                self.assertEqual(
                    root.find('./genre[@genreId="0x10"]').text,  # type: ignore[union-attr]
                    "Drama & Comedy <Live>"
                )



    def test_configure_pvr_instances_missing_dir_is_noop(self) -> None:
        self._configure_pvr_instances("/nonexistent/pvr_data_path_xyz", [])


    def test_configure_pvr_instances_updates_existing_managed_file_by_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            channel_dir = os.path.join(tmp, "channels", "channel.nos", "nos2010")
            pvr_data = os.path.join(tmp, "pvr")
            os.makedirs(profile_dir)
            os.makedirs(channel_dir)
            os.makedirs(pvr_data)

            owned = os.path.join(pvr_data, "instance-settings-1.xml")
            with open(owned, "w", encoding="utf-8") as fh:
                fh.write('<?xml version="1.0"?>\n<settings version="2">\n'
                         '    <setting id="retrospect_managed_channel">channel.nos.nos2010.uzgjson</setting>\n'
                         '    <setting id="kodi_addon_instance_name">Custom label</setting>\n'
                         '</settings>\n')
            other = os.path.join(pvr_data, "instance-settings-2.xml")
            with open(other, "w", encoding="utf-8") as fh:
                fh.write('<?xml version="1.0"?>\n<settings version="2">\n</settings>\n')

            channel = self._make_channel(channel_dir)
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                self._configure_pvr_instances(pvr_data, [channel])

            owned_content = self._read_file(owned)
            other_content = self._read_file(other)

        root = ET.fromstring(owned_content)
        self.assertEqual(
            root.find('./setting[@id="retrospect_managed_channel"]').text,  # type: ignore[union-attr]
            "channel.nos.nos2010.uzgjson"
        )
        self.assertEqual(
            root.find('./setting[@id="kodi_addon_instance_name"]').text,  # type: ignore[union-attr]
            "Custom label"
        )
        self.assertEqual(root.find('./setting[@id="oneTvGroup"]').text, "NPO Start")  # type: ignore[union-attr]
        self.assertEqual(root.find('./setting[@id="useEpgGenreText"]').text, "false")  # type: ignore[union-attr]
        self.assertIsNone(root.find('./setting[@id="genresPath"]'))
        self.assertEqual(
            root.find('./setting[@id="m3uPath"]').text,  # type: ignore[union-attr]
            os.path.join(profile_dir, "iptv", "playlist-channel.nos.nos2010.uzgjson.m3u8"))
        self.assertEqual(
            root.find('./setting[@id="epgPath"]').text,  # type: ignore[union-attr]
            os.path.join(profile_dir, "iptv", "epg-channel.nos.nos2010.uzgjson.xml"))
        self.assertNotIn("catchupEnabled", other_content)


    def test_configure_pvr_instances_creates_new_file_from_installed_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            channel_dir = os.path.join(tmp, "channels", "channel.nos", "nos2010")
            pvr_data = os.path.join(tmp, "pvr")
            os.makedirs(channel_dir)
            os.makedirs(pvr_data)

            channel = self._make_channel(channel_dir)
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                self._configure_pvr_instances(pvr_data, [channel])

            created = os.path.join(pvr_data, "instance-settings-1.xml")
            self.assertTrue(os.path.exists(created))
            created_root = ET.parse(created).getroot()
            self.assertEqual(
                created_root.find('./setting[@id="kodi_addon_instance_name"]').text,  # type: ignore[union-attr]
                "Retrospect - NPO Start"
            )
            self.assertEqual(
                created_root.find('./setting[@id="m3uPath"]').text,  # type: ignore[union-attr]
                os.path.join(profile_dir, "iptv", "playlist-channel.nos.nos2010.uzgjson.m3u8"))
            self.assertEqual(
                created_root.find('./setting[@id="epgPath"]').text,  # type: ignore[union-attr]
                os.path.join(profile_dir, "iptv", "epg-channel.nos.nos2010.uzgjson.xml"))
            self.assertEqual(created_root.find('./setting[@id="oneTvGroup"]').text, "NPO Start")  # type: ignore[union-attr]
            self.assertFalse(os.path.exists(profile_dir))


    def test_configure_pvr_instances_creates_second_file_from_installed_template(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            first_channel_dir = os.path.join(tmp, "channels", "channel.nos", "nos2010")
            second_channel_dir = os.path.join(tmp, "channels", "channel.be", "vrtmax")
            pvr_data = os.path.join(tmp, "pvr")
            os.makedirs(profile_dir)
            os.makedirs(first_channel_dir)
            os.makedirs(second_channel_dir)
            os.makedirs(pvr_data)

            managed = os.path.join(pvr_data, "instance-settings-1.xml")
            with open(managed, "w", encoding="utf-8") as fh:
                fh.write('<?xml version="1.0"?>\n<settings version="2">\n'
                         '    <setting id="retrospect_managed_channel">channel.nos.nos2010.uzgjson</setting>\n'
                         '    <setting id="m3uPathType">0</setting>\n'
                         '    <setting id="epgPathType">0</setting>\n'
                         '</settings>\n')

            first_channel = self._make_channel(first_channel_dir)
            second_channel = self._make_channel(
                second_channel_dir,
                channel_id="channel.be.vrtmax.bejson",
                name="VRT MAX"
            )

            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                self._configure_pvr_instances(pvr_data, [first_channel, second_channel])

            created = os.path.join(pvr_data, "instance-settings-2.xml")
            self.assertTrue(os.path.exists(created))
            created_content = self._read_file(created)
            created_root = ET.fromstring(created_content)
            self.assertEqual(
                created_root.find('./setting[@id="retrospect_managed_channel"]').text,  # type: ignore[union-attr]
                "channel.be.vrtmax.bejson"
            )
            self.assertNotIn("channel.nos.nos2010.uzgjson", created_content)
            self.assertEqual(
                created_root.find('./setting[@id="m3uPath"]').text,  # type: ignore[union-attr]
                os.path.join(profile_dir, "iptv", "playlist-channel.be.vrtmax.bejson.m3u8"))
            self.assertEqual(
                created_root.find('./setting[@id="epgPath"]').text,  # type: ignore[union-attr]
                os.path.join(profile_dir, "iptv", "epg-channel.be.vrtmax.bejson.xml"))
            self.assertEqual(
                created_root.find('./setting[@id="kodi_addon_instance_name"]').text,  # type: ignore[union-attr]
                "Retrospect - VRT MAX"
            )
            self.assertEqual(created_root.find('./setting[@id="oneTvGroup"]').text, "VRT MAX")  # type: ignore[union-attr]


    def test_configure_pvr_instances_disables_inactive_managed_instances(self) -> None:
        """Managed instances for channels no longer active are disabled, not deleted."""
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            pvr_data = os.path.join(tmp, "pvr")
            os.makedirs(profile_dir)
            os.makedirs(pvr_data)

            managed = os.path.join(pvr_data, "instance-settings-1.xml")
            with open(managed, "w", encoding="utf-8") as fh:
                fh.write('<?xml version="1.0"?>\n<settings version="2">\n'
                         '    <setting id="retrospect_managed_channel">channel.nos.nos2010.uzgjson</setting>\n'
                         '    <setting id="kodi_addon_instance_enabled">true</setting>\n'
                         '</settings>\n')

            genre_target = os.path.join(profile_dir, "genres-channel.nos.nos2010.uzgjson.xml")
            with open(genre_target, "w", encoding="utf-8") as fh:
                fh.write("genre")

            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                self._configure_pvr_instances(pvr_data, [])

            self.assertTrue(os.path.exists(managed))
            self.assertTrue(os.path.exists(genre_target))
            import xml.etree.ElementTree as ET
            root = ET.parse(managed).getroot()
            self.assertEqual(
                root.find('./setting[@id="kodi_addon_instance_enabled"]').text, "false")


    # -- __init__ -----------------------------------------------------------

    # -- _get_iptv_channels -------------------------------------------------

    def test_get_iptv_channels_filters_disabled_and_invisible(self) -> None:
        """Default call excludes channels that are disabled or invisible."""
        from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper

        def _ch(has_iptv: bool, visible: bool, enabled: bool) -> MagicMock:
            m = MagicMock()
            m.has_iptv = has_iptv
            m.visible = visible
            m.enabled = enabled
            return m

        channels = [
            _ch(True, True, True),    # included
            _ch(True, True, False),   # disabled → excluded
            _ch(True, False, True),   # invisible → excluded
            _ch(False, True, True),   # no IPTV → excluded
        ]

        with patch("resources.lib.helpers.iptvsimplehelper.ChannelIndex") as mock_ci:
            mock_ci.get_register.return_value.get_channels.return_value = channels
            result = IptvSimpleHelper()._get_iptv_channels()

        self.assertEqual(result, [channels[0]])


    def test_get_iptv_channels_include_disabled_returns_all_iptv(self) -> None:
        """include_disabled=True returns all IPTV channels regardless of visible/enabled."""
        from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper

        def _ch(has_iptv: bool, visible: bool = True, enabled: bool = True) -> MagicMock:
            m = MagicMock()
            m.has_iptv = has_iptv
            m.visible = visible
            m.enabled = enabled
            return m

        channels = [
            _ch(True, True, True),
            _ch(True, True, False),
            _ch(True, False, True),
            _ch(False, True, True),
        ]

        with patch("resources.lib.helpers.iptvsimplehelper.ChannelIndex") as mock_ci:
            mock_ci.get_register.return_value.get_channels.return_value = channels
            result = IptvSimpleHelper()._get_iptv_channels(include_disabled=True)

        self.assertEqual(len(result), 3)
        self.assertNotIn(channels[3], result)


    # -- _sync_channel_genres_file (error paths) ----------------------------

    def test_install_channel_genres_file_unchanged_content_not_rewritten(self) -> None:
        """Target with the same content as source is not rewritten."""
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            channel_dir = os.path.join(tmp, "channels", "channel.nos", "nos2010")
            os.makedirs(profile_dir)
            os.makedirs(channel_dir)
            content = "<genres/>\n"
            source = os.path.join(channel_dir, "genres.xml")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write(content)
            target = os.path.join(profile_dir, "genres-channel.nos.nos2010.uzgjson.xml")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(content)

            channel = self._make_channel(channel_dir)
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                result = self._install_channel_genres_file(channel)

            self.assertEqual(result, target)
            with open(target, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), content)



    def test_install_channel_genres_file_oserror_reading_source(self) -> None:
        """OSError when reading the source file is handled; None is returned."""
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            channel_dir = os.path.join(tmp, "channels", "channel.nos", "nos2010")
            os.makedirs(profile_dir)
            os.makedirs(channel_dir)
            source = os.path.join(channel_dir, "genres.xml")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("<genres/>")
            os.chmod(source, 0o000)

            try:
                channel = self._make_channel(channel_dir)
                with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                    result = self._install_channel_genres_file(channel)
            finally:
                os.chmod(source, 0o644)

        self.assertIsNone(result)


    def test_install_channel_genres_file_makedirs_failure(self) -> None:
        """OSError from makedirs when profileDir is absent is handled; None is returned."""
        with tempfile.TemporaryDirectory() as tmp:
            channel_dir = os.path.join(tmp, "channels", "channel.nos", "nos2010")
            os.makedirs(channel_dir)
            source = os.path.join(channel_dir, "genres.xml")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("<genres/>")
            profile_dir = os.path.join(tmp, "nonexistent_profile")

            channel = self._make_channel(channel_dir)
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir), \
                    patch("resources.lib.helpers.iptvsimplehelper.os.makedirs",
                          side_effect=OSError("no space left")):
                result = self._install_channel_genres_file(channel)

        self.assertIsNone(result)


    def test_install_channel_genres_file_oserror_writing_target(self) -> None:
        """OSError when writing the target file is handled; None is returned."""
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            channel_dir = os.path.join(tmp, "channels", "channel.nos", "nos2010")
            os.makedirs(profile_dir)
            os.makedirs(channel_dir)
            os.chmod(profile_dir, 0o555)  # read-only: write will fail

            source = os.path.join(channel_dir, "genres.xml")
            with open(source, "w", encoding="utf-8") as fh:
                fh.write("<genres/>")

            try:
                channel = self._make_channel(channel_dir)
                with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                    result = self._install_channel_genres_file(channel)
            finally:
                os.chmod(profile_dir, 0o755)

        self.assertIsNone(result)


    # -- _get_xml_settings --------------------------------------------------

    def test_get_xml_settings_invalid_xml_returns_empty(self) -> None:
        """Malformed XML input to _get_xml_settings returns an empty dict."""
        from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
        result = IptvSimpleHelper._get_xml_settings("not valid < xml >")
        self.assertEqual(result, {})


    # -- _load_instance_settings --------------------------------------------

    def test_load_instance_settings_missing_file_returns_none(self) -> None:
        """Missing template file causes _load_instance_settings to return None."""
        from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
        with patch("resources.lib.helpers.iptvsimplehelper.Config.rootDir", "/nonexistent_xyz"):
            result = IptvSimpleHelper()._load_instance_settings()
        self.assertIsNone(result)


    # -- _disable_managed_pvr_instance --------------------------------------

    def test_disable_managed_pvr_instance_sets_enabled_false(self) -> None:
        """SUCCESS → instance file remains on disk with kodi_addon_instance_enabled=false."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "instance-settings-1.xml")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write('<?xml version="1.0"?>\n<settings version="2">\n'
                         '    <setting id="retrospect_managed_channel">ch.id</setting>\n'
                         '    <setting id="kodi_addon_instance_enabled">true</setting>\n'
                         '</settings>\n')
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            IptvSimpleHelper()._disable_managed_pvr_instance(path, "ch.id")
            self.assertTrue(os.path.exists(path))
            import xml.etree.ElementTree as ET
            root = ET.parse(path).getroot()
            self.assertEqual(
                root.find('./setting[@id="kodi_addon_instance_enabled"]').text, "false")

    def test_disable_managed_pvr_instance_wrong_channel_id_skips(self) -> None:
        """SUCCESS → file is untouched when channel id does not match."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "instance-settings-1.xml")
            original = ('<?xml version="1.0"?>\n<settings version="2">\n'
                        '    <setting id="retrospect_managed_channel">other.id</setting>\n'
                        '</settings>\n')
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(original)
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            IptvSimpleHelper()._disable_managed_pvr_instance(path, "ch.id")
            with open(path, encoding="utf-8") as fh:
                self.assertEqual(fh.read(), original)

    def test_disable_managed_pvr_instance_oserror_reading_handled(self) -> None:
        """OSError when reading the instance file is handled without raising."""
        from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
        with patch("resources.lib.helpers.iptvsimplehelper.IptvSimpleHelper._get_xml_setting",
                   return_value="ch.id"), \
             patch("builtins.open", side_effect=OSError("permission denied")):
            IptvSimpleHelper()._disable_managed_pvr_instance("/nonexistent.xml", "ch.id")


    # -- _configure_pvr_instances (error paths) ------------------------------

    def test_configure_pvr_instances_skips_channel_when_template_is_none(self) -> None:
        """Channel is silently skipped when _load_instance_settings returns None."""
        with tempfile.TemporaryDirectory() as tmp:
            pvr_data = os.path.join(tmp, "pvr")
            channel_dir = os.path.join(tmp, "channel")
            os.makedirs(pvr_data)
            os.makedirs(channel_dir)
            channel = self._make_channel(channel_dir)

            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch.object(IptvSimpleHelper, "_load_instance_settings", return_value=None):
                self._configure_pvr_instances(pvr_data, [channel])

            self.assertEqual(os.listdir(pvr_data), [])


    def test_configure_pvr_instances_oserror_reading_instance_skips_it(self) -> None:
        """An unreadable instance-settings file is silently skipped."""
        with tempfile.TemporaryDirectory() as tmp:
            pvr_data = os.path.join(tmp, "pvr")
            os.makedirs(pvr_data)
            unreadable = os.path.join(pvr_data, "instance-settings-1.xml")
            with open(unreadable, "w", encoding="utf-8") as fh:
                fh.write('<settings/>')
            os.chmod(unreadable, 0o000)
            try:
                self._configure_pvr_instances(pvr_data, [])
            finally:
                os.chmod(unreadable, 0o644)


    def test_configure_pvr_instances_oserror_writing_instance_handled(self) -> None:
        """OSError when writing a new instance file is handled without raising."""
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            channel_dir = os.path.join(tmp, "channel")
            pvr_data = os.path.join(tmp, "pvr")
            os.makedirs(channel_dir)
            os.makedirs(pvr_data)
            os.chmod(pvr_data, 0o555)  # read-only: writing new files will fail

            channel = self._make_channel(channel_dir)
            try:
                with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                    self._configure_pvr_instances(pvr_data, [channel])
            finally:
                os.chmod(pvr_data, 0o755)

            self.assertEqual(os.listdir(pvr_data), [])


    # -- setup_iptvsimple ---------------------------------------------------

    def test_setup_iptvsimple_not_installed_skips(self) -> None:
        """setup_iptvsimple() does nothing when pvr.iptvsimple is not installed."""
        from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
        with patch("resources.lib.helpers.iptvsimplehelper.xbmcaddon.Addon",
                   side_effect=RuntimeError("not installed")), \
                patch.object(IptvSimpleHelper, "_configure_pvr_instances") as mock_configure:
            IptvSimpleHelper().setup_iptvsimple()
        mock_configure.assert_not_called()


    def test_setup_iptvsimple_installed_configures_instances(self) -> None:
        """setup_iptvsimple() calls _configure_pvr_instances with the pvr addon profile path."""
        from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
        mock_addon = MagicMock()
        mock_addon.getAddonInfo.return_value = "special://profile/pvr.iptvsimple"
        with patch("resources.lib.helpers.iptvsimplehelper.xbmcaddon.Addon",
                   return_value=mock_addon), \
                patch("resources.lib.helpers.iptvsimplehelper.xbmcvfs.translatePath",
                      return_value="/tmp/pvr_data"), \
                patch.object(IptvSimpleHelper, "_get_iptv_channels", return_value=[]), \
                patch.object(IptvSimpleHelper, "_configure_pvr_instances") as mock_configure:
            IptvSimpleHelper().setup_iptvsimple()
        mock_configure.assert_called_once_with("/tmp/pvr_data", [])


class TestIptvSimpleHelperWriters(unittest.TestCase):
    """Tests for IptvSimpleHelper playlist and EPG file writers."""

    _SAMPLE_STREAMS = [
        {"id": "npo1", "name": "NPO 1", "logo": "https://example.com/npo1.png",
         "group": "NLZIET", "stream": "plugin://plugin.video.retrospect/?action=play&pickle=abc--123"},
        {"id": "rtl4", "name": "RTL 4", "logo": "", "group": "NLZIET",
         "stream": "plugin://plugin.video.retrospect/?action=play&pickle=abc--456"},
    ]

    _SAMPLE_EPG = {
        "npo1": [
            {"start": "2026-02-21T20:00:00+01:00", "stop": "2026-02-21T20:30:00+01:00",
             "title": "News", "image": "https://example.com/news.jpg"},
            {"start": "2026-02-21T20:30:00+01:00", "stop": "2026-02-21T21:30:00+01:00",
             "title": "Drama"},
        ]
    }

    def test_write_playlist_creates_m3u(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", tmp):
                IptvSimpleHelper().write_playlist(self._SAMPLE_STREAMS, "test-ch")
            path = os.path.join(tmp, "iptv", "playlist-test-ch.m3u8")
            self.assertTrue(os.path.exists(path))
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
        self.assertTrue(content.startswith("#EXTM3U"))
        self.assertIn('tvg-id="npo1"', content)
        self.assertIn('tvg-name="NPO 1"', content)
        self.assertIn('tvg-logo="https://example.com/npo1.png"', content)
        self.assertIn('group-title="NLZIET"', content)
        self.assertIn("plugin://plugin.video.retrospect/", content)

    def test_write_epg_creates_xmltv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", tmp):
                IptvSimpleHelper().write_epg(self._SAMPLE_EPG, "test-ch", streams=self._SAMPLE_STREAMS)
            path = os.path.join(tmp, "iptv", "epg-test-ch.xml")
            self.assertTrue(os.path.exists(path))
            root = ET.parse(path).getroot()
        channels = root.findall("./channel")
        self.assertEqual(len(channels), 1)
        self.assertEqual(channels[0].get("id"), "npo1")
        self.assertEqual(channels[0].findtext("display-name"), "NPO 1")
        programmes = root.findall("./programme")
        self.assertEqual(len(programmes), 2)
        p = programmes[0]
        self.assertEqual(p.findtext("title"), "News")
        self.assertEqual(p.get("start"), "20260221200000 +0100")
        self.assertEqual(p.get("stop"), "20260221203000 +0100")

    def test_write_epg_escapes_special_chars(self) -> None:
        epg = {"ch1": [{"start": "2026-01-01T00:00:00+00:00", "stop": "2026-01-01T01:00:00+00:00",
                         "title": "News & Drama <Live>"}]}
        with tempfile.TemporaryDirectory() as tmp:
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", tmp):
                IptvSimpleHelper().write_epg(epg, "ch1")
            path = os.path.join(tmp, "iptv", "epg-ch1.xml")
            root = ET.parse(path).getroot()
        title = root.find("./programme/title").text  # type: ignore[union-attr]
        self.assertEqual(title, "News & Drama <Live>")

    def test_write_playlist_atomic_write(self) -> None:
        """write_playlist uses .tmp file then replaces."""
        with tempfile.TemporaryDirectory() as tmp:
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", tmp):
                IptvSimpleHelper().write_playlist(self._SAMPLE_STREAMS, "test-ch")
            path = os.path.join(tmp, "iptv", "playlist-test-ch.m3u8")
            tmp_path = path + ".tmp"
            # After successful write, .tmp should not exist
            self.assertFalse(os.path.exists(tmp_path))
            self.assertTrue(os.path.exists(path))

    def test_build_pvr_instance_content_sets_per_channel_paths(self) -> None:
        """_build_pvr_instance_content sets per-channel m3uPath and epgPath."""
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            channel_dir = os.path.join(tmp, "channels", "channel.nos", "nos2010")
            os.makedirs(channel_dir)
            channel = SimpleNamespace(
                id="channel.nos.nos2010.uzgjson",
                channelName="NPO Start",
                path=channel_dir
            )
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                template = IptvSimpleHelper()._load_instance_settings()
                content = IptvSimpleHelper()._build_pvr_instance_content(template, channel, None)  # type: ignore[arg-type]
            root = ET.fromstring(content)
            expected_m3u = os.path.join(
                profile_dir, "iptv", "playlist-channel.nos.nos2010.uzgjson.m3u8")
            expected_epg = os.path.join(
                profile_dir, "iptv", "epg-channel.nos.nos2010.uzgjson.xml")
            self.assertEqual(root.find('./setting[@id="m3uPath"]').text, expected_m3u)  # type: ignore[union-attr]
            self.assertEqual(root.find('./setting[@id="epgPath"]').text, expected_epg)  # type: ignore[union-attr]


    def test_write_playlist_creates_parent_dir(self) -> None:
        """write_playlist creates missing iptv sub-directory."""
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            os.makedirs(profile_dir)
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                IptvSimpleHelper().write_playlist(self._SAMPLE_STREAMS, "test-ch")
            path = os.path.join(profile_dir, "iptv", "playlist-test-ch.m3u8")
            self.assertTrue(os.path.exists(path))


    def test_write_epg_creates_parent_dir(self) -> None:
        """write_epg creates missing iptv sub-directory."""
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            os.makedirs(profile_dir)
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                IptvSimpleHelper().write_epg(self._SAMPLE_EPG, "test-ch")
            path = os.path.join(profile_dir, "iptv", "epg-test-ch.xml")
            self.assertTrue(os.path.exists(path))


    def test_write_epg_includes_optional_fields_description_genre_stream(self) -> None:
        """write_epg emits <desc>, <category>, and <stream> when provided."""
        epg = {"ch1": [{
            "start": "2026-01-01T20:00:00+00:00",
            "stop": "2026-01-01T21:00:00+00:00",
            "title": "The Show",
            "description": "A great show",
            "genre": "Drama",
            "stream": "https://cdn.example.com/recording.mp4",
        }]}
        with tempfile.TemporaryDirectory() as tmp:
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", tmp):
                IptvSimpleHelper().write_epg(epg, "ch1")
            path = os.path.join(tmp, "iptv", "epg-ch1.xml")
            root = ET.parse(path).getroot()
        prog = root.find("./programme")
        self.assertIsNotNone(prog)
        self.assertEqual(prog.findtext("desc"), "A great show")  # type: ignore[union-attr]
        self.assertEqual(prog.findtext("category"), "Drama")  # type: ignore[union-attr]
        self.assertEqual(prog.findtext("stream"), "https://cdn.example.com/recording.mp4")  # type: ignore[union-attr]


    def test_write_epg_invalid_timestamp_passes_through(self) -> None:
        """Non-ISO start/stop timestamps are passed through unchanged."""
        epg = {"ch1": [{"start": "not-a-date", "stop": "also-not", "title": "Show"}]}
        with tempfile.TemporaryDirectory() as tmp:
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", tmp):
                IptvSimpleHelper().write_epg(epg, "ch1")
            path = os.path.join(tmp, "iptv", "epg-ch1.xml")
            root = ET.parse(path).getroot()
        prog = root.find("./programme")
        self.assertIsNotNone(prog)
        self.assertEqual(prog.get("start"), "not-a-date")  # type: ignore[union-attr]
        self.assertEqual(prog.get("stop"), "also-not")  # type: ignore[union-attr]


    def test_write_epg_includes_date_tag(self) -> None:
        """write_epg emits <date> when the 'date' key is present in a programme dict."""

        epg: Dict[str, Any] = {"ch1": [{
            "start": "2026-01-01T20:00:00+00:00",
            "stop": "2026-01-01T21:00:00+00:00",
            "title": "The Show",
            "date": "20260101",
        }]}
        with tempfile.TemporaryDirectory() as tmp:
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", tmp):
                IptvSimpleHelper().write_epg(epg, "ch1")
            path = os.path.join(tmp, "iptv", "epg-ch1.xml")
            root = ET.parse(path).getroot()
        prog = root.find("./programme")
        self.assertIsNotNone(prog)
        self.assertEqual(prog.findtext("date"), "20260101")  # type: ignore[union-attr]


    def test_write_epg_omits_date_tag_when_absent(self) -> None:
        """write_epg does not emit <date> when the 'date' key is missing."""

        epg: Dict[str, Any] = {"ch1": [{
            "start": "2026-01-01T20:00:00+00:00",
            "stop": "2026-01-01T21:00:00+00:00",
            "title": "The Show",
        }]}
        with tempfile.TemporaryDirectory() as tmp:
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", tmp):
                IptvSimpleHelper().write_epg(epg, "ch1")
            path = os.path.join(tmp, "iptv", "epg-ch1.xml")
            root = ET.parse(path).getroot()
        prog = root.find("./programme")
        self.assertIsNotNone(prog)
        self.assertIsNone(prog.find("date"))  # type: ignore[union-attr]


    def test_build_pvr_instance_content_leaves_provider_mapping_disabled(self) -> None:
        """
        _build_pvr_instance_content leaves provider mapping at template defaults.
        """

        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            channel_dir = os.path.join(tmp, "channels", "channel.nos", "nos2010")
            os.makedirs(channel_dir)
            channel = SimpleNamespace(
                id="channel.nos.nos2010.uzgjson",
                channelName="NPO Start",
                path=channel_dir
            )
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                template = IptvSimpleHelper()._load_instance_settings()
                content = IptvSimpleHelper()._build_pvr_instance_content(template, channel, None)
            root = ET.fromstring(content)
            self.assertEqual(
                root.find('./setting[@id="enableProviderMappings"]').text, "false")
            self.assertEqual(
                root.find('./setting[@id="providerMappingFile"]').text or "", "")


    def test_enable_provider_mapping_sets_flag_and_path(self) -> None:
        """enable_provider_mapping flips enableProviderMappings and writes the per-channel path."""

        with tempfile.TemporaryDirectory() as tmp:
            pvr_data = os.path.join(tmp, "pvr_data")
            os.makedirs(pvr_data)
            profile_dir = os.path.join(tmp, "profile")
            channel_dir = os.path.join(tmp, "channels", "channel.nos", "nos2010")
            os.makedirs(channel_dir)
            channel = SimpleNamespace(
                id="channel.nos.nos2010.uzgjson",
                channelName="NPO Start",
                path=channel_dir
            )
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                template = IptvSimpleHelper()._load_instance_settings()
                instance_content = IptvSimpleHelper()._build_pvr_instance_content(
                    template, channel, None)
                expected_path = os.path.join(profile_dir, "iptv", f"providers-{channel.id}.xml")

            instance_path = os.path.join(pvr_data, "instance-settings-1.xml")
            with open(instance_path, "w", encoding="utf-8") as fh:
                fh.write(instance_content)

            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir), \
                    patch("resources.lib.helpers.iptvsimplehelper.xbmcaddon.Addon") as mock_addon_cls, \
                    patch("resources.lib.helpers.iptvsimplehelper.xbmcvfs.translatePath",
                          return_value=pvr_data):
                mock_addon_cls.return_value.getAddonInfo.return_value = pvr_data
                IptvSimpleHelper().enable_provider_mapping(channel.id)

            root = ET.parse(instance_path).getroot()
            self.assertEqual(
                root.find('./setting[@id="enableProviderMappings"]').text, "true")
            self.assertEqual(
                root.find('./setting[@id="providerMappingFile"]').text, expected_path)


    def test_enable_provider_mapping_idempotent(self) -> None:
        """enable_provider_mapping does not rewrite the file if already enabled."""

        with tempfile.TemporaryDirectory() as tmp:
            pvr_data = os.path.join(tmp, "pvr_data")
            os.makedirs(pvr_data)
            profile_dir = os.path.join(tmp, "profile")
            channel_dir = os.path.join(tmp, "channels", "channel.nos", "nos2010")
            os.makedirs(channel_dir)
            channel = SimpleNamespace(
                id="channel.nos.nos2010.uzgjson",
                channelName="NPO Start",
                path=channel_dir
            )
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                template = IptvSimpleHelper()._load_instance_settings()
                instance_content = IptvSimpleHelper()._build_pvr_instance_content(
                    template, channel, None)

            instance_path = os.path.join(pvr_data, "instance-settings-1.xml")
            with open(instance_path, "w", encoding="utf-8") as fh:
                fh.write(instance_content)

            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir), \
                    patch("resources.lib.helpers.iptvsimplehelper.xbmcaddon.Addon") as mock_addon_cls, \
                    patch("resources.lib.helpers.iptvsimplehelper.xbmcvfs.translatePath",
                          return_value=pvr_data):
                mock_addon_cls.return_value.getAddonInfo.return_value = pvr_data
                IptvSimpleHelper().enable_provider_mapping(channel.id)

            mtime_after_first = os.path.getmtime(instance_path)

            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir), \
                    patch("resources.lib.helpers.iptvsimplehelper.xbmcaddon.Addon") as mock_addon_cls, \
                    patch("resources.lib.helpers.iptvsimplehelper.xbmcvfs.translatePath",
                          return_value=pvr_data):
                mock_addon_cls.return_value.getAddonInfo.return_value = pvr_data
                IptvSimpleHelper().enable_provider_mapping(channel.id)

            self.assertEqual(os.path.getmtime(instance_path), mtime_after_first)


    def test_write_provider_mapping_creates_xml(self) -> None:
        """write_provider_mapping groups channel names by provider."""

        streams = [
            {"id": "npo1", "name": "NPO 1", "provider": "NPO",
             "logo": "", "group": "NOS", "stream": "url1"},
            {"id": "npo2", "name": "NPO 2", "provider": "NPO",
             "logo": "", "group": "NOS", "stream": "url2"},
            {"id": "rtl4", "name": "RTL 4", "provider": "RTL",
             "logo": "", "group": "NOS", "stream": "url3"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", tmp):
                IptvSimpleHelper().write_provider_mapping(streams, "test")
            output = os.path.join(tmp, "iptv", "providers-test.xml")
            self.assertTrue(os.path.isfile(output))
            root = ET.parse(output).getroot()
            self.assertEqual(root.tag, "providerMappings")
            providers = {el.get("provider"): [c.text for c in el]
                         for el in root.findall("providerMapping")}
            self.assertIn("NPO", providers)
            self.assertIn("RTL", providers)
            self.assertCountEqual(providers["NPO"], ["NPO 1", "NPO 2"])
            self.assertCountEqual(providers["RTL"], ["RTL 4"])


    def test_write_provider_mapping_skips_streams_without_provider(self) -> None:
        """Streams without a 'provider' key are silently ignored."""

        streams = [
            {"id": "x", "name": "Some Channel", "logo": "", "group": "G", "stream": "url"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", tmp):
                IptvSimpleHelper().write_provider_mapping(streams, "test")
            output = os.path.join(tmp, "iptv", "providers-test.xml")
            self.assertFalse(os.path.isfile(output))


    def test_write_provider_mapping_skips_empty_provider(self) -> None:
        """Streams with an empty provider string are ignored."""

        streams = [
            {"id": "x", "name": "Ch", "provider": "",
             "logo": "", "group": "G", "stream": "url"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", tmp):
                IptvSimpleHelper().write_provider_mapping(streams, "test")
            output = os.path.join(tmp, "iptv", "providers-test.xml")
            self.assertFalse(os.path.isfile(output))
