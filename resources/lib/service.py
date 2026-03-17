# SPDX-License-Identifier: GPL-3.0-or-later

import dataclasses
import os
import threading
import time
from typing import Any, Callable, List, Optional

import xbmc

from resources.lib.channelinfo import ChannelInfo
from resources.lib.logger import Logger
from resources.lib.helpers.channelimporter import ChannelIndex
from resources.lib.retroconfig import Config

MIN_SERVICE_INTERVAL = 60     # 1 minute
MAX_SERVICE_INTERVAL = 3600   # 60 minutes
IPTV_INTERVAL_DEFAULT = 1800  # 30 minutes


@dataclasses.dataclass
class _ChannelTask:
    channel_name: str
    callback: Callable[[], None]
    last_run: float
    interval: int


class RetroService(xbmc.Monitor):
    """Background service that dispatches periodic callbacks to enrolled channels."""

    _instance = None  # keeps the xbmc.Monitor object alive for the process lifetime
    _thread = None    # the active daemon thread

    def __init__(self) -> None:
        super(RetroService, self).__init__()


    @staticmethod
    def _resolve_interval(raw: Any) -> Optional[int]:
        """
        Convert *raw* to a clamped integer interval.

        Returns a clamped interval, None otherwise.
        """

        try:
            interval = int(raw)
        except (TypeError, ValueError):
            if raw is not None:
                Logger.error("RetroService: invalid interval=%r", raw)
            return None

        if not (MIN_SERVICE_INTERVAL <= interval <= MAX_SERVICE_INTERVAL):
            Logger.warning(
                "RetroService: interval=%ss out of range [%s, %s] — clamping",
                interval, MIN_SERVICE_INTERVAL, MAX_SERVICE_INTERVAL)
            interval = max(MIN_SERVICE_INTERVAL, min(interval, MAX_SERVICE_INTERVAL))

        return interval


    def _write_channel_iptv_files(self, channel_entry: ChannelInfo, channel: Any) -> None:
        """Write per-channel M3U playlist and XMLTV EPG for pvr.iptvsimple."""

        from resources.lib.actions.actionparser import ActionParser
        from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper

        iptv_dir = os.path.join(Config.profileDir, "iptv")
        if not os.path.isdir(iptv_dir):
            try:
                os.makedirs(iptv_dir)
            except OSError as exc:
                Logger.warning("RetroService: failed to create iptv dir: %s", exc)
                return

        parser = ActionParser(Config.addonId, 0, "")
        streams = channel.create_iptv_streams(parser)
        if streams:
            playlist_path = IptvSimpleHelper._get_channel_playlist_path(channel_entry.id)
            IptvSimpleHelper.write_playlist(streams, playlist_path)

            if any(s.get("provider") for s in streams):
                provider_path = IptvSimpleHelper._get_channel_provider_mapping_path(channel_info.id)
                IptvSimpleHelper.write_provider_mapping(streams, provider_path)
                IptvSimpleHelper.enable_provider_mapping_if_available(channel_info.id)

        epg = channel.create_iptv_epg(parser)
        if epg:
            epg_path = IptvSimpleHelper._get_channel_epg_path(channel_entry.id)
            IptvSimpleHelper.write_epg(epg, epg_path, streams=streams)


    def _enroll_channels(self) -> List[_ChannelTask]:
        """Enroll channels that opt into background service tasks."""

        tasks: List[_ChannelTask] = []

        channel_index = ChannelIndex.get_register()
        for channel_entry in channel_index.get_channels():
            channel = channel_entry.get_channel()
            if channel is None:
                continue

            service_interval = self._resolve_interval(
                getattr(channel, 'service_interval', None))
            if service_interval is not None:
                tasks.append(_ChannelTask(
                    channel_name=channel_entry.channelName,
                    callback=channel.service_update,
                    last_run=0.0,
                    interval=service_interval))
                Logger.info("RetroService: '%s' [service_update] every %ss",
                            channel_entry.channelName, service_interval)

            if channel_entry.has_iptv:
                iptv_interval = self._resolve_interval(
                    getattr(channel, 'iptv_refresh_interval', IPTV_INTERVAL_DEFAULT))
                tasks.append(_ChannelTask(
                    channel_name=channel_entry.channelName,
                    callback=lambda ci=channel_entry, ch=channel: self._write_channel_iptv_files(ci, ch),
                    last_run=0.0,
                    interval=iptv_interval))
                Logger.info("RetroService: '%s' [iptv_service] every %ss",
                            channel_entry.channelName, iptv_interval)

        Logger.info("RetroService: %d task(s) enrolled", len(tasks))
        return tasks


    def _tick(self, tasks: List[_ChannelTask]) -> None:
        """Run pending service tasks."""

        now = time.time()
        for task in tasks:
            if self.abortRequested():
                return

            if now - task.last_run >= task.interval:
                Logger.debug("RetroService: calling [%s] for '%s'",
                             task.callback.__name__, task.channel_name)
                try:
                    task.callback()
                    Logger.debug("RetroService: [%s] done for '%s'",
                                 task.callback.__name__, task.channel_name)
                except KeyboardInterrupt:
                    raise
                except BaseException as e:
                    Logger.warning("RetroService: [%s] failed for '%s': %s",
                                   task.callback.__name__, task.channel_name, e)
                finally:
                    task.last_run = now


    def _run(self) -> None:
        """Entry point for the background thread."""

        Logger.info("RetroService: started")

        tasks = self._enroll_channels()

        if not tasks:
            Logger.info("RetroService: no tasks enrolled")
            return

        tick = min((task.interval for task in tasks), default=None)
        if tick is None:
            Logger.info("RetroService: no service tasks registered")
            return

        Logger.info("RetroService: entering main loop")
        Logger.debug("RetroService: main loop tick every %ss", tick)
        while not self.waitForAbort(tick):
            self._tick(tasks)

        Logger.info("RetroService: stopped")


    def onNotification(self, sender: str, method: str, data: str) -> None:
        """Reconfigure IPTV provider instances in response to addon lifecycle events."""

        if method != "System.OnAddonEnabled":
            return

        if not data:
            return

        if ("pvr.iptvsimple" in data or
            "service.iptv.manager" in data):
            Logger.info("RetroService: pvr.iptvsimple/service.iptv.manager enabled — reconfiguring")
            from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
            IptvSimpleHelper.setup_iptvsimple()


    @classmethod
    def start(cls) -> None:
        """Start the background service thread; safe to call multiple times."""

        if (cls._thread is not None and
            cls._thread.is_alive()):
            return

        cls._instance = cls()
        cls._thread = threading.Thread(target=cls._instance._run, name="RetroService", daemon=True)
        cls._thread.start()
