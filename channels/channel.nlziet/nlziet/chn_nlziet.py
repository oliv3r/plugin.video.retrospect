# SPDX-License-Identifier: GPL-3.0-or-later
""" NLZIET channel for Retrospect. """

import json
import time
from typing import Dict, final

from resources.lib import chn_class
from resources.lib.addonsettings import AddonSettings, LOCAL
from resources.lib.channelinfo import ChannelInfo
from resources.lib.logger import Logger
from resources.lib.urihandler import UriHandler


# NLZIET API
API_CONTENT_URL = "https://api.nlziet.nl"

# V7 API
API_V7_APPCONFIG = "/v7/appconfig"

# NLZIET channel defaults
APPCONFIG_CACHE_KEY = "nlziet_appconfig"
APPCONFIG_HEARTBEAT_DEFAULT = 90  # seconds


@final
class Channel(chn_class.Channel):
    """ NLZIET channel for Retrospect. """

    service_interval: int = APPCONFIG_HEARTBEAT_DEFAULT
    """ Appconfig heartbeat interval in seconds. """

    is_blocked: bool = False
    """ Set to ``True`` when the server reports ``isAppBlocked`` in the appconfig. """

    blocked_reason: str = ""
    """ Human-readable block reason from the API (``appBlockedReason``), or empty string. """

    is_update_required: bool = False
    """ Set to ``True`` when the server reports ``isUpdateRequired`` in the appconfig. """

    update_reason: str = ""
    """ Human-readable update reason from the API (``updateText``), or empty string. """


    def __init__(self, channel_info: ChannelInfo) -> None:
        """
        Initialization of the class.

        All class variables should be instantiated here and this method should
        not be overridden by any derived classes.

        :param channel_info: The channel info object to base this channel on.
        """

        chn_class.Channel.__init__(self, channel_info)

        self.baseUrl = API_CONTENT_URL
        self.mainListUri = "#mainlist"
        self.noImage = channel_info.icon


    # -- Appconfig cache ---------------------------------------------------

    def _sync_appconfig(self) -> None:
        """ Fetch and cache the appconfig from the API. """

        Logger.debug(f"NLZIET: Syncing appconfig from {self.baseUrl}{API_V7_APPCONFIG}")

        raw = UriHandler.open(self._prefix_urls(API_V7_APPCONFIG + "?os=web&origin=app"))
        status = UriHandler.instance().status
        if status.error:
            Logger.error(f"NLZIET: Could not fetch appconfig: {status.code} {status.reason}")
            return
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            Logger.warning("NLZIET: Could not parse appconfig response")
            return
        if not isinstance(data, dict):
            Logger.warning("NLZIET: Appconfig response is not a JSON object")
            return

        cached_raw = AddonSettings.get_setting(APPCONFIG_CACHE_KEY, store=LOCAL)
        try:
            settings_cached = json.loads(cached_raw) if cached_raw else {}
        except (ValueError, TypeError):
            settings_cached = {}

        settings_cached.pop("_synced_at", None)
        settings_no_synced_at = {k: v for k, v in data.items() if k != "_synced_at"}
        if settings_no_synced_at != settings_cached:
            data["_synced_at"] = time.time()
            AddonSettings.set_setting(APPCONFIG_CACHE_KEY, json.dumps(data), store=LOCAL)

        Channel.service_interval = data.get("heartbeatInterval", APPCONFIG_HEARTBEAT_DEFAULT)
        Logger.debug(f"NLZIET: Next heartbeat in {Channel.service_interval}s")

        Channel.is_blocked = data.get("isAppBlocked", False)
        Channel.blocked_reason = data.get("appBlockedReason") or ""
        if Channel.is_blocked:
            Logger.warning(f"NLZIET: App is blocked - {Channel.blocked_reason!r}")

        Channel.is_update_required = data.get("isUpdateRequired", False)
        Channel.update_reason = data.get("updateText") or ""
        if Channel.is_update_required:
            Logger.warning(f"NLZIET: API update required - {Channel.update_reason!r}")


    # -- Background service ------------------------------------------------

    def service_update(self) -> None:
        """ Periodic background callback. """

        self._sync_appconfig()
