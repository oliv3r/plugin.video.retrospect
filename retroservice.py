# SPDX-License-Identifier: GPL-3.0-or-later

import importlib
import os
import sys
import time

import xbmc
import xbmcaddon

from resources.lib.logger import Logger
from resources.lib.retroconfig import Config

MAX_SERVICE_INTERVAL = 3600   # 60 minutes — hard ceiling
WARN_SERVICE_INTERVAL = 600   # 10 minutes — slow-interval warning threshold
TICK_INTERVAL = 5             # seconds between service ticks


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
        Logger.info(f"RetroService: channels loaded ({len(self._service_channels)} registered), "
                    f"entering main loop")

        while not self.waitForAbort(TICK_INTERVAL):
            self._tick()

        self._service_channels.clear()
        Logger.info("RetroService: stopped")


if __name__ == '__main__':
    RetroService().run()
