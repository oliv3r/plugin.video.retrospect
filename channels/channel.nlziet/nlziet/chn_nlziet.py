# SPDX-License-Identifier: GPL-3.0-or-later
"""NLZIET channel for Retrospect."""

import api
import json
import time

from resources.lib import chn_class
from resources.lib.addonsettings import AddonSettings, LOCAL
from resources.lib.logger import Logger
from resources.lib.urihandler import UriHandler

_APPCONFIG_CACHE_KEY = "nlziet_appconfig"
_APPCONFIG_HEARTBEAT_DEFAULT = 90  # seconds; fallback when server value is absent


class Channel(chn_class.Channel):
    service_interval = _APPCONFIG_HEARTBEAT_DEFAULT
    """Refresh the appconfig every :data:`_APPCONFIG_HEARTBEAT_DEFAULT` seconds
    until the first successful fetch, after which it follows the server-provided
    ``heartbeatInterval``.
    """

    def __init__(self, channel_info):
        """Initialisation of the class.

        All class variables should be instantiated here and this method should not
        be overridden by any derived classes.

        :param ChannelInfo channel_info: The channel info object to base this channel on.

        """

        chn_class.Channel.__init__(self, channel_info)

        self.noImage = channel_info.icon
        self.mainListUri = "#mainlist"
        self.baseUrl = "https://api.nlziet.nl"


    def _refresh_appconfig(self) -> dict:
        """Fetch ``/v7/appconfig`` and store the result in the local settings cache.

        This is the write side of the appconfig cache: it always performs a
        network request and overwrites whatever was previously cached.  Browser
        code that needs the appconfig (read side) will find a warm entry here.

        :return: The parsed appconfig payload, or an empty dict on failure.
        :rtype: dict
        """

        Logger.debug("NLZIET: refreshing appconfig from %s%s", self.baseUrl, api.API_V7_APPCONFIG)

        raw = UriHandler.open(
            self._prefix_urls(api.API_V7_APPCONFIG + "?os=web&origin=app"),
            additional_headers=self.httpHeaders)
        if not raw:
            Logger.warning("NLZIET: could not fetch appconfig")
            return {}

        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            Logger.warning("NLZIET: could not parse appconfig response")
            return {}

        data["_fetched_at"] = time.time()
        AddonSettings.set_setting(_APPCONFIG_CACHE_KEY, json.dumps(data), store=LOCAL)

        Channel.service_interval = data.get("heartbeatInterval", _APPCONFIG_HEARTBEAT_DEFAULT)
        Logger.debug("NLZIET: next heartbeat in %ss", Channel.service_interval)

        is_blocked = data.get("isAppBlocked", False)
        Logger.info("NLZIET: isAppBlocked=%s", is_blocked)

        return data


    def on_service(self) -> None:
        """Periodic background callback: proactively refresh the appconfig cache.

        Called by the Retrospect background service every
        :attr:`~chn_class.Channel.service_interval` seconds so that the cached
        appconfig is always fresh when a user opens the channel.
        """

        self._refresh_appconfig()
