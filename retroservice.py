# SPDX-License-Identifier: GPL-3.0-or-later

import glob as _glob
import importlib
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

import xbmc
import xbmcaddon
import xbmcvfs

from resources.lib.logger import Logger
from resources.lib.retroconfig import Config

MAX_SERVICE_INTERVAL = 3600   # 60 minutes — hard ceiling
WARN_SERVICE_INTERVAL = 600   # 10 minutes — slow-interval warning threshold
TICK_INTERVAL = 5             # seconds between service ticks

_PVR_GENRES_VERSION = 1
_PVR_INSTANCE_LOCK = threading.Lock()


def _set_xml_setting(content, setting_id, value):
    """Update or insert a ``<setting id="...">`` element in raw XML text.

    Strips the ``default="true"`` attribute (marking as user-configured) and
    replaces the element text.  Inserts a new element before ``</settings>``
    if the id is absent.

    :param str content: Raw XML file content.
    :param str setting_id: The ``id`` attribute to match.
    :param str value: The new element text.
    :return: Updated XML content.
    :rtype: str
    """
    escaped_setting_id = escape(setting_id, {'"': '&quot;'})
    escaped_value = escape(value)
    pattern = r'<setting id="%s"[^>]*>[^<]*</setting>' % re.escape(setting_id)
    replacement = '<setting id="%s">%s</setting>' % (escaped_setting_id, escaped_value)
    if re.search(pattern, content):
        return re.sub(pattern, replacement, content)
    new_line = '    <setting id="%s">%s</setting>\n' % (escaped_setting_id, escaped_value)
    return content.replace("</settings>", new_line + "</settings>")


def _merge_genre_xmls():
    """Merge per-channel genres.xml files into a single mapping for pvr.iptvsimple.

    Walks ``channels/**/genres.xml``; each file maps channel-specific genre
    text strings (e.g. Dutch API labels) to DVB hex codes.  Multiple channels
    can share the same genreId; later files override earlier ones for the same
    text key.

    :return: Merged XML string, or None if no genres.xml files are found.
    :rtype: str|None
    """
    genres = {}
    channels_dir = os.path.join(Config.rootDir, "channels")
    if os.path.isdir(channels_dir):
        for dirpath, _dirs, files in os.walk(channels_dir):
            if "genres.xml" not in files:
                continue
            path = os.path.join(dirpath, "genres.xml")
            try:
                croot = ET.parse(path).getroot()
                count = 0
                for elem in croot.findall("genre"):
                    gid = elem.get("genreId")
                    if gid and elem.text:
                        genres[elem.text.strip()] = gid
                        count += 1
                Logger.debug(f"RetroService: merged {count} genre entries from {path}")
            except ET.ParseError as e:
                Logger.warning(f"RetroService: skipping {path} (parse error: {e})")

    if not genres:
        Logger.debug("RetroService: no per-channel genres.xml files found")
        return None

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<!--',
        '  Retrospect merged genre-text mappings for pvr.iptvsimple.',
        '  Generated from per-channel genres.xml files.',
        '  version: %d' % _PVR_GENRES_VERSION,
        '-->',
        '',
        '<genres>',
        '  <name>Retrospect Genre Mappings</name>',
        '',
    ]
    for text, gid in genres.items():
        lines.append(
            '  <genre genreId="%s">%s</genre>' % (
                escape(gid, {'"': '&quot;'}),
                escape(text)
            )
        )
    lines += ['</genres>', '']
    return '\n'.join(lines)


def _setup_pvr_genres():
    """Install or refresh the merged genre-text mapping for pvr.iptvsimple.

    Version-stamps the installed file so it is only rewritten when
    ``_PVR_GENRES_VERSION`` changes.  Always ends by attempting instance
    configuration so catchup/name settings are applied even when genre
    data files are not yet available.
    """
    target = os.path.join(Config.profileDir, "genres.xml")
    version_marker = "version: %d" % _PVR_GENRES_VERSION
    if os.path.isfile(target):
        try:
            with open(target, encoding="utf-8") as fh:
                head = fh.read(512)
            if version_marker in head:
                Logger.debug(f"RetroService: pvr_genres v{_PVR_GENRES_VERSION} already installed")
                _configure_pvr_instance_if_available()
                return
        except OSError:
            pass

    merged = _merge_genre_xmls()
    genres_path = None
    if merged:
        try:
            with open(target, "w", encoding="utf-8") as fh:
                fh.write(merged)
            Logger.info(f"RetroService: installed pvr_genres v{_PVR_GENRES_VERSION} -> {target}")
            genres_path = target
        except OSError as e:
            Logger.warning(f"RetroService: failed to write genres.xml: {e}")

    _configure_pvr_instance_if_available(genres_path=genres_path)


def _configure_pvr_instance(pvr_data, genres_path=None):
    """Find or create Retrospect's pvr.iptvsimple instance and apply settings.

    Three-phase lookup — never modifies files that do not belong to Retrospect:

    1. **By name**: scan for ``kodi_addon_instance_name=Retrospect``.
    2. **IPTV Manager claim**: scan for files referencing ``service.iptv.manager``
       (IPTV Manager created them for Retrospect; claim on first run).
    3. **Create fresh**: write a new ``instance-settings-N.xml`` with the next
       unused N.

    :param str pvr_data: pvr.iptvsimple profile directory.
    :param str|None genres_path: Absolute path to installed genres.xml, or None.
    """
    with _PVR_INSTANCE_LOCK:
        if not os.path.isdir(pvr_data):
            Logger.debug(f"RetroService: pvr_data dir absent ({pvr_data}) — skipping")
            return

        existing = _glob.glob(os.path.join(pvr_data, "instance-settings-*.xml"))

        # Phase 1: find our own file by name
        target_file = None
        for path in existing:
            try:
                with open(path, encoding="utf-8") as fh:
                    content = fh.read()
            except OSError:
                continue
            if '<setting id="kodi_addon_instance_name">Retrospect</setting>' in content:
                target_file = path
                Logger.debug(f"RetroService: found existing Retrospect instance: {path}")
                break

        # Phase 2: claim a file IPTV Manager created for us
        if target_file is None:
            for path in existing:
                try:
                    with open(path, encoding="utf-8") as fh:
                        content = fh.read()
                except OSError:
                    continue
                if "service.iptv.manager" in content:
                    target_file = path
                    Logger.info(f"RetroService: claiming IPTV Manager instance: {path}")
                    break

        # Phase 3: create fresh instance-settings-N.xml
        if target_file is None:
            nums = set()
            for path in existing:
                m = re.search(r'instance-settings-(\d+)\.xml$', os.path.basename(path))
                if m:
                    nums.add(int(m.group(1)))
            n = 1
            while n in nums:
                n += 1
            target_file = os.path.join(pvr_data, "instance-settings-%d.xml" % n)
            content = ('<?xml version="1.0" encoding="utf-8"?>\n'
                       '<settings version="2">\n'
                       '</settings>\n')
            Logger.info(f"RetroService: creating new pvr.iptvsimple instance: {target_file}")
        else:
            try:
                with open(target_file, encoding="utf-8") as fh:
                    content = fh.read()
            except OSError as e:
                Logger.warning(f"RetroService: failed to read {target_file}: {e}")
                return

        original = content
        content = _set_xml_setting(content, "kodi_addon_instance_name", "Retrospect")
        content = _set_xml_setting(content, "catchupEnabled", "true")
        content = _set_xml_setting(content, "catchupOnlyOnFinishedProgrammes", "false")
        if genres_path:
            content = _set_xml_setting(content, "useEpgGenreText", "true")
            content = _set_xml_setting(content, "genresPathType", "0")
            content = _set_xml_setting(content, "genresPath", genres_path)

        if content != original or not os.path.isfile(target_file):
            try:
                with open(target_file, "w", encoding="utf-8") as fh:
                    fh.write(content)
                Logger.info(f"RetroService: configured pvr.iptvsimple instance {target_file}")
            except OSError as e:
                Logger.warning(f"RetroService: failed to write {target_file}: {e}")


def _configure_pvr_instance_if_available(genres_path=None):
    """Call ``_configure_pvr_instance`` if pvr.iptvsimple is installed.

    :param str|None genres_path: Path to installed genres.xml, or None.
    """
    try:
        pvr_addon = xbmcaddon.Addon("pvr.iptvsimple")
    except RuntimeError:
        Logger.debug("RetroService: pvr.iptvsimple not installed — skipping instance config")
        return
    pvr_data = xbmcvfs.translatePath(pvr_addon.getAddonInfo("profile"))
    genres = genres_path
    if genres is None:
        candidate = os.path.join(Config.profileDir, "genres.xml")
        if os.path.isfile(candidate):
            genres = candidate
    _configure_pvr_instance(pvr_data, genres_path=genres)


class RetroService(xbmc.Monitor):
    """Background service: auto-run Retrospect and dispatch channel service callbacks."""

    def __init__(self):
        super(RetroService, self).__init__()
        self._service_channels = {}
        if not Logger.exists():
            Logger.create_logger(
                os.path.join(Config.profileDir, Config.logFileNameAddon),
                Config.appName,
                append=True)

    def _tick(self):
        """Dispatch on_service() to each channel whose interval has elapsed."""
        if self.abortRequested():
            return
        now = time.time()
        for guid, entry in self._service_channels.items():
            channel, last_run, interval = entry
            if now - last_run >= interval:
                Logger.debug(f"RetroService: calling on_service for '{guid}'")
                try:
                    channel.on_service()
                    Logger.debug(f"RetroService: on_service done for '{guid}'")
                except KeyboardInterrupt:
                    raise
                except BaseException as e:
                    Logger.warning(f"RetroService: on_service() failed for channel '{guid}'"
                                   f": {e}")
                finally:
                    entry[1] = now

    def _load_service_channels(self):
        """Instantiate channels that opt into periodic service callbacks.

        Peeks at each Channel class's ``service_interval`` attribute before
        instantiating so that channels that do not opt in are never loaded.
        Validates and clamps the interval before storing it.
        """
        from resources.lib.helpers.channelimporter import ChannelIndex

        channel_index = ChannelIndex.get_register()
        for channel_info in channel_index.get_channels():
            try:
                if channel_info.path not in sys.path:
                    sys.path.append(channel_info.path)
                mod = importlib.import_module(channel_info.moduleName)
                if getattr(mod.Channel, 'service_interval', None) is None:
                    continue
                channel = channel_info.get_channel()
                if channel is None:
                    continue

                interval = channel.service_interval
                if not isinstance(interval, (int, float)) or interval <= 0:
                    Logger.error(f"RetroService: '{channel_info.channelName}' "
                                 f"has invalid service_interval={interval!r} — skipping")
                    continue

                if interval > MAX_SERVICE_INTERVAL:
                    Logger.warning(f"RetroService: '{channel_info.channelName}' "
                                   f"service_interval={interval}s exceeds 60 minutes "
                                   f"— clamping to {MAX_SERVICE_INTERVAL}s")
                    interval = MAX_SERVICE_INTERVAL
                elif interval > WARN_SERVICE_INTERVAL:
                    Logger.warning(f"RetroService: '{channel_info.channelName}' "
                                   f"slow service_interval={interval}s (> 10 minutes)")

                self._service_channels[channel_info.guid] = [channel, 0.0, interval]
                Logger.info(f"RetroService: registered '{channel_info.channelName}' "
                            f"(effective_interval={interval}s)")
            except Exception as e:
                Logger.error(f"RetroService: failed to load channel "
                             f"'{channel_info.channelName}': {e}")

    def onNotification(self, sender, method, data):  # NOSONAR
        """Re-configure pvr.iptvsimple genre settings whenever a relevant addon is enabled.

        Acts on ``System.OnAddonEnabled`` for:
        - ``pvr.iptvsimple``: configure immediately.
        - ``service.iptv.manager``: creates the instance file asynchronously;
          wait 5 s so it exists before configuring.
        """
        if method != "System.OnAddonEnabled":
            return
        data = data or ""
        if "pvr.iptvsimple" in data:
            Logger.info("RetroService: pvr.iptvsimple enabled — reconfiguring")
            _setup_pvr_genres()
        elif "service.iptv.manager" in data:
            Logger.info("RetroService: service.iptv.manager enabled — scheduling reconfigure")
            threading.Thread(target=RetroService._delayed_pvr_setup, daemon=True).start()

    @staticmethod
    def _delayed_pvr_setup():
        """Wait briefly then reconfigure pvr.iptvsimple (IPTV Manager creates files async)."""
        time.sleep(5)
        _setup_pvr_genres()

    def run(self):
        Logger.info("RetroService: started")

        if xbmcaddon.Addon().getSetting("auto_run") == "true":
            xbmc.executebuiltin('RunAddon(plugin.video.retrospect)')

        from resources.lib.urihandler import UriHandler
        from resources.lib.addonsettings import AddonSettings
        UriHandler.create_uri_handler(
            cache_dir=Config.cacheDir if AddonSettings.cache_http_responses() else None,
            cookie_jar=os.path.join(Config.profileDir, "cookiejar.dat"),
            ignore_ssl_errors=AddonSettings.ignore_ssl_errors(),
            web_time_out=8)

        self._load_service_channels()
        _configure_pvr_instance_if_available()
        Logger.info(f"RetroService: channels loaded ({len(self._service_channels)} registered), "
                    f"entering main loop")

        while not self.waitForAbort(TICK_INTERVAL):
            self._tick()

        self._service_channels.clear()
        Logger.info("RetroService: stopped")


if __name__ == '__main__':
    RetroService().run()
