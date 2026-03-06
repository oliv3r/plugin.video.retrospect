# SPDX-License-Identifier: GPL-3.0-or-later
"""NLZIET channel for Retrospect."""

import json
import time

from resources.lib import chn_class
from resources.lib.addonsettings import AddonSettings, LOCAL
from resources.lib.logger import Logger
from resources.lib.urihandler import UriHandler


class Channel(chn_class.Channel):
    APPCONFIG_CACHE_KEY = "nlziet_appconfig"
    APPCONFIG_HEARTBEAT_DEFAULT = 90  # seconds

    API_CONTENT_URL = "https://api.nlziet.nl"

    API_V7_APPCONFIG = "/v7/appconfig"


    service_interval = APPCONFIG_HEARTBEAT_DEFAULT
    """Refresh the appconfig every :data:`APPCONFIG_HEARTBEAT_DEFAULT` seconds
    until the first successful fetch, after which it follows the server-provided
    ``heartbeatInterval``.
    """

    is_blocked = False
    """Set to ``True`` when the server reports ``isAppBlocked`` in the appconfig."""

    is_update_required = False
    """Set to ``True`` when the server reports ``isUpdateRequired`` in the appconfig."""


    def __init__(self, channel_info):
        """Initialisation of the class.

        All class variables should be instantiated here and this method should not
        be overridden by any derived classes.

        :param ChannelInfo channel_info: The channel info object to base this channel on.

        """

        chn_class.Channel.__init__(self, channel_info)

        self.baseUrl = Channel.API_CONTENT_URL
        self.mainListUri = "#mainlist"
        self.noImage = channel_info.icon


    # -- Appconfig cache ---------------------------------------------------

    def __sync_appconfig(self) -> None:
        """Fetch ``/appconfig`` and store the result in the local settings cache.

        This is the write side of the appconfig cache: it always performs a
        network request and overwrites whatever was previously cached on change.
        """

        Logger.debug(f"NLZIET: syncing appconfig from {self.baseUrl}{self.API_V7_APPCONFIG}")

        raw = UriHandler.open(
            self._prefix_urls(self.API_V7_APPCONFIG + "?os=web&origin=app"),
            additional_headers=self.httpHeaders)
        if not raw:
            Logger.warning("NLZIET: could not fetch appconfig")
            return

        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            Logger.warning("NLZIET: could not parse appconfig response")
            return

        cached_raw = AddonSettings.get_setting(self.APPCONFIG_CACHE_KEY, store=LOCAL)
        try:
            cached = json.loads(cached_raw) if cached_raw else {}
        except (ValueError, TypeError):
            cached = {}

        cached.pop("_synced_at", None)
        data_no_ts = {k: v for k, v in data.items() if k != "_synced_at"}
        if data_no_ts != cached:
            data["_synced_at"] = time.time()
            AddonSettings.set_setting(self.APPCONFIG_CACHE_KEY, json.dumps(data), store=LOCAL)

        Channel.service_interval = data.get("heartbeatInterval", self.APPCONFIG_HEARTBEAT_DEFAULT)
        Logger.debug(f"NLZIET: next heartbeat in {Channel.service_interval}s")

        Channel.is_blocked = data.get("isAppBlocked", False)
        if Channel.is_blocked:
            Logger.warning("NLZIET: App is blocked!")

        Channel.is_update_required = data.get("isUpdateRequired", False)
        if Channel.is_update_required:
            Logger.warning(f"NLZIET: API update required — {data.get('updateText', 'no details provided')}")

    def on_service(self) -> None:
        """Periodic background callback: sync the appconfig cache with the server.

        Called by the Retrospect background service every
        :attr:`~chn_class.Channel.service_interval` seconds so that the cached
        appconfig is always fresh when a user opens the channel.
        """
        if xbmc.Monitor().abortRequested():
            return
        self.__sync_appconfig()
