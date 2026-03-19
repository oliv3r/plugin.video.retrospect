# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import dataclasses
import os
import time
from typing import Any, Callable, List, Optional

import xbmc

from resources.lib.channelinfo import ChannelInfo
from resources.lib.logger import Logger
from resources.lib.actions.actionparser import ActionParser
from resources.lib.helpers.channelimporter import ChannelIndex
from resources.lib.helpers.iptvsimplehelper import IptvSimpleHelper
from resources.lib.retroconfig import Config
from resources.lib.settings.kodisettings import KodiSettings

MIN_SERVICE_INTERVAL = 60     # 1 minute
MAX_SERVICE_INTERVAL = 3600   # 60 minutes
IPTV_INTERVAL_DEFAULT = 1800  # 30 minutes

_CHECK_INTERVAL = 2           # seconds between stop-condition checks in the main loop


@dataclasses.dataclass
class _ChannelTask:
    channel_name: str
    callback: Callable[[], None]
    last_run: float
    interval: int


class RetroService(xbmc.Monitor):
    """ Background service that dispatches periodic callbacks to enrolled channels. """

    def __init__(self) -> None:
        super(RetroService, self).__init__()


    @staticmethod
    def _iptv_active() -> bool:
        """
        Check whether IPTV Manager is active to determine if we should run.

        :returns:   - True when we should run,
                    - False otherwise.
        """

        return not KodiSettings(Logger.instance()).get_boolean_setting("iptv.enabled")


    @staticmethod
    def _resolve_interval(raw: Any) -> Optional[int]:
        """
        Convert *raw* to a clamped integer interval.

        :param raw:     Value to convert; any type accepted by ``int()``.
        :returns:       - Clamped interval in ``[MIN_SERVICE_INTERVAL, MAX_SERVICE_INTERVAL]``.
                        - ``None`` if *raw* is ``None`` or not numeric.
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

        iptv_dir = os.path.join(Config.profileDir, "iptv")
        if not os.path.isdir(iptv_dir):
            try:
                os.makedirs(iptv_dir)
            except OSError as exc:
                Logger.error("RetroService: failed to create iptv dir", exc_info=True)
                return

        parser = ActionParser(Config.addonId, 0, "")
        streams = channel.create_iptv_streams(parser)
        helper = IptvSimpleHelper()
        if streams:
            helper.write_playlist(streams, channel_entry.id)

        epg = channel.create_iptv_epg(parser)
        if epg:
            helper.write_epg(epg, channel_entry.id, streams=streams)


    def _enroll_channels(self) -> List[_ChannelTask]:
        """ Enroll channels that opt into background service tasks.

        :returns:   List of tasks for all channels that expose a ``service_update`` callback.
        """

        tasks: List[_ChannelTask] = []

        iptv_active = self._iptv_active()
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

            if (iptv_active and
                    channel_entry.has_iptv):
                raw_iptv_interval = getattr(channel, 'iptv_refresh_interval', None)
                iptv_interval = self._resolve_interval(
                    raw_iptv_interval if raw_iptv_interval is not None else IPTV_INTERVAL_DEFAULT)
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
        """ Run pending service tasks.

        :param tasks:   List of enrolled channel tasks to evaluate and dispatch.

        """

        now = time.time()
        for task in tasks:
            if self.abortRequested():
                return

            if now - task.last_run < task.interval:
                continue

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
        """ Service main loop. """

        Logger.info("RetroService: started")

        if self._iptv_active():
            IptvSimpleHelper().setup_iptvsimple()
        tasks = [t for t in self._enroll_channels() if t.interval is not None]

        if not tasks:
            Logger.info("RetroService: no tasks enrolled")
            return

        tick = min(_CHECK_INTERVAL, min(task.interval for task in tasks))
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

        if self._iptv_active():
            if ("pvr.iptvsimple" in data or
                    "service.iptv.manager" in data):
                Logger.info("RetroService: pvr.iptvsimple/service.iptv.manager enabled — reconfiguring")
                IptvSimpleHelper().setup_iptvsimple()


def run_service() -> None:
    """ Bootstrap the logger and run the Retrospect background service. """

    log_file = Logger.create_logger(
        os.path.join(Config.profileDir, Config.logFileNameAddon),
        Config.appName + " [service]",
        append=False,
        dual_logger=lambda msg, level=4: xbmc.log(msg, level))

    try:
        RetroService()._run()
    finally:
        log_file.close_log()
