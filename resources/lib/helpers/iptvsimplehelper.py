# SPDX-License-Identifier: GPL-3.0-or-later
from __future__ import annotations

import filecmp
import glob as _glob
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Dict, Optional
from xml.sax.saxutils import escape

import xbmcaddon
import xbmcvfs

from resources.lib.helpers.channelimporter import ChannelIndex
from resources.lib.logger import Logger
from resources.lib.retroconfig import Config

if TYPE_CHECKING:
    from resources.lib.channelinfo import ChannelInfo

_IPTV_INSTANCE_TEMPLATE_PATH = "resources/data/iptv-instance-template.xml"

_PVR_MANAGED_CHANNEL_SETTING = "retrospect_managed_channel"
_PVR_MANAGED_VERSION_SETTING = "retrospect_managed_version"
_PVR_MANAGED_LAST_MODIFICATION_TIME_SETTING = "retrospect_managed_last_modification_time"
_PVR_MANAGED_VERSION = "1"
_PVR_MANAGED_SETTING_IDS = frozenset((
    "epgPath",
    "epgPathType",
    "genresPath",
    "genresPathType",
    "m3uPath",
    "m3uPathType",
    "numTvGroups",
    "oneTvGroup",
    _PVR_MANAGED_CHANNEL_SETTING,
    _PVR_MANAGED_LAST_MODIFICATION_TIME_SETTING,
    _PVR_MANAGED_VERSION_SETTING,
    "tvGroupMode",
    "useEpgGenreText",
))


class IptvSimpleHelper(object):
    """ Helpers for Retrospect-managed pvr.iptvsimple instances. """


    @staticmethod
    def _get_iptv_channels(include_disabled: bool = False) -> list:
        """
        Return the IPTV-capable channels known to Retrospect.

        :param include_disabled:  When ``True``, include disabled channels.

        :return: List of channel objects.
        """

        channels = ChannelIndex.get_register().get_channels(include_disabled=True)
        iptv_channels = [channel for channel in channels if channel.has_iptv]
        if include_disabled:
            return iptv_channels

        return [channel for channel in iptv_channels if channel.visible and channel.enabled]


    @staticmethod
    def _get_xml_settings(content: str) -> Dict[str, str]:
        """
        Get all ``<setting>`` values from raw IPTV Simple settings XML.

        :param content:  Raw XML settings content.

        :return: Dict mapping setting id to setting text value.
        """

        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            Logger.error("RetroService: failed to parse IPTV Simple settings XML", exc_info=True)
            return {}

        settings = {}
        for setting in root.findall("./setting"):
            setting_id = setting.get("id")
            if setting_id:
                settings[setting_id] = setting.text or ""
        return settings


    @staticmethod
    def _settings_compare(path_a: str, path_b: str) -> bool:
        """
        Check if both settings files are equivalent.

        The ``retrospect_managed_last_modification_time`` setting is excluded
        from the comparison, as it is expected to differ between runs.

        :param path_a:  Path to the first settings XML file.
        :param path_b:  Path to the second settings XML file.

        :return: ``True`` if all settings (except timestamp) are equal.
        """

        try:
            with open(path_a, encoding="utf-8") as fh:
                content_a = fh.read()
            with open(path_b, encoding="utf-8") as fh:
                content_b = fh.read()
        except OSError:
            return False

        ignore = frozenset((_PVR_MANAGED_LAST_MODIFICATION_TIME_SETTING,))
        a = {k: v for k, v in IptvSimpleHelper._get_xml_settings(content_a).items()
             if k not in ignore}
        b = {k: v for k, v in IptvSimpleHelper._get_xml_settings(content_b).items()
             if k not in ignore}

        return a == b


    @staticmethod
    def _get_xml_setting(path: str, setting_id: str) -> Optional[str]:
        """
        Read a single ``<setting>`` value from an IPTV Simple settings file.

        :param path:        Path to the settings XML file.
        :param setting_id:  The ``id`` attribute of the setting to read.

        :return: - The setting value if found,
                 - ``None`` if the file could not be read or the setting is absent.
        """

        try:
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            Logger.error(f"RetroService: failed to read {path}", exc_info=True)
            return None

        return IptvSimpleHelper._get_xml_settings(content).get(setting_id)


    @staticmethod
    def _set_xml_setting(content: str, setting_id: str, value: str) -> str:
        """
        Update or insert a ``<setting id="...">`` element in XML settings content.

        :param content:     Raw XML settings content.
        :param setting_id:  The ``id`` attribute of the setting to write.
        :param value:       The text value to set.

        :return: Updated XML content string.
        """

        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            Logger.error("RetroService: failed to parse IPTV Simple settings XML", exc_info=True)
            return content

        for element in root.findall("./setting"):
            if element.get("id") == setting_id:
                element.text = value
                element.attrib.pop("default", None)
                break
        else:
            new = ET.SubElement(root, "setting")
            new.set("id", setting_id)
            new.text = value

        return ET.tostring(root, encoding="unicode")


    @staticmethod
    def _remove_xml_setting(content: str, setting_id: str) -> str:
        """
        Remove a ``<setting id="...">`` element from XML settings content if present.

        :param content:     Raw XML settings content.
        :param setting_id:  The ``id`` attribute of the setting to remove.

        :return: Updated XML content string.
        """

        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            Logger.error("RetroService: failed to parse IPTV Simple settings XML", exc_info=True)
            return content

        for element in root.findall("./setting"):
            if element.get("id") == setting_id:
                root.remove(element)
                break

        return ET.tostring(root, encoding="unicode")


    @staticmethod
    def _install_channel_genres_file(channel_info: ChannelInfo) -> Optional[str]:
        """
        If we have a ``genres.xml``, copy it to a channel-local specific file.

        :param channel_info:  Channel info object with ``path`` and ``id``.

        :return: - Path to the genres file if present,
                 - ``None`` if not available.
        """

        source = os.path.join(channel_info.path, "genres.xml")
        if not os.path.isfile(source):
            return None

        if not os.path.isdir(Config.profileDir):
            try:
                os.makedirs(Config.profileDir)
            except OSError:
                Logger.error(
                    f"RetroService: failed to create profile dir {Config.profileDir}",
                    exc_info=True)
                return None

        target = os.path.join(Config.profileDir, f"genres-{channel_info.id}.xml")
        if (os.path.isfile(target) and
            filecmp.cmp(source, target, shallow=False)):
            return target

        try:
            shutil.copy2(source, target)
            Logger.info(f"RetroService: copied {source} -> {target}")
        except OSError:
            Logger.error(f"RetroService: failed to write {target}", exc_info=True)
            return None

        return target


    def _write_file(self, path: str, content: str) -> Optional[str]:
        """
        Write *content* to *path* atomically via a temp file.

        If *path* already exists and only the timestamp setting differs the
        write is skipped.

        :param path:     Destination file path.
        :param content:  Content to write.

        :return: - *path* on success (written or unchanged),
                 - ``None`` on I/O failure.
        """

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                delete=False,
                dir=os.path.dirname(path),
                encoding="utf-8",
                mode="w",
                suffix=".tmp",
            ) as fh:
                tmp_path = fh.name
                try:
                    fh.write(content)
                except OSError:
                    Logger.error(
                        "RetroService: failed to write pvr.iptvsimple instance", exc_info=True)
                    return None

            if os.path.isfile(path) and self._settings_compare(tmp_path, path):
                Logger.debug(
                    f"RetroService: pvr.iptvsimple instance {path} unchanged — skipping")
                return path

            try:
                os.replace(tmp_path, path)
            except OSError:
                Logger.error(
                    f"RetroService: failed to write pvr.iptvsimple instance {path}",
                    exc_info=True)
                return None

            tmp_path = None
            Logger.info(f"RetroService: configured pvr.iptvsimple instance {path}")
            return path
        except OSError:
            Logger.error(
                "RetroService: failed to create pvr.iptvsimple instance temp file", exc_info=True)
            return None
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass


    def _create_instance_settings(self, iptvsimple_profile_dir: str, content: str) -> Optional[str]:
        """
        Create a new IPTV Simple instance settings file with the next available number.

        :param iptvsimple_profile_dir:  Profile directory to create the file in.
        :param content:                 XML settings content to write.

        :return: - Path of the newly created file on success,
                 - ``None`` on I/O failure.
        """

        existing_paths = _glob.glob(os.path.join(iptvsimple_profile_dir, "instance-settings-*.xml"))
        used_numbers = []
        for path in existing_paths:
            match = re.search(r'instance-settings-(\d+)\.xml$', os.path.basename(path))
            if match:
                used_numbers.append(int(match.group(1)))
        number = 1
        while number in used_numbers:
            number += 1
        target_path = os.path.join(iptvsimple_profile_dir, f"instance-settings-{number}.xml")

        return self._write_file(target_path, content)


    def _find_instance_settings(self, iptvsimple_profile_dir: str, channel_id: str) -> Optional[str]:
        """
        Return the path of the managed instance settings file for *channel_id*, or ``None``.

        :param pvr_data:    Path to the pvr.iptvsimple profile directory.
        :param channel_id:  Channel identifier to search for.

        :return: Matching instance settings path, or ``None`` if not found.
        """

        for path in _glob.glob(os.path.join(iptvsimple_profile_dir, "instance-settings-*.xml")):
            if self._get_xml_setting(path, _PVR_MANAGED_CHANNEL_SETTING) == channel_id:
                return path
        return None


    def _load_instance_settings(self, existing_path: Optional[str] = None) -> Optional[str]:
        """
        Load a ready-to-configure IPTV Simple instance from the bundled template.

        Reads the template, then merges any user-managed settings from *existing_path*
        (when supplied).

        :param existing_path:  Path to an already-installed instance file whose
                               user-managed settings should be preserved, or
                               ``None`` to start from the clean template.

        :return: - Ready-to-configure XML content string,
                 - ``None`` if the template file could not be read.
        """

        template_path = os.path.join(Config.rootDir, _IPTV_INSTANCE_TEMPLATE_PATH)
        try:
            with open(template_path, encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            Logger.error(f"RetroService: failed to read {template_path}", exc_info=True)
            return None

        if existing_path is not None:
            try:
                with open(existing_path, encoding="utf-8") as fh:
                    existing = fh.read()
                for setting_id, value in self._get_xml_settings(existing).items():
                    if setting_id in _PVR_MANAGED_SETTING_IDS:
                        continue
                    content = self._set_xml_setting(content, setting_id, value)
            except OSError:
                Logger.error(f"RetroService: failed to read {existing_path}", exc_info=True)

        return content


    def _build_pvr_instance_content(self, content: str,
                                    channel_info: ChannelInfo,
                                    genres_path: Optional[str]) -> str:
        """
        Apply Retrospect's per-channel IPTV Simple settings to XML content.

        :param content:       XML content to update.
        :param channel_info:  Channel info object.
        :param genres_path:   Path to the genres file, or ``None`` if absent.

        :return: Updated XML content string.
        """

        content = self._set_xml_setting(content, _PVR_MANAGED_CHANNEL_SETTING, channel_info.id)
        content = self._set_xml_setting(content, _PVR_MANAGED_VERSION_SETTING, _PVR_MANAGED_VERSION)
        content = self._set_xml_setting(content, _PVR_MANAGED_LAST_MODIFICATION_TIME_SETTING,
                                        datetime.now(timezone.utc).isoformat())
        current_name = self._get_xml_settings(content).get("kodi_addon_instance_name", "")
        if not current_name or current_name.startswith("Retrospect"):
            content = self._set_xml_setting(
                content, "kodi_addon_instance_name", f"Retrospect - {channel_info.channelName}")
        content = self._set_xml_setting(content, "tvGroupMode", "2")
        content = self._set_xml_setting(content, "numTvGroups", "1")
        content = self._set_xml_setting(content, "oneTvGroup", channel_info.channelName)

        playlist_path = os.path.join(Config.profileDir, "iptv", f"playlist-{channel_info.id}.m3u8")
        epg_path = os.path.join(Config.profileDir, "iptv", f"epg-{channel_info.id}.xml")
        content = self._set_xml_setting(content, "m3uPath", playlist_path)
        content = self._set_xml_setting(content, "m3uPathType", "0")
        content = self._set_xml_setting(content, "epgPath", epg_path)
        content = self._set_xml_setting(content, "epgPathType", "0")

        if genres_path is None:
            content = self._set_xml_setting(content, "useEpgGenreText", "false")
            content = self._remove_xml_setting(content, "genresPath")
            content = self._remove_xml_setting(content, "genresPathType")
        else:
            content = self._set_xml_setting(content, "genresPath", genres_path)
            content = self._set_xml_setting(content, "genresPathType", "0")
            content = self._set_xml_setting(content, "useEpgGenreText", "true")

        return content


    def _disable_managed_pvr_instance(self, path: str, channel_id: str) -> None:
        """
        Disable a Retrospect-managed IPTV Simple instance.

        :param path:        Path to the instance settings XML file.
        :param channel_id:  Channel identifier used to verify ownership.
        """

        if self._get_xml_setting(path, _PVR_MANAGED_CHANNEL_SETTING) != channel_id:
            Logger.warning(
                f"RetroService: {path} is not our managed instance for {channel_id} — skipping")
            return

        try:
            with open(path, encoding="utf-8") as fh:
                content = fh.read()
        except OSError:
            Logger.error(f"RetroService: failed to read {path}", exc_info=True)
            return

        content = self._set_xml_setting(content, "kodi_addon_instance_enabled", "false")
        self._write_file(path, content)
        Logger.info(f"RetroService: disabled managed pvr.iptvsimple instance {path}")


    def _configure_pvr_instances(self, iptvsimple_profile_dir: str, channels: list) -> None:
        """
        Install, update, or remove Retrospect-managed IPTV Simple instances.

        :param pvr_data:  Path to the pvr.iptvsimple profile directory.
        :param channels:  IPTV-capable channel objects to configure.
        """

        if not os.path.isdir(iptvsimple_profile_dir):
            Logger.debug(f"RetroService: pvr_data dir absent ({iptvsimple_profile_dir}) — skipping")
            return

        active_ids = {channel.id for channel in channels}
        for path in _glob.glob(os.path.join(iptvsimple_profile_dir, "instance-settings-*.xml")):
            channel_id = self._get_xml_setting(path, _PVR_MANAGED_CHANNEL_SETTING)
            if channel_id and channel_id not in active_ids:
                self._disable_managed_pvr_instance(path, channel_id)

        for channel in channels:
            existing_path = self._find_instance_settings(iptvsimple_profile_dir, channel.id)
            base_settings = self._load_instance_settings(existing_path)
            if base_settings is None:
                continue
            genres_path = self._install_channel_genres_file(channel)
            new_xml = self._build_pvr_instance_content(base_settings, channel, genres_path)
            if existing_path is not None:
                self._write_file(existing_path, new_xml)
            else:
                new_xml = self._set_xml_setting(new_xml, "kodi_addon_instance_enabled", "true")
                self._create_instance_settings(iptvsimple_profile_dir, new_xml)


    def write_provider_mapping(self, streams: list, channel_id: str) -> None:
        """
        Write a pvr.iptvsimple providerMappings XML file.

        :param streams:     Stream dicts with optional ``provider`` and
                            ``name`` keys.
        :param channel_id:  Channel identifier used to derive the output path.
        """

        providers = defaultdict(list)
        for s in streams:
            provider = s.get("provider")
            if provider:
                providers[provider].append(s["name"])
        if not providers:
            return

        output_path = os.path.join(Config.profileDir, "iptv", f"providers-{channel_id}.xml")
        parent_dir = os.path.dirname(output_path)
        if not os.path.isdir(parent_dir):
            os.makedirs(parent_dir)

        lines = ['<?xml version="1.0" encoding="utf-8"?>', "<providerMappings>"]
        for provider, names in sorted(providers.items()):
            lines.append(f'  <providerMapping provider="{escape(provider)}">')
            for name in names:
                lines.append(f"    <channelName>{escape(name)}</channelName>")
            lines.append("  </providerMapping>")
        lines.append("</providerMappings>")

        content = "\n".join(lines) + "\n"
        tmp_path = output_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, output_path)
        Logger.info(
            f"IptvSimpleHelper: wrote provider mapping to {output_path} ({len(providers)} providers)")


    def enable_provider_mapping(self, channel_id: str) -> None:
        """
        Enable provider mapping on the pvr.iptvsimple instance for *channel_id*.

        No-op when pvr.iptvsimple is not installed, its profile directory does
        not exist, or no matching instance is found.

        :param channel_id:  Channel identifier to match.
        """

        try:
            pvr_addon = xbmcaddon.Addon("pvr.iptvsimple")
        except RuntimeError:
            return

        iptvsimple_profile_dir = xbmcvfs.translatePath(pvr_addon.getAddonInfo("profile"))
        if not os.path.isdir(iptvsimple_profile_dir):
            return

        for instance_settings_path in _glob.glob(os.path.join(iptvsimple_profile_dir, "instance-settings-*.xml")):
            if self._get_xml_setting(instance_settings_path, _PVR_MANAGED_CHANNEL_SETTING) != channel_id:
                continue

            if self._get_xml_setting(instance_settings_path, "enableProviderMappings") == "true":
                return  # already enabled — nothing to do

            try:
                with open(instance_settings_path, encoding="utf-8") as fh:
                    content = fh.read()
            except OSError:
                continue

            provider_path = os.path.join(Config.profileDir, "iptv", f"providers-{channel_id}.xml")
            content = self._set_xml_setting(content, "providerMappingFile", provider_path)
            content = self._set_xml_setting(content, "enableProviderMappings", "true")
            try:
                with open(instance_settings_path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                Logger.info(f"IptvSimpleHelper: enabled provider mappings for {channel_id} in {instance_settings_path}")
            except OSError:
                Logger.error(f"IptvSimpleHelper: failed to enable provider mappings for {channel_id}", exc_info=True)
            return


    def write_playlist(self, streams: list, channel_id: str) -> None:
        """
        Write an M3U8 playlist file for pvr.iptvsimple.

        :param streams:     Stream dicts with ``id``, ``name``, ``logo``,
                                              ``group``, and ``stream`` keys.
        :param channel_id:  Channel identifier used to derive the output path.
        """

        parent_dir = os.path.join(Config.profileDir, "iptv")
        if not os.path.isdir(parent_dir):
            os.makedirs(parent_dir)

        lines = ["#EXTM3U"]
        for s in streams:
            lines.append(
                f'#EXTINF:-1 tvg-id="{s["id"]}" tvg-name="{s["name"]}" '
                f'tvg-logo="{s["logo"]}" group-title="{s["group"]}",{s["name"]}')
            lines.append(s["stream"])
        content = "\n".join(lines) + "\n"

        output_path = os.path.join(parent_dir, f"playlist-{channel_id}.m3u8")
        tmp_path = output_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, output_path)
        Logger.info(f"IptvSimpleHelper: wrote playlist to {output_path} ({len(streams)} channels)")


    @staticmethod
    def _convert_timestamp(timestamp: str) -> str:
        """
        Convert the NLZIET timestamp to a iso timestamp for us.

        :param timestamp:  timestamp to convert

        :return: - ISO formatted timestamp or,
                 - The same timestamp offered if conversion failed.
        """

        try:
            dt = datetime.fromisoformat(timestamp)
            return f'{dt.strftime("%Y%m%d%H%M%S")} {dt.strftime("%z") or "+0000"}'
        except (ValueError, TypeError):
            return timestamp


    def write_epg(self, epg_dict: dict, channel_id: str, streams: Optional[list] = None) -> None:
        """
        Write an XMLTV EPG file for pvr.iptvsimple.

        :param epg_dict:    Dict mapping channel id to list of programme dicts.
        :param channel_id:  Channel identifier used to derive the output path.
        :param streams:     Optional stream dicts for channel display-name and
                            logo enrichment.
        """

        parent_dir = os.path.join(Config.profileDir, "iptv")
        if not os.path.isdir(parent_dir):
            os.makedirs(parent_dir)

        channel_meta = {}
        if streams:
            channel_meta = {s["id"]: s for s in streams}

        lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<tv>"]
        for chn_id in epg_dict:
            lines.append(f'  <channel id="{escape(chn_id)}">')
            meta = channel_meta.get(chn_id, {})
            name = escape(meta.get("name", chn_id))
            lines.append(f"    <display-name>{name}</display-name>")
            logo = meta.get("logo", "")
            if logo:
                lines.append(f'    <icon src="{escape(logo)}"/>')
            lines.append("  </channel>")

        for chn_id, programmes in epg_dict.items():
            for prog in programmes:
                start = self._convert_timestamp(prog["start"])
                stop = self._convert_timestamp(prog["stop"])
                lines.append(
                    f'  <programme start="{escape(start)}" stop="{escape(stop)}"'
                    f' channel="{escape(chn_id)}">')
                title = escape(prog["title"])
                lines.append(f"    <title>{title}</title>")
                image = prog.get("image", "")
                if image:
                    lines.append(f'    <icon src="{escape(image)}"/>')
                date = prog.get("date", "")
                if date:
                    lines.append(f"    <date>{escape(date)}</date>")
                description = prog.get("description", "")
                if description:
                    lines.append(f"    <desc>{escape(description)}</desc>")
                genre = prog.get("genre", "")
                if genre:
                    lines.append(f"    <category>{escape(genre)}</category>")
                stream = prog.get("stream", "")
                if stream:
                    lines.append(f"    <stream>{escape(stream)}</stream>")
                lines.append("  </programme>")
        lines.append("</tv>")
        content = "\n".join(lines) + "\n"

        output_path = os.path.join(parent_dir, f"epg-{channel_id}.xml")
        tmp_path = output_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, output_path)
        Logger.info(f"IptvSimpleHelper: wrote EPG to {output_path}")


    def setup_iptvsimple(self) -> None:
        """ Configure pvr.iptvsimple instances if the addon is installed. """

        try:
            pvr_addon = xbmcaddon.Addon("pvr.iptvsimple")
        except RuntimeError:
            Logger.debug("RetroService: pvr.iptvsimple not installed — skipping instance config")
            return

        iptvsimple_profile_dir = xbmcvfs.translatePath(pvr_addon.getAddonInfo("profile"))
        self._configure_pvr_instances(iptvsimple_profile_dir, self._get_iptv_channels())
