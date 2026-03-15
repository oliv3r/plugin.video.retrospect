# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import dataclasses
import os.path
import time
from typing import Any, Callable, List, Optional

import xbmc

from resources.lib.helpers.channelimporter import ChannelIndex
from resources.lib.logger import Logger
from resources.lib.retroconfig import Config

MIN_SERVICE_INTERVAL = 60     # 1 minute
MAX_SERVICE_INTERVAL = 3600   # 60 minutes

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


    def _enroll_channels(self) -> List[_ChannelTask]:
        """ Enroll channels that opt into background service tasks.

        :returns:   List of tasks for all channels that expose a ``service_update`` callback.
        """

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

        tasks = self._enroll_channels()

        if not tasks:
            Logger.info("RetroService: no tasks enrolled")
            return

        tick = min(_CHECK_INTERVAL, min(task.interval for task in tasks))
        Logger.info("RetroService: entering main loop")
        Logger.debug("RetroService: main loop tick every %ss", tick)
        while not self.waitForAbort(tick):
            self._tick(tasks)

        Logger.info("RetroService: stopped")


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
