# SPDX-License-Identifier: GPL-3.0-or-later

import importlib
import sys
import time

import xbmc
import xbmcaddon

_MAX_SERVICE_INTERVAL = 3600   # 60 minutes — hard ceiling
_WARN_SERVICE_INTERVAL = 600   # 10 minutes — slow-interval warning threshold


def _log(msg, level=xbmc.LOGDEBUG):
    xbmc.log("[RetroService] " + msg, level)


def autorun_retrospect():
    if xbmcaddon.Addon().getSetting("auto_run") == "true":
        xbmc.executebuiltin('RunAddon(plugin.video.retrospect)')


class RetroService(xbmc.Monitor):
    """Background service: auto-run Retrospect and dispatch channel service callbacks."""

    def __init__(self):
        super(RetroService, self).__init__()
        self._service_channels = {}

    def run(self):
        _log("started", xbmc.LOGINFO)
        autorun_retrospect()
        self._load_service_channels()
        while not self.waitForAbort(30):
            self._tick()
        _log("stopped", xbmc.LOGINFO)

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
                    _log("'%s' has invalid service_interval=%r — skipping"
                         % (channel_info.channelName, interval), xbmc.LOGWARNING)
                    continue
                if interval > _MAX_SERVICE_INTERVAL:
                    _log("'%s' service_interval=%ds exceeds 60 minutes — clamping to %ds"
                         % (channel_info.channelName, interval, _MAX_SERVICE_INTERVAL),
                         xbmc.LOGWARNING)
                    interval = _MAX_SERVICE_INTERVAL
                elif interval > _WARN_SERVICE_INTERVAL:
                    _log("'%s' slow service_interval=%ds (> 10 minutes)"
                         % (channel_info.channelName, interval), xbmc.LOGWARNING)

                self._service_channels[channel_info.guid] = [channel, 0.0, interval]
                _log("registered '%s' (effective_interval=%ds)" % (
                    channel_info.channelName, interval), xbmc.LOGINFO)
            except Exception as e:
                _log("failed to load channel '%s': %s" % (channel_info.channelName, e),
                     xbmc.LOGWARNING)

    def _tick(self):
        """Dispatch on_service() to each channel whose interval has elapsed."""
        now = time.time()
        for guid, entry in self._service_channels.items():
            channel, last_run, interval = entry
            if now - last_run >= interval:
                try:
                    channel.on_service()
                    entry[1] = now
                except KeyboardInterrupt:
                    raise
                except BaseException as e:
                    _log("on_service() failed for channel '%s': %s" % (guid, e),
                         xbmc.LOGWARNING)


if __name__ == '__main__':
    RetroService().run()
