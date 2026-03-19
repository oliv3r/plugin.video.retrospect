# SPDX-License-Identifier: GPL-3.0-or-later

import os
import tempfile
import unittest
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import patch

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
    def _sync_channel_genres_file(*args: Any, **kwargs: Any) -> Optional[str]:
        from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
        return IptvSimpleHelper._sync_channel_genres_file(*args, **kwargs)


    @staticmethod
    def _configure_pvr_instances(pvr_data: str, channels: list) -> None:
        from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
        return IptvSimpleHelper.configure_pvr_instances(pvr_data, channels)


    @staticmethod
    def _make_channel(path: str, channel_id: str = "channel.nos.nos2010.uzgjson",
                      name: str = "NPO Start") -> SimpleNamespace:
        return SimpleNamespace(id=channel_id, channelName=name, path=path)


    @staticmethod
    def _iptv_manager_content() -> str:
        return ('<?xml version="1.0"?>\n<settings version="2">\n'
                '    <setting id="kodi_addon_instance_name">Living room IPTV</setting>\n'
                '    <setting id="m3uPath">special://profile/custom-playlist.m3u8</setting>\n'
                '    <setting id="epgPath">special://profile/custom-epg.xml</setting>\n'
                '    <setting id="epgCache">false</setting>\n'
                '    <!-- created by service.iptv.manager -->\n'
                '</settings>\n')


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

        Source snapshots:
        ``kodi-pvr/pvr.iptvsimple`` branch ``Piers``
        ``pvr.iptvsimple/resources/instance-settings.xml``
        ``service.iptv.manager/resources/lib/modules/iptvsimple.py::IptvSimple.setup()``
        """

        template_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "resources",
            "data",
            "iptv-instance-template.xml"
        )
        root = ET.parse(template_path).getroot()
        actual = {setting.get("id"): setting.text or "" for setting in root.findall("./setting")}
        self.assertEqual(len(actual), 81)

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
            self.assertEqual(actual.get(setting_id), value)


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


    def test_sync_channel_genres_file_copies_source_xml(self) -> None:
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
                copied = self._sync_channel_genres_file(channel)
                self.assertEqual(
                    copied,
                    os.path.join(profile_dir, "genres-channel.nos.nos2010.uzgjson.xml")
                )
                root = ET.parse(copied).getroot()  # type: ignore[arg-type]
                self.assertEqual(
                    root.find('./genre[@genreId="0x10"]').text,  # type: ignore[union-attr]
                    "Drama & Comedy <Live>"
                )


    def test_sync_channel_genres_file_removes_stale_target_when_source_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            channel_dir = os.path.join(tmp, "channels", "channel.nos", "nos2010")
            os.makedirs(profile_dir)
            os.makedirs(channel_dir)
            target = os.path.join(profile_dir, "genres-channel.nos.nos2010.uzgjson.xml")
            with open(target, "w", encoding="utf-8") as fh:
                fh.write("stale")

            channel = self._make_channel(channel_dir)
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                copied = self._sync_channel_genres_file(channel)
                self.assertIsNone(copied)
                self.assertFalse(os.path.exists(target))


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
                         '    <!-- Do not modfiy this line. '
                         'Retrospect managed channel: channel.nos.nos2010.uzgjson -->\n'
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
        self.assertIn("Retrospect managed channel: channel.nos.nos2010.uzgjson", owned_content)
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


    def test_configure_pvr_instances_reuses_iptv_manager_instance_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            channel_dir = os.path.join(tmp, "channels", "channel.nos", "nos2010")
            pvr_data = os.path.join(tmp, "pvr")
            os.makedirs(channel_dir)
            os.makedirs(pvr_data)

            with open(os.path.join(channel_dir, "genres.xml"), "w", encoding="utf-8") as fh:
                fh.write('<?xml version="1.0"?>\n<genres>\n</genres>\n')

            iptv_file = os.path.join(pvr_data, "instance-settings-1.xml")
            with open(iptv_file, "w", encoding="utf-8") as fh:
                fh.write(self._iptv_manager_content())

            channel = self._make_channel(channel_dir)
            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                self._configure_pvr_instances(pvr_data, [channel])

            content = self._read_file(iptv_file)
            self.assertEqual(sorted(os.listdir(pvr_data)), ["instance-settings-1.xml"])

        root = ET.fromstring(content)
        self.assertIn("Retrospect managed channel: channel.nos.nos2010.uzgjson", content)
        self.assertEqual(
            root.find('./setting[@id="kodi_addon_instance_name"]').text,  # type: ignore[union-attr]
            "Living room IPTV"
        )
        self.assertEqual(
            root.find('./setting[@id="m3uPath"]').text,  # type: ignore[union-attr]
            os.path.join(profile_dir, "iptv", "playlist-channel.nos.nos2010.uzgjson.m3u8"))
        self.assertEqual(
            root.find('./setting[@id="epgPath"]').text,  # type: ignore[union-attr]
            os.path.join(profile_dir, "iptv", "epg-channel.nos.nos2010.uzgjson.xml"))
        self.assertEqual(root.find('./setting[@id="epgCache"]').text, "false")  # type: ignore[union-attr]
        self.assertEqual(root.find('./setting[@id="tvGroupMode"]').text, "1")  # type: ignore[union-attr]
        self.assertEqual(root.find('./setting[@id="oneTvGroup"]').text, "NPO Start")  # type: ignore[union-attr]
        self.assertEqual(
            root.find('./setting[@id="genresPath"]').text,  # type: ignore[union-attr]
            os.path.join(profile_dir, "genres-channel.nos.nos2010.uzgjson.xml")
        )


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
                         '    <!-- Do not modfiy this line. '
                         'Retrospect managed channel: channel.nos.nos2010.uzgjson -->\n'
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
            self.assertIn("Retrospect managed channel: channel.be.vrtmax.bejson", created_content)
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


    def test_configure_pvr_instances_removes_disabled_managed_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile_dir = os.path.join(tmp, "profile")
            pvr_data = os.path.join(tmp, "pvr")
            os.makedirs(profile_dir)
            os.makedirs(pvr_data)

            managed = os.path.join(pvr_data, "instance-settings-1.xml")
            with open(managed, "w", encoding="utf-8") as fh:
                fh.write('<?xml version="1.0"?>\n<settings version="2">\n'
                         '    <!-- Do not modfiy this line. '
                         'Retrospect managed channel: channel.nos.nos2010.uzgjson -->\n'
                         '</settings>\n')

            genre_target = os.path.join(profile_dir, "genres-channel.nos.nos2010.uzgjson.xml")
            with open(genre_target, "w", encoding="utf-8") as fh:
                fh.write("genre")

            with patch("resources.lib.helpers.iptvsimplehelper.Config.profileDir", profile_dir):
                self._configure_pvr_instances(pvr_data, [])
                self.assertFalse(os.path.exists(managed))
                self.assertFalse(os.path.exists(genre_target))


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
            path = os.path.join(tmp, "playlist.m3u8")
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            IptvSimpleHelper.write_playlist(self._SAMPLE_STREAMS, path)
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
            path = os.path.join(tmp, "epg.xml")
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            IptvSimpleHelper.write_epg(self._SAMPLE_EPG, path, streams=self._SAMPLE_STREAMS)
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
            path = os.path.join(tmp, "epg.xml")
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            IptvSimpleHelper.write_epg(epg, path)
            root = ET.parse(path).getroot()
        title = root.find("./programme/title").text  # type: ignore[union-attr]
        self.assertEqual(title, "News & Drama <Live>")

    def test_write_playlist_atomic_write(self) -> None:
        """write_playlist uses .tmp file then replaces."""
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "playlist.m3u8")
            tmp_path = path + ".tmp"
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            IptvSimpleHelper.write_playlist(self._SAMPLE_STREAMS, path)
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
                template = IptvSimpleHelper._get_pvr_instance_template(channel)
                content = IptvSimpleHelper._build_pvr_instance_content(template, channel, None)  # type: ignore[arg-type]
            root = ET.fromstring(content)
            expected_m3u = os.path.join(
                profile_dir, "iptv", "playlist-channel.nos.nos2010.uzgjson.m3u8")
            expected_epg = os.path.join(
                profile_dir, "iptv", "epg-channel.nos.nos2010.uzgjson.xml")
            self.assertEqual(root.find('./setting[@id="m3uPath"]').text, expected_m3u)  # type: ignore[union-attr]
            self.assertEqual(root.find('./setting[@id="epgPath"]').text, expected_epg)  # type: ignore[union-attr]

