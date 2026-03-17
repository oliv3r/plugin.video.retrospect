# SPDX-License-Identifier: GPL-3.0-or-later

import glob as _glob
import os
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape

import xbmcaddon
import xbmcvfs

from resources.lib.logger import Logger
from resources.lib.retroconfig import Config


class IptvSimpleHelper(object):
    """Static helpers for Retrospect-managed pvr.iptvsimple instances."""

    _PVR_CHANNEL_MARKER = "Do not modfiy this line. Retrospect managed channel: "
    _PVR_MANAGED_SETTING_IDS = frozenset((
        "epgPath",
        "epgPathType",
        "genresPath",
        "genresPathType",
        "m3uPath",
        "m3uPathType",
        "numTvGroups",
        "oneTvGroup",
        "tvGroupMode",
        "useEpgGenreText",
    ))


    def __init__(self) -> None:
        raise NotImplementedError("Just statics")


    @staticmethod
    def _set_xml_setting(content: str, setting_id: str, value: str) -> str:
        """Update or insert a ``<setting id="...">`` element in raw XML text."""

        escaped_setting_id = escape(setting_id, {'"': '&quot;'})
        escaped_value = escape(value)
        pattern = r'<setting id="%s"[^>]*>[^<]*</setting>' % re.escape(setting_id)
        replacement = '<setting id="%s">%s</setting>' % (escaped_setting_id, escaped_value)
        if re.search(pattern, content):
            return re.sub(pattern, replacement, content)
        new_line = '    <setting id="%s">%s</setting>\n' % (escaped_setting_id, escaped_value)
        return content.replace("</settings>", new_line + "</settings>")


    @staticmethod
    def _remove_xml_setting(content: str, setting_id: str) -> str:
        """Remove a ``<setting id="...">`` element from raw XML text if present."""

        pattern = r'\s*<setting id="%s"[^>]*>[^<]*</setting>\n?' % re.escape(setting_id)
        return re.sub(pattern, "", content)


    @staticmethod
    def _set_xml_comment(content: str, comment: str) -> str:
        """Insert an XML comment directly below the root ``<settings>`` element."""

        marker = "<!-- %s -->" % comment
        if marker in content:
            return content

        match = re.search(r'(<settings[^>]*>\n?)', content)
        if not match:
            return content

        return "{}    {}\n{}".format(content[:match.end()], marker, content[match.end():])


    @classmethod
    def _set_managed_channel_comment(cls, content: str, channel_id: str) -> str:
        """Replace any existing Retrospect channel marker with the current channel id."""

        pattern = r'\s*<!--\s*%s.+?\s*-->\n?' % re.escape(cls._PVR_CHANNEL_MARKER)
        content = re.sub(pattern, "", content)
        return cls._set_xml_comment(content, "{}{}".format(cls._PVR_CHANNEL_MARKER, channel_id))


    @classmethod
    def _get_managed_channel_id(cls, content: str) -> Optional[str]:
        """Extract the Retrospect channel marker from an IPTV Simple settings file."""

        pattern = r'<!--\s*%s(.+?)\s*-->' % re.escape(cls._PVR_CHANNEL_MARKER)
        match = re.search(pattern, content)
        if match:
            return match.group(1).strip()
        return None


    @staticmethod
    def _is_iptv_manager_instance(content: str) -> bool:
        """Return whether an IPTV Simple settings file belongs to IPTV Manager."""

        return "service.iptv.manager" in content


    @staticmethod
    def _get_iptv_channels(include_disabled: bool = False) -> list:
        """Return the IPTV-capable channels known to Retrospect."""

        from resources.lib.helpers.channelimporter import ChannelIndex

        channels = ChannelIndex.get_register().get_channels(include_disabled=True)
        iptv_channels = [channel for channel in channels if channel.has_iptv]
        if include_disabled:
            return iptv_channels

        return [channel for channel in iptv_channels if channel.visible and channel.enabled]


    @staticmethod
    def _get_channel_genres_target(channel_id: str) -> str:
        """Return Retrospect's profile-local genres file path for a channel id."""

        return os.path.join(Config.profileDir, "genres-{}.xml".format(channel_id))


    @classmethod
    def _sync_channel_genres_file(cls, channel_info: Any) -> Optional[str]:
        """Copy a channel-local ``genres.xml`` into Retrospect's profile if present."""

        source = os.path.join(channel_info.path, "genres.xml")
        target = cls._get_channel_genres_target(channel_info.id)
        if not os.path.isfile(source):
            if os.path.isfile(target):
                try:
                    os.remove(target)
                    Logger.info("RetroService: removed stale genres file %s", target)
                except OSError as exc:
                    Logger.warning(f"RetroService: failed to remove {target}: {exc}")
            return None

        try:
            with open(source, encoding="utf-8") as fh:
                content = fh.read()
        except OSError as exc:
            Logger.warning(f"RetroService: failed to read {source}: {exc}")
            return None

        if not os.path.isdir(Config.profileDir):
            try:
                os.makedirs(Config.profileDir)
            except OSError as exc:
                Logger.warning(
                    "RetroService: failed to create profile dir %s: %s", Config.profileDir, exc)
                return None

        try:
            current = None
            if os.path.isfile(target):
                with open(target, encoding="utf-8") as fh:
                    current = fh.read()
            if current != content:
                with open(target, "w", encoding="utf-8") as fh:
                    fh.write(content)
                Logger.info("RetroService: synced %s -> %s", source, target)
        except OSError as exc:
            Logger.warning(f"RetroService: failed to write {target}: {exc}")
            return None

        return target


    @staticmethod
    def _get_channel_playlist_path(channel_id: str) -> str:
        """Return the profile-local M3U playlist path for a channel."""

        return os.path.join(Config.profileDir, "iptv", "playlist-{}.m3u8".format(channel_id))


    @staticmethod
    def _get_channel_epg_path(channel_id: str) -> str:
        """Return the profile-local XMLTV EPG path for a channel."""

        return os.path.join(Config.profileDir, "iptv", "epg-{}.xml".format(channel_id))


    @staticmethod
    def _get_channel_provider_mapping_path(channel_id: str) -> str:
        """Return the profile-local providerMappings XML path for a channel."""

        return os.path.join(Config.profileDir, "iptv", "providers-{}.xml".format(channel_id))


    @staticmethod
    def _next_pvr_instance_path(pvr_data: str, existing_paths: Any) -> str:
        """Return the next unused ``instance-settings-N.xml`` path."""

        nums = set()
        for path in existing_paths:
            match = re.search(r'instance-settings-(\d+)\.xml$', os.path.basename(path))
            if match:
                nums.add(int(match.group(1)))

        number = 1
        while number in nums:
            number += 1
        return os.path.join(pvr_data, "instance-settings-%d.xml" % number)


    @staticmethod
    def _get_xml_settings(content: str) -> Dict[str, str]:
        """Return the ``<setting>`` values from raw IPTV Simple settings XML."""

        try:
            root = ET.fromstring(content)
        except ET.ParseError as exc:
            Logger.warning(f"RetroService: failed to parse IPTV Simple settings XML: {exc}")
            return {}

        settings = {}
        for setting in root.findall("./setting"):
            setting_id = setting.get("id")
            if setting_id:
                settings[setting_id] = setting.text or ""
        return settings


    @classmethod
    def _get_pvr_instance_template(cls, channel_info: Any) -> Optional[str]:
        """Load Retrospect's bundled IPTV Simple instance template."""

        template_path = os.path.join(
            Config.rootDir, "resources", "data", "iptv-instance-template.xml")
        try:
            with open(template_path, encoding="utf-8") as fh:
                content = fh.read()
        except OSError as exc:
            Logger.warning(f"RetroService: failed to read {template_path}: {exc}")
            return None

        content = cls._set_xml_setting(
            content,
            "kodi_addon_instance_name",
            "Retrospect - {}".format(channel_info.channelName)
        )
        return content


    @classmethod
    def _merge_pvr_instance_template(cls, template_content: str, source_content: str) -> str:
        """Overlay user-managed IPTV Simple settings from an existing installed file."""

        for setting_id, value in cls._get_xml_settings(source_content).items():
            if setting_id in cls._PVR_MANAGED_SETTING_IDS or value == "":
                continue
            template_content = cls._set_xml_setting(template_content, setting_id, value)
        return template_content


    @classmethod
    def _build_pvr_instance_content(
            cls, content: str, channel_info: Any, genres_path: Optional[str]) -> str:
        """Apply Retrospect's per-channel IPTV Simple settings to XML content."""

        content = cls._set_managed_channel_comment(content, channel_info.id)
        content = cls._set_xml_setting(content, "tvGroupMode", "1")
        content = cls._set_xml_setting(content, "numTvGroups", "1")
        content = cls._set_xml_setting(content, "oneTvGroup", channel_info.channelName)

        playlist_path = cls._get_channel_playlist_path(channel_info.id)
        epg_path = cls._get_channel_epg_path(channel_info.id)
        content = cls._set_xml_setting(content, "m3uPathType", "0")
        content = cls._set_xml_setting(content, "m3uPath", playlist_path)
        content = cls._set_xml_setting(content, "epgPathType", "0")
        content = cls._set_xml_setting(content, "epgPath", epg_path)

        if genres_path is None:
            content = cls._set_xml_setting(content, "useEpgGenreText", "false")
            content = cls._remove_xml_setting(content, "genresPathType")
            content = cls._remove_xml_setting(content, "genresPath")
        else:
            content = cls._set_xml_setting(content, "useEpgGenreText", "true")
            content = cls._set_xml_setting(content, "genresPathType", "0")
            content = cls._set_xml_setting(content, "genresPath", genres_path)

        return content


    @classmethod
    def _remove_managed_pvr_instance(cls, path: str, channel_id: str) -> None:
        """Remove a Retrospect-managed IPTV Simple instance and its genres file."""

        try:
            os.remove(path)
            Logger.info("RetroService: removed managed pvr.iptvsimple instance %s", path)
        except OSError as exc:
            Logger.warning(f"RetroService: failed to remove {path}: {exc}")

        genres_path = cls._get_channel_genres_target(channel_id)
        if os.path.isfile(genres_path):
            try:
                os.remove(genres_path)
                Logger.info("RetroService: removed managed genres file %s", genres_path)
            except OSError as exc:
                Logger.warning(f"RetroService: failed to remove {genres_path}: {exc}")


    @classmethod
    def configure_pvr_instances(cls, pvr_data: str, channels: list) -> None:
        """Install, update, or remove Retrospect-managed IPTV Simple instances."""

        if not os.path.isdir(pvr_data):
            Logger.debug(f"RetroService: pvr_data dir absent ({pvr_data}) — skipping")
            return

        existing_paths = sorted(_glob.glob(os.path.join(pvr_data, "instance-settings-*.xml")))
        managed_instances = {}
        claimable_paths = []

        for path in existing_paths:
            try:
                with open(path, encoding="utf-8") as fh:
                    content = fh.read()
            except OSError:
                continue

            managed_channel_id = cls._get_managed_channel_id(content)
            if managed_channel_id:
                managed_instances[managed_channel_id] = (path, content)
            elif cls._is_iptv_manager_instance(content):
                claimable_paths.append((path, content))

        active_channels = {channel.id: channel for channel in channels}
        for channel_id, (path, _content) in managed_instances.items():
            if channel_id not in active_channels:
                cls._remove_managed_pvr_instance(path, channel_id)

        if not channels:
            return

        claimable_index = 0
        known_paths = set(existing_paths)
        for channel in channels:
            genres_path = cls._sync_channel_genres_file(channel)
            template_content = cls._get_pvr_instance_template(channel)
            if template_content is None:
                continue

            if channel.id in managed_instances:
                target_file, source_content = managed_instances[channel.id]
                original = cls._merge_pvr_instance_template(template_content, source_content)
            elif claimable_index < len(claimable_paths):
                target_file, source_content = claimable_paths[claimable_index]
                original = cls._merge_pvr_instance_template(template_content, source_content)
                claimable_index += 1
            else:
                target_file = cls._next_pvr_instance_path(pvr_data, known_paths)
                original = template_content
                known_paths.add(target_file)

            content = cls._build_pvr_instance_content(original, channel, genres_path)
            if content != original or not os.path.isfile(target_file):
                try:
                    with open(target_file, "w", encoding="utf-8") as fh:
                        fh.write(content)
                    Logger.info("RetroService: configured pvr.iptvsimple instance %s", target_file)
                except OSError as exc:
                    Logger.warning(f"RetroService: failed to write {target_file}: {exc}")


    @classmethod
    def setup_iptvsimple(cls) -> None:
        """Configure pvr.iptvsimple instances if the addon is installed."""

        try:
            pvr_addon = xbmcaddon.Addon("pvr.iptvsimple")
        except RuntimeError:
            Logger.debug("RetroService: pvr.iptvsimple not installed — skipping instance config")
            return

        pvr_data = xbmcvfs.translatePath(pvr_addon.getAddonInfo("profile"))
        cls.configure_pvr_instances(pvr_data, cls._get_iptv_channels())


    @classmethod
    def enable_provider_mapping(cls, pvr_data: str, channel_id: str) -> None:
        """
        Enable provider mapping on the pvr.iptvsimple instance for *channel_id*.

        No-op when *pvr_data* does not exist or no matching instance is found.
        """

        if not os.path.isdir(pvr_data):
            return

        for path in sorted(_glob.glob(os.path.join(pvr_data, "instance-settings-*.xml"))):
            try:
                with open(path, encoding="utf-8") as fh:
                    content = fh.read()
            except OSError:
                continue

            if cls._get_managed_channel_id(content) != channel_id:
                continue

            if cls._get_xml_settings(content).get("enableProviderMappings") == "true":
                return  # already enabled — nothing to do

            provider_path = cls._get_channel_provider_mapping_path(channel_id)
            content = cls._set_xml_setting(content, "enableProviderMappings", "true")
            content = cls._set_xml_setting(content, "providerMappingFile", provider_path)
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(content)
                Logger.info(
                    "IptvSimpleHelper: enabled provider mappings for %s in %s",
                    channel_id, path)
            except OSError as exc:
                Logger.warning(
                    "IptvSimpleHelper: failed to enable provider mappings for %s: %s",
                    channel_id, exc)
            return


    @classmethod
    def enable_provider_mapping_if_available(cls, channel_id: str) -> None:
        """Call ``enable_provider_mapping`` if pvr.iptvsimple is installed."""

        try:
            pvr_addon = xbmcaddon.Addon("pvr.iptvsimple")
        except RuntimeError:
            return

        pvr_data = xbmcvfs.translatePath(pvr_addon.getAddonInfo("profile"))
        cls.enable_provider_mapping(pvr_data, channel_id)


    @staticmethod
    def write_playlist(streams: list, output_path: str) -> None:
        """Write an M3U8 playlist file for pvr.iptvsimple."""

        parent_dir = os.path.dirname(output_path)
        if not os.path.isdir(parent_dir):
            os.makedirs(parent_dir)

        lines = ["#EXTM3U"]
        for s in streams:
            extinf = (
                '#EXTINF:-1 tvg-id="{id}" tvg-name="{name}" '
                'tvg-logo="{logo}" group-title="{group}",{name}'.format(**s)
            )
            lines.append(extinf)
            lines.append(s["stream"])

        content = "\n".join(lines) + "\n"
        tmp_path = output_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, output_path)
        Logger.info("IptvSimpleHelper: wrote playlist to %s (%d channels)",
                    output_path, len(streams))


    @staticmethod
    def write_epg(epg_dict: dict, output_path: str, streams: Optional[list] = None) -> None:
        """Write an XMLTV EPG file for pvr.iptvsimple."""

        from datetime import datetime

        channel_meta = {}
        if streams:
            channel_meta = {s["id"]: s for s in streams}

        def _fmt_ts(ts_str: str) -> str:
            try:
                dt = datetime.fromisoformat(ts_str)
                return dt.strftime("%Y%m%d%H%M%S") + " " + dt.strftime("%z")
            except (ValueError, TypeError):
                return ts_str

        parent_dir = os.path.dirname(output_path)
        if not os.path.isdir(parent_dir):
            os.makedirs(parent_dir)

        lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<tv>"]

        for channel_id in epg_dict:
            meta = channel_meta.get(channel_id, {})
            name = escape(meta.get("name", channel_id))
            lines.append('  <channel id="{}">'.format(escape(channel_id)))
            lines.append("    <display-name>{}</display-name>".format(name))
            logo = meta.get("logo", "")
            if logo:
                lines.append('    <icon src="{}"/>'.format(escape(logo)))
            lines.append("  </channel>")

        for channel_id, programmes in epg_dict.items():
            for prog in programmes:
                start = _fmt_ts(prog.get("start", ""))
                stop = _fmt_ts(prog.get("stop", ""))
                title = escape(prog.get("title", ""))
                lines.append(
                    '  <programme start="{}" stop="{}" channel="{}">'.format(
                        escape(start), escape(stop), escape(channel_id)))
                lines.append("    <title>{}</title>".format(title))
                image = prog.get("image", "")
                if image:
                    lines.append('    <icon src="{}"/>'.format(escape(image)))
                description = prog.get("description", "")
                if description:
                    lines.append("    <desc>{}</desc>".format(escape(description)))
                genre = prog.get("genre", "")
                if genre:
                    lines.append("    <category>{}</category>".format(escape(genre)))
                stream = prog.get("stream", "")
                if stream:
                    lines.append("    <stream>{}</stream>".format(escape(stream)))
                lines.append("  </programme>")

        lines.append("</tv>")
        content = "\n".join(lines) + "\n"
        tmp_path = output_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, output_path)
        Logger.info("IptvSimpleHelper: wrote EPG to %s", output_path)


    @staticmethod
    def write_provider_mapping(streams: list, output_path: str) -> None:
        """Write a pvr.iptvsimple providerMappings XML file."""

        providers = defaultdict(list)
        for s in streams:
            provider = s.get("provider")
            if provider:
                providers[provider].append(s["name"])

        if not providers:
            return

        parent_dir = os.path.dirname(output_path)
        if not os.path.isdir(parent_dir):
            os.makedirs(parent_dir)

        lines = ['<?xml version="1.0" encoding="utf-8"?>', "<providerMappings>"]
        for provider, names in sorted(providers.items()):
            lines.append('  <providerMapping provider="{}">'.format(escape(provider)))
            for name in names:
                lines.append("    <channelName>{}</channelName>".format(escape(name)))
            lines.append("  </providerMapping>")
        lines.append("</providerMappings>")

        content = "\n".join(lines) + "\n"
        tmp_path = output_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, output_path)
        Logger.info("IptvSimpleHelper: wrote provider mapping to %s (%d providers)",
                    output_path, len(providers))
